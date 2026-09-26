import uuid
from typing import Any

from django.conf import settings
from django.db import connection

from common.api.errors import APIError


def _dict_rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def license_summary(tenant_id: uuid.UUID) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT b.product_id,b.license_id,b.entitlement_revision,b.billing_timezone,
                   l.status,l.license_number_last4,l.issued_at,
                   current_term.id,current_term.plan_version_id,current_term.starts_at,
                   current_term.expires_at,current_term.support_valid_until,
                   current_term.updates_valid_until,current_term.max_activations_snapshot,
                   (SELECT count(*) FROM licensing.license_activations a
                    WHERE a.license_id=b.license_id AND a.deactivated_at IS NULL) active_activations
            FROM licensing.products p
            JOIN licensing.tenant_product_bindings b
              ON b.product_id=p.id AND b.tenant_id=%s
            LEFT JOIN licensing.licenses l
              ON l.id=b.license_id AND l.tenant_id=b.tenant_id
            LEFT JOIN LATERAL (
                SELECT t.* FROM licensing.license_terms t
                WHERE t.license_id=b.license_id AND t.starts_at<=clock_timestamp()
                  AND (t.expires_at IS NULL OR clock_timestamp()<t.expires_at)
                ORDER BY t.starts_at DESC,t.id DESC LIMIT 1
            ) current_term ON true
            WHERE p.product_code=%s AND p.is_active
            """,
            [tenant_id, settings.ERP_PRODUCT_CODE],
        )
        row = cursor.fetchone()
        if row is None:
            raise APIError(
                code="PRODUCT_NOT_CONFIGURED",
                message="No product entitlement coordinator is configured for this tenant.",
                status_code=503,
            )
        plan_version_id = row[8]
        cursor.execute(
            """
            SELECT feature_code,is_enabled,limit_value
            FROM licensing.plan_features
            WHERE plan_version_id=%s ORDER BY feature_code
            """,
            [plan_version_id],
        )
        features = _dict_rows(cursor)
        cursor.execute(
            """
            SELECT id,plan_version_id,starts_at,expires_at
            FROM licensing.license_terms
            WHERE license_id=%s AND starts_at>clock_timestamp()
            ORDER BY starts_at,id LIMIT 1
            """,
            [row[1]],
        )
        next_term = cursor.fetchone()
    current_term = None
    if row[7] is not None:
        current_term = {
            "id": row[7],
            "plan_version_id": row[8],
            "starts_at": row[9],
            "expires_at": row[10],
            "support_valid_until": row[11],
            "updates_valid_until": row[12],
        }
    return {
        "product_id": row[0],
        "license_id": row[1],
        "entitlement_revision": int(row[2]),
        "billing_timezone": row[3],
        "status": row[4] or "unbound",
        "license_number_last4": row[5],
        "issued_at": row[6],
        "current_term": current_term,
        "next_term": (
            {
                "id": next_term[0],
                "plan_version_id": next_term[1],
                "starts_at": next_term[2],
                "expires_at": next_term[3],
            }
            if next_term
            else None
        ),
        "features": features,
        "activation_quota": int(row[13]) if row[13] is not None else None,
        "active_activations": int(row[14]),
        "read_only_reason": (
            None
            if row[4] == "active" and current_term is not None
            else "LICENSE_INACTIVE_OR_EXPIRED"
        ),
    }


def list_activations(tenant_id: uuid.UUID, license_id: uuid.UUID | None) -> list[dict[str, Any]]:
    if license_id is None:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id,activated_at,last_seen_at,deactivated_at,
                   device_public_key IS NOT NULL AS has_device_public_key
            FROM licensing.license_activations
            WHERE tenant_id=%s AND license_id=%s
            ORDER BY activated_at DESC,id DESC
            """,
            [tenant_id, license_id],
        )
        return _dict_rows(cursor)


def renewal_order(tenant_id: uuid.UUID, order_id: uuid.UUID) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id,license_id,plan_version_id,currency_code,amount,provider,
                   provider_order_reference,status,fulfilled_term_id,created_at,fulfilled_at
            FROM licensing.billing_orders WHERE tenant_id=%s AND id=%s
            """,
            [tenant_id, order_id],
        )
        rows = _dict_rows(cursor)
    return rows[0] if rows else None
