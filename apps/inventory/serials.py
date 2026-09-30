import uuid
from typing import Any, cast

from apps.payments.services import _json, _rows
from common.api.errors import APIError, ScopeNotFound


def normalize_serial(value: str) -> str:
    normalized = value.strip().upper()
    if not 1 <= len(normalized) <= 100:
        raise APIError(code="INVALID_SERIAL", message="Serial must contain 1–100 characters.")
    return normalized


def serial_detail(company_id: uuid.UUID, serial_id: uuid.UUID) -> dict[str, Any]:
    rows = _rows(
        "SELECT s.id,s.item_id,s.lot_id,s.serial_number,s.normalized_serial,"
        "s.current_warehouse_id,s.lifecycle_status,s.first_receipt_movement_id,"
        "s.last_sales_invoice_line_id,s.row_version,s.created_at,s.updated_at,"
        "i.sku,i.name AS item_name,first_receipt.source_type AS receipt_source_type,"
        "first_receipt.source_id AS receipt_source_id,"
        "first_receipt.source_line_id AS receipt_source_line_id "
        "FROM erp.inventory_serials s "
        "JOIN erp.items i ON i.company_id=s.company_id AND i.id=s.item_id "
        "LEFT JOIN erp.stock_movements first_receipt ON "
        "first_receipt.company_id=s.company_id AND first_receipt.id=s.first_receipt_movement_id "
        "WHERE s.company_id=%s AND s.id=%s",
        [company_id, serial_id],
    )
    if not rows:
        raise ScopeNotFound()
    return cast(dict[str, Any], _json(rows[0]))


def list_serials(
    company_id: uuid.UUID,
    *,
    serial: str | None = None,
    item_id: uuid.UUID | None = None,
    warehouse_id: uuid.UUID | None = None,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    source_line_id: uuid.UUID | None = None,
    after: uuid.UUID | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise APIError(code="INVALID_LIMIT", message="Limit must be between 1 and 200.")
    if source_type and source_type not in {
        "inventory_document",
        "sales_invoice",
        "sales_return",
        "purchase_bill",
        "sales_return_replacement",
        "return_repair_job",
        "inventory_reservation",
        "production_execution",
        "return_stock_action",
    }:
        raise APIError(code="INVALID_FILTER", message="Unsupported source type.")
    if bool(source_type) != bool(source_id) or (source_line_id and not source_id):
        raise APIError(code="INVALID_FILTER", message="Source type and ID must be paired.")
    if not any((serial, item_id, warehouse_id, source_id)):
        raise APIError(code="FILTER_REQUIRED", message="Supply a serial or scoped document filter.")
    clauses = ["s.company_id=%s", "(%s::uuid IS NULL OR s.id>%s::uuid)"]
    params: list[Any] = [company_id, after, after]
    if serial is not None:
        clauses.append("s.normalized_serial=%s")
        params.append(normalize_serial(serial))
    if item_id:
        clauses.append("s.item_id=%s")
        params.append(item_id)
    if warehouse_id:
        clauses.append("s.current_warehouse_id=%s AND s.lifecycle_status='in_stock'")
        params.append(warehouse_id)
    if source_id:
        clauses.append(
            "EXISTS(SELECT 1 FROM erp.serial_movements m WHERE m.company_id=s.company_id "
            "AND m.serial_id=s.id AND m.source_type=%s AND m.source_id=%s "
            "AND (%s::uuid IS NULL OR m.source_line_id=%s::uuid))"
        )
        params.extend([source_type, source_id, source_line_id, source_line_id])
    rows = _rows(
        "SELECT s.id,s.item_id,s.serial_number,s.normalized_serial,s.current_warehouse_id,"
        "s.lifecycle_status,s.last_sales_invoice_line_id FROM erp.inventory_serials s WHERE "
        + " AND ".join(clauses)
        + " ORDER BY s.id LIMIT %s",
        params + [limit + 1],
    )
    next_cursor = str(rows[limit - 1]["id"]) if len(rows) > limit else None
    return {"results": _json(rows[:limit]), "next_cursor": next_cursor}


def serial_history(
    company_id: uuid.UUID, serial_id: uuid.UUID, after: uuid.UUID | None = None, limit: int = 50
) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise APIError(code="INVALID_LIMIT", message="Limit must be between 1 and 200.")
    serial_detail(company_id, serial_id)
    rows = _rows(
        "SELECT id,stock_movement_id,movement_kind,warehouse_id,quantity_delta,"
        "source_type,source_id,source_line_id,occurred_at,posted_at,actor_user_id "
        "FROM erp.serial_movements WHERE company_id=%s AND serial_id=%s "
        "AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",
        [company_id, serial_id, after, after, limit + 1],
    )
    next_cursor = str(rows[limit - 1]["id"]) if len(rows) > limit else None
    return {"results": _json(rows[:limit]), "next_cursor": next_cursor}
