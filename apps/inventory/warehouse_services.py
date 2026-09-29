import uuid
from typing import Any

from django.db import IntegrityError, connection, transaction

from apps.inventory.warehouse_selectors import warehouse_detail
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict, PreconditionFailed, ScopeNotFound

FIELDS = ("code", "name", "address_text", "stock_category", "is_active", "operational_role")


def save_warehouse(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    warehouse_id: uuid.UUID | None = None,
    expected_revision: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    try:
        with transaction.atomic(durable=True):
            bind_and_verify_company(scope)
            assert_company_write(scope.company_id, "inventory", "inventory.manage")
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.request_id',%s,true)", [request_id or ""])
                if warehouse_id is None:
                    cursor.execute(
                        "INSERT INTO erp.warehouses(company_id,code,name,address_text,"
                        "stock_category,is_active,operational_role) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        [scope.company_id, *(data.get(field) for field in FIELDS)],
                    )
                    warehouse_id = cursor.fetchone()[0]
                else:
                    cursor.execute(
                        "SELECT row_version FROM erp.warehouses "
                        "WHERE company_id=%s AND id=%s FOR UPDATE",
                        [scope.company_id, warehouse_id],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ScopeNotFound()
                    if row[0] != expected_revision:
                        raise PreconditionFailed(row[0])
                    fields = [field for field in FIELDS if field in data]
                    if not fields:
                        raise Conflict("EMPTY_WAREHOUSE_UPDATE", "No warehouse fields supplied.")
                    assignments = ",".join(f"{field}=%s" for field in fields)
                    cursor.execute(
                        f"UPDATE erp.warehouses SET {assignments} WHERE company_id=%s AND id=%s",
                        [*(data[field] for field in fields), scope.company_id, warehouse_id],
                    )
            assert warehouse_id is not None
            return warehouse_detail(scope.company_id, warehouse_id)
    except IntegrityError as exc:
        diagnostic = getattr(getattr(exc, "__cause__", None), "diag", None)
        constraint = str(getattr(diagnostic, "constraint_name", ""))
        code = {
            "warehouses_company_id_code_key": "WAREHOUSE_CODE_EXISTS",
            "warehouse_category_in_use": "WAREHOUSE_CATEGORY_IN_USE",
            "warehouse_balance_in_use": "WAREHOUSE_BALANCE_IN_USE",
        }.get(constraint, "WAREHOUSE_WRITE_CONFLICT")
        raise Conflict(
            code, "Warehouse change conflicts with existing stock or configuration."
        ) from exc
