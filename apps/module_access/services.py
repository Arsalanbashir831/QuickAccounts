import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from django.db import DatabaseError, connection, transaction

from apps.module_access.selectors import module_configuration
from common.access.scopes import (
    CompanyScope,
    bind_and_verify_company,
    require_permission,
    resolve_product_id,
)
from common.api.errors import Conflict, PermissionDenied, PreconditionFailed


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


def _hash(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).digest()


def _claim_receipt(scope: CompanyScope, idempotency_key: str, payload: dict[str, Any]) -> Receipt:
    request_hash = _hash(payload)
    row = _execute(
        """
        INSERT INTO erp.api_command_receipts(
            tenant_id,company_id,actor_user_id,operation,resource_key,
            idempotency_key,request_hash,retain_until
        ) VALUES (%s,%s,%s,'company.modules.change','',%s,%s,
                  clock_timestamp()+interval '30 days')
        ON CONFLICT DO NOTHING RETURNING id
        """,
        [scope.tenant_id, scope.company_id, scope.user_id, idempotency_key, request_hash],
    )
    if row is not None:
        return Receipt(cast(uuid.UUID, row[0]))
    row = _execute(
        """
        SELECT id,request_hash,status,response_status,response_body
        FROM erp.api_command_receipts
        WHERE tenant_id=%s AND company_id=%s AND actor_user_id=%s
          AND actor_service_id IS NULL AND operation='company.modules.change'
          AND resource_key='' AND idempotency_key=%s FOR UPDATE
        """,
        [scope.tenant_id, scope.company_id, scope.user_id, idempotency_key],
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


def validate_changes(
    scope: CompanyScope, changes: list[dict[str, Any]], expected_revision: int | None = None
) -> dict[str, Any]:
    bind_and_verify_company(scope)
    require_permission(scope.company_id, "company.modules.manage")
    current = module_configuration(scope.company_id)
    if expected_revision is not None and current["policy_revision"] != expected_revision:
        raise PreconditionFailed(current["policy_revision"])

    modules = {module["module_code"]: module for module in current["modules"]}
    proposed = {code: module["configured_mode"] for code, module in modules.items()}
    blockers: list[dict[str, Any]] = []
    for change in changes:
        code = change["module_code"]
        mode = change["mode"]
        module = modules.get(code)
        if module is None:
            blockers.append({"code": "UNKNOWN_MODULE", "module_code": code})
            continue
        proposed[code] = mode
        if module["is_required"] and mode != "enabled":
            blockers.append({"code": "REQUIRED_MODULE", "module_code": code})
        if mode == "enabled" and not module["licensed"]:
            blockers.append({"code": "FEATURE_NOT_LICENSED", "module_code": code})
        if mode == "enabled" and module["release_status"] != "available":
            blockers.append({"code": "MODULE_UNAVAILABLE", "module_code": code})

    for code, module in modules.items():
        if proposed[code] != "enabled":
            continue
        for dependency in module["dependencies"]:
            if proposed.get(dependency) != "enabled":
                blockers.append(
                    {
                        "code": "DEPENDENCY_CONFLICT",
                        "module_code": code,
                        "required_module_code": dependency,
                    }
                )
    return {
        "valid": not blockers,
        "policy_revision": current["policy_revision"],
        "blockers": blockers,
        "changes": changes,
    }


def apply_changes(
    scope: CompanyScope,
    *,
    changes: list[dict[str, Any]],
    reason: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str | None,
) -> tuple[dict[str, Any], int]:
    payload = {"changes": changes, "reason": reason, "expected_revision": expected_revision}
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            receipt = _claim_receipt(scope, idempotency_key, payload)
            if receipt.replay_body is not None:
                return receipt.replay_body, receipt.replay_status or 200
            validation = validate_changes(scope, changes, expected_revision)
            if not validation["valid"]:
                blockers = validation["blockers"]
                if any(blocker["code"] == "FEATURE_NOT_LICENSED" for blocker in blockers):
                    raise PermissionDenied(
                        "FEATURE_NOT_LICENSED", "The current license does not permit this module."
                    )
                raise Conflict("MODULE_CHANGE_BLOCKED", "The module change is invalid.", blockers)
            product_id = resolve_product_id()
            _execute("SELECT set_config('app.request_id',%s,true)", [request_id or ""])
            row = _execute(
                "SELECT erp.set_company_modules(%s,%s,%s,%s::jsonb,%s)",
                [scope.company_id, product_id, expected_revision, json.dumps(changes), reason],
            )
            assert row is not None
            revision = int(row[0])
            event_rows = _execute(
                """
                SELECT array_agg(id ORDER BY occurred_at,id)
                FROM erp.company_audit_events
                WHERE company_id=%s AND request_id IS NOT DISTINCT FROM %s
                  AND object_type='module'
                """,
                [scope.company_id, request_id],
            )
            body = {
                "company_id": str(scope.company_id),
                "policy_revision": revision,
                "changes": changes,
                "audit_event_ids": [str(value) for value in (event_rows[0] or [])]
                if event_rows
                else [],
            }
            _execute(
                """
                UPDATE erp.api_command_receipts
                SET status='completed',response_status=200,response_body=%s::jsonb,
                    completed_at=clock_timestamp(),result_type='company_policy',result_id=%s
                WHERE id=%s
                """,
                [json.dumps(body), scope.company_id, receipt.id],
            )
            return body, 200
    except (Conflict, PermissionDenied, PreconditionFailed):
        raise
    except DatabaseError as exc:
        sqlstate = getattr(exc, "sqlstate", None) or getattr(
            getattr(exc, "__cause__", None), "sqlstate", None
        )
        if sqlstate == "40001":
            raise PreconditionFailed() from exc
        if sqlstate == "42501":
            raise PermissionDenied(
                "MODULE_CHANGE_DENIED", "Permission or license policy denies this change."
            ) from exc
        raise Conflict("MODULE_CHANGE_CONFLICT", "The module change could not be applied.") from exc
