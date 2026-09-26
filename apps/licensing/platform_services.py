import calendar
import datetime as dt
import hashlib
import json
import secrets
import uuid
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import connection, transaction
from django.utils import timezone

from common.api.errors import Conflict, ScopeNotFound


def _execute(sql: str, params: list[Any]) -> tuple[Any, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return None
        return cast(tuple[Any, ...] | None, cursor.fetchone())


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


def issue_license(
    operator_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    plan_version_id: uuid.UUID,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {"tenant_id": tenant_id, "plan_version_id": plan_version_id, "reason": reason}
    with transaction.atomic(durable=True):
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
    with transaction.atomic(durable=True):
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


def publish_plan_version(
    operator_id: uuid.UUID,
    plan_version_id: uuid.UUID,
    *,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    payload = {"plan_version_id": plan_version_id, "reason": reason}
    with transaction.atomic(durable=True):
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
