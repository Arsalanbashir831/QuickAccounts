import base64
import binascii
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.licensing.selectors import renewal_order
from common.access.scopes import TenantScope, bind_and_verify_tenant
from common.api.errors import APIError, Conflict, ScopeNotFound


@dataclass(frozen=True, slots=True)
class Receipt:
    id: uuid.UUID
    replay_body: dict[str, Any] | None = None
    replay_status: int | None = None


def _execute(sql: str, params: list[Any]) -> tuple[Any, ...] | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return None
        return cast(tuple[Any, ...] | None, cursor.fetchone())


def _bound_license(tenant_id: uuid.UUID, *, lock: bool = False) -> tuple[uuid.UUID, uuid.UUID]:
    suffix = " FOR UPDATE" if lock else ""
    row = _execute(
        """
        SELECT b.product_id,b.license_id
        FROM licensing.products p
        JOIN licensing.tenant_product_bindings b
          ON b.product_id=p.id AND b.tenant_id=%s
        WHERE p.product_code=%s AND p.is_active
        """
        + suffix,
        [tenant_id, settings.ERP_PRODUCT_CODE],
    )
    if row is None or row[1] is None:
        raise Conflict(
            "LICENSE_NOT_BOUND", "No designated product license is bound to this tenant."
        )
    return cast(uuid.UUID, row[0]), cast(uuid.UUID, row[1])


def _claim_receipt(
    scope: TenantScope,
    *,
    operation: str,
    resource_key: str,
    key: str,
    payload: dict[str, Any],
) -> Receipt:
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).digest()
    row = _execute(
        """
        INSERT INTO erp.api_command_receipts(
            tenant_id,company_id,actor_user_id,operation,resource_key,
            idempotency_key,request_hash,retain_until
        ) VALUES (%s,NULL,%s,%s,%s,%s,%s,clock_timestamp()+interval '30 days')
        ON CONFLICT DO NOTHING RETURNING id
        """,
        [scope.tenant_id, scope.user_id, operation, resource_key, key, request_hash],
    )
    if row:
        return Receipt(cast(uuid.UUID, row[0]))
    row = _execute(
        """
        SELECT id,request_hash,status,response_status,response_body
        FROM erp.api_command_receipts
        WHERE tenant_id=%s AND company_id IS NULL AND actor_user_id=%s
          AND actor_service_id IS NULL AND operation=%s AND resource_key=%s
          AND idempotency_key=%s FOR UPDATE
        """,
        [scope.tenant_id, scope.user_id, operation, resource_key, key],
    )
    if row is None:
        raise Conflict("IDEMPOTENCY_CONFLICT", "The command receipt could not be resolved.")
    if bytes(row[1]) != request_hash:
        raise Conflict(
            "IDEMPOTENCY_PAYLOAD_MISMATCH",
            "This idempotency key was already used with a different payload.",
        )
    if row[2] != "completed":
        raise Conflict("COMMAND_IN_PROGRESS", "A command with this key is still processing.")
    body = row[4]
    if isinstance(body, str):
        body = json.loads(body)
    return Receipt(cast(uuid.UUID, row[0]), cast(dict[str, Any], body), int(row[3]))


def _complete(
    receipt: Receipt, body: dict[str, Any], status_code: int, result_id: uuid.UUID
) -> None:
    _execute(
        """
        UPDATE erp.api_command_receipts
        SET status='completed',response_status=%s,response_body=%s::jsonb,
            completed_at=clock_timestamp(),result_type='license_operation',result_id=%s
        WHERE id=%s
        """,
        [status_code, json.dumps(body, default=str), result_id, receipt.id],
    )


def create_activation(
    scope: TenantScope,
    *,
    fingerprint: str,
    public_key: str | None,
    idempotency_key: str,
) -> tuple[dict[str, Any], int]:
    payload = {"device_fingerprint": fingerprint, "device_public_key": public_key}
    try:
        decoded_key = base64.b64decode(public_key, validate=True) if public_key else None
    except (ValueError, binascii.Error) as exc:
        raise APIError(
            code="INVALID_PUBLIC_KEY", message="Device public key must be base64."
        ) from exc
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_tenant(scope, administer=True)
            receipt = _claim_receipt(
                scope,
                operation="license.activation.create",
                resource_key="",
                key=idempotency_key,
                payload=payload,
            )
            if receipt.replay_body is not None:
                return receipt.replay_body, receipt.replay_status or 201
            _, license_id = _bound_license(scope.tenant_id, lock=True)
            row = _execute(
                """
                INSERT INTO licensing.license_activations(
                    tenant_id,license_id,device_fingerprint_hash,device_public_key
                ) VALUES (%s,%s,%s,%s)
                RETURNING id,activated_at,last_seen_at,deactivated_at
                """,
                [
                    scope.tenant_id,
                    license_id,
                    hashlib.sha256(fingerprint.encode()).digest(),
                    decoded_key,
                ],
            )
            assert row is not None
            body = {
                "id": str(row[0]),
                "activated_at": row[1].isoformat(),
                "last_seen_at": row[2].isoformat(),
                "deactivated_at": row[3].isoformat() if row[3] else None,
            }
            _complete(receipt, body, 201, row[0])
            return body, 201
    except (APIError, Conflict):
        raise
    except (DatabaseError, IntegrityError) as exc:
        message = str(exc).lower()
        code = "ACTIVATION_QUOTA_EXCEEDED" if "quota" in message else "ACTIVATION_REJECTED"
        raise Conflict(code, "The license activation could not be allocated.") from exc


def touch_activation(
    scope: TenantScope, activation_id: uuid.UUID, *, deactivate: bool
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        bind_and_verify_tenant(scope, administer=True)
        _, license_id = _bound_license(scope.tenant_id)
        assignment = (
            "deactivated_at=coalesce(deactivated_at,clock_timestamp())"
            if deactivate
            else "last_seen_at=clock_timestamp()"
        )
        row = _execute(
            f"""
            UPDATE licensing.license_activations SET {assignment}
            WHERE tenant_id=%s AND license_id=%s AND id=%s
            RETURNING id,activated_at,last_seen_at,deactivated_at
            """,
            [scope.tenant_id, license_id, activation_id],
        )
        if row is None:
            raise ScopeNotFound()
        return {
            "id": str(row[0]),
            "activated_at": row[1].isoformat(),
            "last_seen_at": row[2].isoformat(),
            "deactivated_at": row[3].isoformat() if row[3] else None,
        }


def create_renewal_order(
    scope: TenantScope,
    *,
    plan_version_id: uuid.UUID,
    provider: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    with transaction.atomic(durable=True):
        bind_and_verify_tenant(scope, administer=True)
        product_id, license_id = _bound_license(scope.tenant_id, lock=True)
        row = _execute(
            """
            SELECT price_currency,price_amount
            FROM licensing.plan_versions
            WHERE id=%s AND product_id=%s AND published_at IS NOT NULL
            """,
            [plan_version_id, product_id],
        )
        if row is None or row[0] is None or row[1] is None:
            raise Conflict(
                "PLAN_NOT_OFFERED", "The selected published plan has no purchasable price."
            )
        created = _execute(
            """
            INSERT INTO licensing.billing_orders(
                tenant_id,license_id,plan_version_id,order_key,currency_code,
                amount,provider
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(tenant_id,order_key) DO NOTHING RETURNING id
            """,
            [
                scope.tenant_id,
                license_id,
                plan_version_id,
                idempotency_key,
                row[0],
                row[1],
                provider,
            ],
        )
        if created is None:
            existing = _execute(
                """
                SELECT id,plan_version_id,provider FROM licensing.billing_orders
                WHERE tenant_id=%s AND order_key=%s FOR UPDATE
                """,
                [scope.tenant_id, idempotency_key],
            )
            if existing is None:
                raise Conflict("IDEMPOTENCY_CONFLICT", "The renewal order could not be resolved.")
            if existing[1] != plan_version_id or existing[2] != provider:
                raise Conflict(
                    "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "This idempotency key was already used with a different renewal request.",
                )
            order_id = existing[0]
            was_created = False
        else:
            order_id = created[0]
            was_created = True
        body = renewal_order(scope.tenant_id, order_id)
        assert body is not None
        return body, was_created
