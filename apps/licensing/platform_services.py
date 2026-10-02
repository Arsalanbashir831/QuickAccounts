import calendar
import datetime as dt
import hashlib
import json
import secrets
import uuid
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.db import connections, transaction
from django.utils import timezone

from apps.identity.models import User
from common.api.errors import Conflict, ScopeNotFound


def _execute(sql: str, params: list[Any]) -> tuple[Any, ...] | None:
    with connections[_platform_alias()].cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return None
        return cast(tuple[Any, ...] | None, cursor.fetchone())


def _platform_alias() -> str:
    if "platform" in settings.DATABASES:
        return "platform"
    if settings.DEBUG or getattr(settings, "PLATFORM_ALLOW_DEFAULT_CONNECTION", False):
        return "default"
    from common.api.errors import APIError

    raise APIError(
        code="PLATFORM_DATABASE_NOT_CONFIGURED",
        message="Platform onboarding is unavailable until DATABASE_PLATFORM_URL is configured.",
        status_code=503,
        retryable=False,
    )


def _hash(payload: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).digest()


def _claim_or_replay(
    operator_id: uuid.UUID,
    operation: str,
    resource_key: str,
    key: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    lock_key = f"{operator_id}:{operation}:{resource_key}:{key}"
    _execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", [lock_key])
    row = _execute(
        """
        SELECT request_hash,result_reference
        FROM platform.operator_command_receipts
        WHERE operator_user_id=%s AND operation=%s AND resource_key=%s
          AND idempotency_key=%s
        """,
        [operator_id, operation, resource_key, key],
    )
    if row is None:
        return None
    if bytes(row[0]) != _hash(payload):
        raise Conflict(
            "IDEMPOTENCY_PAYLOAD_MISMATCH",
            "This idempotency key was already used with a different payload.",
        )
    body = row[1]
    if isinstance(body, str):
        body = json.loads(body)
    return cast(dict[str, Any], body)


def _record(
    operator_id: uuid.UUID,
    *,
    operation: str,
    resource_key: str,
    key: str,
    payload: dict[str, Any],
    body: dict[str, Any],
    reason: str,
    action: str,
    object_type: str,
    object_id: uuid.UUID,
) -> None:
    _execute(
        """
        INSERT INTO platform.operator_audit_events(
            operator_user_id,action,object_type,object_id,reason,event_data
        ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        """,
        [operator_id, action, object_type, str(object_id), reason, json.dumps(body)],
    )
    _execute(
        """
        INSERT INTO platform.operator_command_receipts(
            operator_user_id,operation,resource_key,idempotency_key,request_hash,
            response_status,result_reference
        ) VALUES (%s,%s,%s,%s,%s,200,%s::jsonb)
        """,
        [operator_id, operation, resource_key, key, _hash(payload), json.dumps(body)],
    )


def _add_term(
    start: dt.datetime, unit: str, count: int | None, timezone_name: str
) -> dt.datetime | None:
    if unit == "lifetime":
        return None
    if count is None:
        raise Conflict("INVALID_PLAN_TERM", "A fixed-term plan must define a term count.")
    if unit == "day":
        return start + dt.timedelta(days=count)
    months = count if unit == "month" else count * 12
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise Conflict("INVALID_BILLING_TIMEZONE", "The billing timezone is invalid.") from exc
    local = start.astimezone(zone)
    month_index = local.year * 12 + local.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day).astimezone(dt.UTC)


def provision_tenant(
    operator_id: uuid.UUID,
    *,
    name: str,
    company_code: str,
    company_name: str,
    currency: str,
    timezone_name: str,
    business_type: str,
    owner_email: str,
    owner_password: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    owner_email = User.objects.normalize_email(owner_email).lower()
    payload = {
        "name": name,
        "company_code": company_code,
        "company_name": company_name,
        "currency": currency,
        "timezone_name": timezone_name,
        "business_type": business_type,
        "owner_email": owner_email,
        "password_fingerprint": hashlib.sha256(owner_password.encode()).hexdigest(),
        "reason": reason,
    }
    validate_password(owner_password)
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, "tenant.provision", owner_email, idempotency_key, payload
        )
        if replay is not None:
            return replay
        if _execute("SELECT 1 FROM identity.users WHERE email=%s", [owner_email]):
            raise Conflict("OWNER_EMAIL_EXISTS", "This owner email is already registered.")
        if not _execute("SELECT 1 FROM erp.currencies WHERE code=%s AND is_active", [currency]):
            raise Conflict("CURRENCY_UNAVAILABLE", "The selected currency is not active.")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise Conflict("INVALID_TIMEZONE", "The company timezone is invalid.") from exc
        tenant = _execute("INSERT INTO erp.tenants(name) VALUES (%s) RETURNING id", [name])
        assert tenant is not None
        tenant_id = tenant[0]
        owner = User.objects.db_manager(_platform_alias()).create_user(owner_email, owner_password)
        _execute(
            "INSERT INTO identity.tenant_memberships(tenant_id,user_id,tenant_role) "
            "VALUES (%s,%s,'owner')",
            [tenant_id, owner.pk],
        )
        company = _execute(
            "INSERT INTO erp.companies(tenant_id,code,legal_name,"
            "functional_currency,timezone_name,business_type) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            [tenant_id, company_code, company_name, currency, timezone_name, business_type],
        )
        assert company is not None
        _execute(
            "INSERT INTO identity.company_memberships(tenant_id,company_id,user_id) "
            "VALUES (%s,%s,%s)",
            [tenant_id, company[0], owner.pk],
        )
        body = {
            "tenant_id": str(tenant_id),
            "company_id": str(company[0]),
            "owner_user_id": str(owner.pk),
        }
        _record(
            operator_id,
            operation="tenant.provision",
            resource_key=owner_email,
            key=idempotency_key,
            payload=payload,
            body=body,
            reason=reason,
            action="tenant.provisioned",
            object_type="tenant",
            object_id=tenant_id,
        )
        return body


def provision_company(
    operator_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    code: str,
    legal_name: str,
    currency: str,
    timezone_name: str,
    business_type: str,
    owner_user_id: uuid.UUID,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {
        "tenant_id": tenant_id,
        "code": code,
        "legal_name": legal_name,
        "currency": currency,
        "timezone_name": timezone_name,
        "business_type": business_type,
        "owner_user_id": owner_user_id,
        "reason": reason,
    }
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, "company.provision", str(tenant_id), idempotency_key, payload
        )
        if replay is not None:
            return replay
        if not _execute("SELECT 1 FROM erp.tenants WHERE id=%s", [tenant_id]):
            raise ScopeNotFound()
        if not _execute("SELECT 1 FROM erp.currencies WHERE code=%s AND is_active", [currency]):
            raise Conflict("CURRENCY_UNAVAILABLE", "The selected currency is not active.")
        if not _execute(
            "SELECT 1 FROM identity.tenant_memberships "
            "WHERE tenant_id=%s AND user_id=%s AND tenant_role='owner' AND is_active",
            [tenant_id, owner_user_id],
        ):
            raise Conflict(
                "OWNER_NOT_IN_TENANT", "The selected user is not an active tenant owner."
            )
        if _execute(
            "SELECT 1 FROM erp.companies WHERE tenant_id=%s AND code=%s", [tenant_id, code]
        ):
            raise Conflict("COMPANY_CODE_EXISTS", "This company code already exists in the tenant.")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise Conflict("INVALID_TIMEZONE", "The company timezone is invalid.") from exc
        company = _execute(
            "INSERT INTO erp.companies(tenant_id,code,legal_name,"
            "functional_currency,timezone_name,business_type) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            [tenant_id, code, legal_name, currency, timezone_name, business_type],
        )
        assert company is not None
        _execute(
            "INSERT INTO identity.company_memberships(tenant_id,company_id,user_id) "
            "VALUES (%s,%s,%s)",
            [tenant_id, company[0], owner_user_id],
        )
        body = {
            "tenant_id": str(tenant_id),
            "company_id": str(company[0]),
            "owner_user_id": str(owner_user_id),
        }
        _record(
            operator_id,
            operation="company.provision",
            resource_key=str(tenant_id),
            key=idempotency_key,
            payload=payload,
            body=body,
            reason=reason,
            action="company.provisioned",
            object_type="company",
            object_id=company[0],
        )
        return body


def assign_license(
    operator_id: uuid.UUID, license_id: uuid.UUID, *, reason: str, idempotency_key: str
) -> dict[str, Any]:
    payload = {"license_id": license_id, "reason": reason}
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, "license.assign", str(license_id), idempotency_key, payload
        )
        if replay is not None:
            return replay
        license_row = _execute(
            "SELECT tenant_id,product_id,status FROM licensing.licenses WHERE id=%s FOR UPDATE",
            [license_id],
        )
        if license_row is None:
            raise ScopeNotFound()
        tenant_id, product_id, license_status = license_row
        if license_status != "active":
            raise Conflict("LICENSE_NOT_ACTIVE", "Only an active license can be assigned.")
        term = _execute(
            "SELECT 1 FROM licensing.license_terms WHERE license_id=%s "
            "AND starts_at<=now() AND (expires_at IS NULL OR expires_at>now()) LIMIT 1",
            [license_id],
        )
        if term is None:
            raise Conflict("LICENSE_TERM_INACTIVE", "The license has no current term.")
        binding = _execute(
            "SELECT license_id FROM licensing.tenant_product_bindings "
            "WHERE tenant_id=%s AND product_id=%s FOR UPDATE",
            [tenant_id, product_id],
        )
        if binding is None:
            raise Conflict("ENTITLEMENT_COORDINATOR_MISSING", "License coordinator is missing.")
        if binding[0] is not None:
            raise Conflict(
                "LICENSE_ALREADY_ASSIGNED",
                "A license is already assigned to this tenant and product.",
            )
        _execute(
            "UPDATE licensing.tenant_product_bindings SET license_id=%s "
            "WHERE tenant_id=%s AND product_id=%s",
            [license_id, tenant_id, product_id],
        )
        _execute(
            "UPDATE licensing.licenses SET redeemed_at=clock_timestamp(),redeemed_by=%s "
            "WHERE id=%s",
            [operator_id, license_id],
        )
        _execute(
            "INSERT INTO licensing.license_events"
            "(tenant_id,license_id,event_type,actor_reference,event_data) "
            "VALUES (%s,%s,'redeemed',%s,%s::jsonb)",
            [
                tenant_id,
                license_id,
                str(operator_id),
                json.dumps({"reason": reason, "source": "platform"}),
            ],
        )
        body = {"id": str(license_id), "tenant_id": str(tenant_id), "status": "assigned"}
        _record(
            operator_id,
            operation="license.assign",
            resource_key=str(license_id),
            key=idempotency_key,
            payload=payload,
            body=body,
            reason=reason,
            action="license.assigned",
            object_type="license",
            object_id=license_id,
        )
        return body


def issue_license(
    operator_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    plan_version_id: uuid.UUID,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {"tenant_id": tenant_id, "plan_version_id": plan_version_id, "reason": reason}
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, "license.issue", str(tenant_id), idempotency_key, payload
        )
        if replay is not None:
            return replay
        plan = _execute(
            """
            SELECT product_id,term_unit,term_count,max_activations
            FROM licensing.plan_versions
            WHERE id=%s AND published_at IS NOT NULL
            """,
            [plan_version_id],
        )
        if plan is None:
            raise Conflict("PLAN_NOT_PUBLISHED", "The selected plan version is not published.")
        credential = f"qa_{secrets.token_urlsafe(32)}"
        digest = hashlib.sha256(credential.strip().encode()).digest()
        license_row = _execute(
            """
            INSERT INTO licensing.licenses(
                tenant_id,product_id,license_number_hash,license_number_last4,status
            ) VALUES (%s,%s,%s,%s,'active') RETURNING id
            """,
            [tenant_id, plan[0], digest, credential[-4:]],
        )
        assert license_row is not None
        license_id = cast(uuid.UUID, license_row[0])
        binding = _execute(
            """
            SELECT billing_timezone FROM licensing.tenant_product_bindings
            WHERE tenant_id=%s AND product_id=%s FOR UPDATE
            """,
            [tenant_id, plan[0]],
        )
        if binding is None:
            raise Conflict("ENTITLEMENT_COORDINATOR_MISSING", "License coordinator is missing.")
        starts_at = timezone.now()
        expires_at = _add_term(starts_at, plan[1], plan[2], binding[0])
        term = _execute(
            """
            INSERT INTO licensing.license_terms(
                tenant_id,license_id,plan_version_id,term_unit_snapshot,
                term_count_snapshot,starts_at,expires_at,max_activations_snapshot
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            [
                tenant_id,
                license_id,
                plan_version_id,
                plan[1],
                plan[2],
                starts_at,
                expires_at,
                plan[3],
            ],
        )
        assert term is not None
        _execute(
            """
            UPDATE licensing.tenant_product_bindings
            SET billing_anchor_day=%s,month_end_anchor=%s
            WHERE tenant_id=%s AND product_id=%s
            """,
            [
                starts_at.day,
                starts_at.day == calendar.monthrange(starts_at.year, starts_at.month)[1],
                tenant_id,
                plan[0],
            ],
        )
        _execute(
            """
            INSERT INTO licensing.license_events(
                tenant_id,license_id,event_type,actor_reference,event_data
            ) VALUES (%s,%s,'issued',%s,%s::jsonb)
            """,
            [
                tenant_id,
                license_id,
                str(operator_id),
                json.dumps({"plan_version_id": str(plan_version_id)}),
            ],
        )
        body = {
            "id": str(license_id),
            "tenant_id": str(tenant_id),
            "plan_version_id": str(plan_version_id),
            "license_term_id": str(term[0]),
            "credential": credential,
            "status": "active",
        }
        persisted_result = {**body, "credential": None, "credential_delivered": True}
        _record(
            operator_id,
            operation="license.issue",
            resource_key=str(tenant_id),
            key=idempotency_key,
            payload=payload,
            body=persisted_result,
            reason=reason,
            action="license.issued",
            object_type="license",
            object_id=license_id,
        )
        return body


def change_license_status(
    operator_id: uuid.UUID,
    license_id: uuid.UUID,
    *,
    action: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {"license_id": license_id, "action": action, "reason": reason}
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, f"license.{action}", str(license_id), idempotency_key, payload
        )
        if replay is not None:
            return replay
        target = {"suspend": "suspended", "resume": "active", "revoke": "revoked"}[action]
        candidate = _execute(
            """
            SELECT tenant_id,product_id
            FROM licensing.licenses
            WHERE id=%s
            """,
            [license_id],
        )
        if candidate is None:
            raise ScopeNotFound()
        coordinator = _execute(
            """
            SELECT license_id
            FROM licensing.tenant_product_bindings
            WHERE tenant_id=%s AND product_id=%s
            FOR UPDATE
            """,
            [candidate[0], candidate[1]],
        )
        if coordinator is None:
            raise Conflict(
                "ENTITLEMENT_COORDINATOR_MISSING",
                "The tenant product entitlement coordinator is missing.",
            )
        row = _execute(
            """
            SELECT tenant_id,product_id,status
            FROM licensing.licenses
            WHERE id=%s AND tenant_id=%s AND product_id=%s
            FOR UPDATE
            """,
            [license_id, candidate[0], candidate[1]],
        )
        if row is None:
            raise ScopeNotFound()
        if row[2] == "revoked":
            raise Conflict("LICENSE_REVOKED", "A revoked license cannot change status.")
        allowed_from = {"suspend": "active", "resume": "suspended", "revoke": row[2]}
        if row[2] != allowed_from[action]:
            raise Conflict(
                "LICENSE_STATUS_CONFLICT",
                f"The license cannot be changed from {row[2]} using {action}.",
            )
        _execute("UPDATE licensing.licenses SET status=%s WHERE id=%s", [target, license_id])
        _execute(
            """
            INSERT INTO licensing.license_events(
                tenant_id,license_id,event_type,actor_reference,event_data
            ) VALUES (%s,%s,%s,%s,%s::jsonb)
            """,
            [
                row[0],
                license_id,
                "resumed" if action == "resume" else target,
                str(operator_id),
                json.dumps({"reason": reason}),
            ],
        )
        revision = _execute(
            """
            SELECT entitlement_revision FROM licensing.tenant_product_bindings
            WHERE tenant_id=%s AND product_id=%s
            """,
            [row[0], row[1]],
        )
        body = {
            "id": str(license_id),
            "status": target,
            "entitlement_revision": int(revision[0]) if revision else None,
        }
        _record(
            operator_id,
            operation=f"license.{action}",
            resource_key=str(license_id),
            key=idempotency_key,
            payload=payload,
            body=body,
            reason=reason,
            action="license.resumed" if action == "resume" else f"license.{target}",
            object_type="license",
            object_id=license_id,
        )
        return body


def create_plan_version(
    operator_id: uuid.UUID,
    *,
    plan_id: uuid.UUID,
    version_number: int,
    term_unit: str,
    term_count: int | None,
    max_activations: int,
    permits_offline_use: bool,
    price_currency: str | None,
    price_amount: Any,
    features: list[dict[str, Any]],
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {
        "plan_id": plan_id,
        "version_number": version_number,
        "term_unit": term_unit,
        "term_count": term_count,
        "max_activations": max_activations,
        "permits_offline_use": permits_offline_use,
        "price_currency": price_currency,
        "price_amount": price_amount,
        "features": features,
        "reason": reason,
    }
    resource_key = f"{plan_id}:{version_number}"
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, "plan_version.create", resource_key, idempotency_key, payload
        )
        if replay is not None:
            return replay
        plan = _execute(
            "SELECT product_id FROM licensing.plans WHERE id=%s AND is_active FOR UPDATE",
            [plan_id],
        )
        if plan is None:
            raise Conflict("PLAN_UNAVAILABLE", "The selected plan does not exist or is inactive.")
        if price_currency and not _execute(
            "SELECT 1 FROM erp.currencies WHERE code=%s AND is_active", [price_currency]
        ):
            raise Conflict("CURRENCY_UNAVAILABLE", "The selected currency is not active.")
        if _execute(
            "SELECT 1 FROM licensing.plan_versions WHERE plan_id=%s AND version_number=%s",
            [plan_id, version_number],
        ):
            raise Conflict(
                "PLAN_VERSION_EXISTS", "This version number already exists for the plan."
            )
        row = _execute(
            """
            INSERT INTO licensing.plan_versions(
                product_id,plan_id,version_number,term_unit,term_count,max_activations,
                permits_offline_use,price_currency,price_amount
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,product_id,created_at
            """,
            [
                plan[0], plan_id, version_number, term_unit, term_count, max_activations,
                permits_offline_use, price_currency, price_amount,
            ],
        )
        assert row is not None
        plan_version_id = cast(uuid.UUID, row[0])
        for feature in features:
            _execute(
                """
                INSERT INTO licensing.plan_features(
                    plan_version_id,feature_code,is_enabled,limit_value
                ) VALUES (%s,%s,%s,%s)
                """,
                [
                    plan_version_id,
                    feature["feature_code"],
                    feature["is_enabled"],
                    feature.get("limit_value"),
                ],
            )
        body = {
            "id": str(plan_version_id),
            "product_id": str(row[1]),
            "plan_id": str(plan_id),
            "version_number": version_number,
            "status": "draft",
            "created_at": row[2].isoformat(),
        }
        _record(
            operator_id,
            operation="plan_version.create",
            resource_key=resource_key,
            key=idempotency_key,
            payload=payload,
            body=body,
            reason=reason,
            action="plan_version.created",
            object_type="plan_version",
            object_id=plan_version_id,
        )
        return body


def publish_plan_version(
    operator_id: uuid.UUID,
    plan_version_id: uuid.UUID,
    *,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {"plan_version_id": plan_version_id, "reason": reason}
    with transaction.atomic(using=_platform_alias(), durable=True):
        replay = _claim_or_replay(
            operator_id, "plan.publish", str(plan_version_id), idempotency_key, payload
        )
        if replay is not None:
            return replay
        row = _execute(
            """
            UPDATE licensing.plan_versions SET published_at=clock_timestamp()
            WHERE id=%s AND published_at IS NULL RETURNING published_at
            """,
            [plan_version_id],
        )
        if row is None:
            raise Conflict("PLAN_NOT_DRAFT", "Only a draft plan version can be published.")
        body = {"id": str(plan_version_id), "published_at": row[0].isoformat()}
        _record(
            operator_id,
            operation="plan.publish",
            resource_key=str(plan_version_id),
            key=idempotency_key,
            payload=payload,
            body=body,
            reason=reason,
            action="plan_version.published",
            object_type="plan_version",
            object_id=plan_version_id,
        )
        return body
