import uuid
from typing import Any

from django.conf import settings
from django.db import connection


def _dict_rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def module_configuration(company_id: uuid.UUID) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.revision,b.entitlement_revision,l.status AS license_status,
                   active.plan_version_id
            FROM erp.company_policy_state p
            JOIN erp.companies c ON c.id=p.company_id
            LEFT JOIN licensing.products product
              ON product.product_code=%s AND product.is_active
            LEFT JOIN licensing.tenant_product_bindings b
              ON b.tenant_id=c.tenant_id AND b.product_id=product.id
            LEFT JOIN licensing.licenses l
              ON l.id=b.license_id AND l.tenant_id=b.tenant_id
            LEFT JOIN LATERAL (
                SELECT term.plan_version_id
                FROM licensing.license_terms term
                WHERE term.license_id=b.license_id
                  AND term.starts_at<=clock_timestamp()
                  AND (term.expires_at IS NULL OR clock_timestamp()<term.expires_at)
                ORDER BY term.starts_at DESC,term.id DESC LIMIT 1
            ) active ON true
            WHERE p.company_id=%s
            """,
            [settings.ERP_PRODUCT_CODE, company_id],
        )
        header = cursor.fetchone()
        if header is None:
            return {"policy_revision": None, "entitlement_revision": None, "modules": []}
        cursor.execute(
            """
            SELECT cfg.module_code,cfg.name,cfg.is_required,cfg.release_status,
                   cfg.configured_mode,cfg.license_feature_code,
                   coalesce(array_agg(dep.required_module_code)
                       FILTER (WHERE dep.required_module_code IS NOT NULL),'{}') AS dependencies,
                   CASE
                     WHEN cfg.release_status<>'available' THEN false
                     WHEN cfg.license_feature_code IS NULL THEN true
                     ELSE EXISTS(
                         SELECT 1 FROM licensing.plan_features feature
                         WHERE feature.plan_version_id=%s
                           AND feature.feature_code=cfg.license_feature_code
                           AND feature.is_enabled
                     )
                   END AS licensed
            FROM erp.v_company_module_configuration cfg
            LEFT JOIN erp.module_dependencies dep ON dep.module_code=cfg.module_code
            WHERE cfg.company_id=%s
            GROUP BY cfg.module_code,cfg.name,cfg.is_required,cfg.release_status,
                     cfg.configured_mode,cfg.license_feature_code
            ORDER BY cfg.module_code
            """,
            [header[3], company_id],
        )
        modules = _dict_rows(cursor)
    return {
        "policy_revision": int(header[0]),
        "entitlement_revision": int(header[1]) if header[1] is not None else None,
        "license_status": header[2],
        "plan_version_id": header[3],
        "modules": modules,
    }


def capabilities(company_id: uuid.UUID) -> dict[str, Any]:
    result = module_configuration(company_id)
    module_by_code = {module["module_code"]: module for module in result["modules"]}
    for module in result["modules"]:
        reasons: list[str] = []
        if result.get("license_status") != "active":
            reasons.append("LICENSE_INACTIVE")
        if not module["licensed"]:
            reasons.append("FEATURE_NOT_LICENSED")
        if module["release_status"] != "available":
            reasons.append("MODULE_UNAVAILABLE")
        if module["configured_mode"] == "disabled":
            reasons.append("MODULE_DISABLED")
        elif module["configured_mode"] == "read_only":
            reasons.append("MODULE_READ_ONLY")
        module["read_allowed"] = (
            module["configured_mode"] != "disabled"
            and module["release_status"] == "available"
            and module["licensed"]
        )
        module["write_allowed"] = (
            module["read_allowed"]
            and module["configured_mode"] == "enabled"
            and result.get("license_status") == "active"
            and result.get("plan_version_id") is not None
        )
        module["denial_reasons"] = reasons
    permission_modules = {
        "company": "core",
        "sales": "sales",
        "purchasing": "purchasing",
        "payments": "payments",
        "inventory": "inventory",
        "manufacturing": "manufacturing",
        "accounting": "accounting",
        "tax": "tax_calculation",
        "reports": "accounting",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT code,identity.has_company_permission(%s,code) AS granted
            FROM identity.permissions ORDER BY code
            """,
            [company_id],
        )
        permissions = _dict_rows(cursor)
    actions = []
    for permission in permissions:
        code = permission["code"]
        module_code = permission_modules.get(code.split(".", 1)[0], "core")
        module = module_by_code.get(module_code)
        is_read = code.endswith(".view")
        module_allowed = bool(
            module and (module["read_allowed"] if is_read else module["write_allowed"])
        )
        reasons = []
        if not permission["granted"]:
            reasons.append("PERMISSION_DENIED")
        if not module_allowed:
            reasons.extend(module["denial_reasons"] if module else ["MODULE_UNAVAILABLE"])
        actions.append(
            {
                "code": code,
                "module_code": module_code,
                "allowed": bool(permission["granted"] and module_allowed),
                "denial_reasons": list(dict.fromkeys(reasons)),
            }
        )
    result["actions"] = actions
    result.pop("plan_version_id", None)
    return result


def module_events(company_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id,action,object_type,object_id,old_data,new_data,reason,
                   request_id,actor_user_id,actor_service_id,occurred_at
            FROM erp.company_audit_events
            WHERE company_id=%s AND object_type='module'
            ORDER BY occurred_at DESC,id DESC LIMIT %s
            """,
            [company_id, limit],
        )
        return _dict_rows(cursor)
