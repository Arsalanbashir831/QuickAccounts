import uuid
from typing import Any, cast

from apps.payments.services import _json, _rows
from common.api.errors import ScopeNotFound


def _public(row: dict[str, Any]) -> dict[str, Any]:
    row.pop("draft_edit_xid", None)
    row.pop("planning_edit_xid", None)
    row.pop("execution_edit_xid", None)
    row.pop("write_xid", None)
    return cast(dict[str, Any], _json(row))


def bom_detail(
    company: uuid.UUID, bom_id: uuid.UUID, *, for_update: bool = False
) -> dict[str, Any]:
    # Aggregate readers lock the parent so draft header/lines cannot tear across statements.
    lock = "UPDATE" if for_update else "SHARE"
    rows = _rows(
        f"SELECT * FROM erp.boms WHERE company_id=%s AND id=%s FOR {lock}", [company, bom_id]
    )
    if not rows:
        raise ScopeNotFound()
    result = rows[0]
    result["lines"] = _rows(
        "SELECT * FROM erp.bom_lines WHERE company_id=%s AND bom_id=%s ORDER BY line_no",
        [company, bom_id],
    )
    return _public(result)


def order_detail(
    company: uuid.UUID, order_id: uuid.UUID, *, for_update: bool = False
) -> dict[str, Any]:
    lock = "UPDATE" if for_update else "SHARE"
    rows = _rows(
        f"SELECT * FROM erp.production_orders WHERE company_id=%s AND id=%s FOR {lock}",
        [company, order_id],
    )
    if not rows:
        raise ScopeNotFound()
    result = rows[0]
    result["requirements"] = _rows(
        "SELECT * FROM erp.production_order_requirements "
        "WHERE company_id=%s AND production_order_id=%s ORDER BY line_no",
        [company, order_id],
    )
    result["availability_is_reserved"] = False
    for name, table in (
        ("reservations", "inventory_reservations"),
        ("material_issues", "production_material_issues"),
        ("outputs", "production_outputs"),
        ("material_returns", "production_material_returns"),
        ("execution_batches", "production_execution_batches"),
    ):
        if name == "reservations":
            rows = _rows(
                "SELECT r.* FROM erp.inventory_reservations r JOIN "
                "erp.production_order_requirements q "
                "ON q.company_id=r.company_id AND q.id=r.source_id WHERE q.company_id=%s "
                "AND q.production_order_id=%s AND r.source_type='production_requirement' "
                "ORDER BY r.id LIMIT 1001",
                [company, order_id],
            )
        else:
            rows = _rows(
                f"SELECT * FROM erp.{table} WHERE company_id=%s "
                "AND production_order_id=%s ORDER BY id LIMIT 1001",
                [company, order_id],
            )
        if len(rows) > 1000:
            from common.api.errors import Conflict

            raise Conflict(
                "MFG_HISTORY_JOB_REQUIRED", "Production history exceeds the interactive limit."
            )
        result[name] = [_public(row) for row in rows]
    result["availability_is_reserved"] = result["status"] in {"released", "in_progress"}
    return _public(result)


def list_documents(
    company: uuid.UUID,
    resource: str,
    *,
    after: uuid.UUID | None,
    limit: int,
    status: str | None,
    output_item_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    table = {"boms": "boms", "orders": "production_orders"}[resource]
    rows = _rows(
        f"SELECT * FROM erp.{table} WHERE company_id=%s "
        "AND (%s::uuid IS NULL OR id>%s::uuid) AND (%s::text IS NULL OR status=%s) "
        "AND (%s::uuid IS NULL OR output_item_id=%s) ORDER BY id LIMIT %s",
        [company, after, after, status, status, output_item_id, output_item_id, limit + 1],
    )
    return [_public(row) for row in rows]
