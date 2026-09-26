import uuid
from typing import Any, cast

from django.db import IntegrityError, connection, transaction

from apps.inventory.selectors import item_accounting_profile_detail, item_detail
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import APIError, Conflict, PreconditionFailed, ScopeNotFound

ACCOUNT_TYPES_BY_PROFILE_FIELD = {
    "inventory_account_id": {"asset"},
    "revenue_account_id": {"revenue"},
    "cogs_account_id": {"cost_of_sales", "expense"},
    "purchase_account_id": {"asset", "cost_of_sales", "expense"},
}


def _constraint_name(exc: IntegrityError) -> str | None:
    diagnostic = getattr(getattr(exc, "__cause__", None), "diag", None)
    return cast(str | None, getattr(diagnostic, "constraint_name", None))


def _set_request_id(request_id: str | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.request_id', %s, true)", [request_id or ""])


def _validate_tracking(item_kind: str, track_lots: bool, track_serials: bool) -> None:
    if track_lots and track_serials:
        raise APIError(
            code="INVALID_ITEM_TRACKING",
            message="An item cannot track lots and serial numbers at the same time.",
        )
    if item_kind != "stock" and (track_lots or track_serials):
        raise APIError(
            code="INVALID_ITEM_TRACKING",
            message="Only stock items can track lots or serial numbers.",
        )


def _validate_uom(uom_id: uuid.UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM erp.uoms WHERE id = %s", [uom_id])
        if cursor.fetchone() is None:
            raise APIError(
                code="INVALID_UOM", message="The selected unit of measure is unavailable."
            )


def _item_write_conflict(exc: IntegrityError) -> Conflict:
    constraint = _constraint_name(exc)
    if constraint == "items_company_id_sku_key":
        return Conflict(
            "ITEM_SKU_EXISTS",
            "An item with this SKU already exists in the company.",
            {"field": "sku"},
        )
    return Conflict(
        "ITEM_WRITE_CONFLICT",
        "The item could not be saved because it conflicts with existing data.",
        {"constraint": constraint},
    )


def create_item(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    _validate_tracking(data["item_kind"], data["track_lots"], data["track_serials"])
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "core", "item.manage")
            _validate_uom(data["base_uom_id"])
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.items(
                        company_id, sku, name, item_kind, base_uom_id,
                        track_lots, track_serials, is_active
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        data["sku"],
                        data["name"],
                        data["item_kind"],
                        data["base_uom_id"],
                        data["track_lots"],
                        data["track_serials"],
                        data["is_active"],
                    ],
                )
                item_id = cast(uuid.UUID, cursor.fetchone()[0])
            result = item_detail(scope.company_id, item_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _item_write_conflict(exc) from exc


def update_item(
    scope: CompanyScope,
    item_id: uuid.UUID,
    data: dict[str, Any],
    *,
    expected_revision: int,
    request_id: str | None,
) -> dict[str, Any]:
    allowed = {
        "sku",
        "name",
        "item_kind",
        "base_uom_id",
        "track_lots",
        "track_serials",
        "is_active",
    }
    fields = [field for field in data if field in allowed]
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "core", "item.manage")
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT item_kind, base_uom_id, track_lots, track_serials, row_version
                    FROM erp.items
                    WHERE company_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    [scope.company_id, item_id],
                )
                current = cursor.fetchone()
                if current is None:
                    raise ScopeNotFound()
                if int(current[4]) != expected_revision:
                    raise PreconditionFailed(int(current[4]))
                item_kind = data.get("item_kind", current[0])
                base_uom_id = data.get("base_uom_id", current[1])
                track_lots = data.get("track_lots", current[2])
                track_serials = data.get("track_serials", current[3])
                _validate_tracking(item_kind, track_lots, track_serials)
                _validate_uom(base_uom_id)
                _set_request_id(request_id)
                assignments = ", ".join(f"{field} = %s" for field in fields)
                cursor.execute(
                    f"""
                    UPDATE erp.items SET {assignments}
                    WHERE company_id = %s AND id = %s
                    RETURNING row_version
                    """,
                    [*(data[field] for field in fields), scope.company_id, item_id],
                )
            result = item_detail(scope.company_id, item_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise _item_write_conflict(exc) from exc


def _lock_item(company_id: uuid.UUID, item_id: uuid.UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1 FROM erp.items
            WHERE company_id = %s AND id = %s
            FOR KEY SHARE
            """,
            [company_id, item_id],
        )
        if cursor.fetchone() is None:
            raise ScopeNotFound()


def _validate_profile_accounts(company_id: uuid.UUID, data: dict[str, Any]) -> None:
    requested = {
        field: account_id
        for field, account_id in data.items()
        if field in ACCOUNT_TYPES_BY_PROFILE_FIELD and account_id is not None
    }
    if not requested:
        return
    placeholders = ",".join(["%s"] * len(requested))
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, account_type, is_active, allow_posting
            FROM erp.accounts
            WHERE company_id = %s AND id IN ({placeholders})
            """,
            [company_id, *requested.values()],
        )
        accounts = {row[0]: row[1:] for row in cursor.fetchall()}
    for field, account_id in requested.items():
        account = accounts.get(account_id)
        if (
            account is None
            or account[0] not in ACCOUNT_TYPES_BY_PROFILE_FIELD[field]
            or not account[1]
            or not account[2]
        ):
            raise APIError(
                code="INVALID_ITEM_ACCOUNT_MAPPING",
                message="An account is unavailable or incompatible with its item profile role.",
                details={"field": field},
            )


def put_item_accounting_profile(
    scope: CompanyScope,
    item_id: uuid.UUID,
    data: dict[str, Any],
    *,
    request_id: str | None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "accounting", "accounting.setup.manage")
            _lock_item(scope.company_id, item_id)
            _validate_profile_accounts(scope.company_id, data)
            _set_request_id(request_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO erp.item_accounting_profiles(
                        company_id, item_id, inventory_account_id, revenue_account_id,
                        cogs_account_id, purchase_account_id
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (company_id, item_id) DO UPDATE SET
                        inventory_account_id = EXCLUDED.inventory_account_id,
                        revenue_account_id = EXCLUDED.revenue_account_id,
                        cogs_account_id = EXCLUDED.cogs_account_id,
                        purchase_account_id = EXCLUDED.purchase_account_id
                    RETURNING id
                    """,
                    [
                        scope.company_id,
                        item_id,
                        data["inventory_account_id"],
                        data["revenue_account_id"],
                        data["cogs_account_id"],
                        data["purchase_account_id"],
                    ],
                )
            result = item_accounting_profile_detail(scope.company_id, item_id)
            assert result is not None
            return result
    except IntegrityError as exc:
        raise Conflict(
            "ITEM_ACCOUNTING_PROFILE_CONFLICT",
            "The item accounting profile conflicts with existing data.",
            {"constraint": _constraint_name(exc)},
        ) from exc
