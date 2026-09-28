import datetime as dt
import uuid
from typing import Any, cast

from apps.payments.services import _json, _rows


def stock_list(
    company: uuid.UUID,
    resource: str,
    *,
    after: uuid.UUID | None,
    limit: int,
    as_of: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    tables = {
        "documents": "inventory_documents",
        "reservations": "inventory_reservations",
        "lots": "inventory_lots",
        "movements": "stock_movements",
    }
    if resource in tables:
        extra = " AND occurred_at<=%s" if resource == "movements" and as_of else ""
        params: list[Any] = [company, after, after]
        if extra:
            params.append(as_of)
        params.append(limit + 1)
        return cast(
            list[dict[str, Any]],
            _json(
                _rows(
                    f"SELECT * FROM erp.{tables[resource]} WHERE company_id=%s "
                    f"AND (%s::uuid IS NULL OR id>%s::uuid){extra} ORDER BY id LIMIT %s",
                    params,
                )
            ),
        )
    if as_of:
        # Historical reservations need an effective-date ledger; report only historical on-hand.
        return cast(
            list[dict[str, Any]],
            _json(
                _rows(
                    "SELECT min(id::text)::uuid id,warehouse_id,item_id,lot_id,"
                    "sum(quantity_delta) on_hand_quantity,"
                    "sum(value_delta_company) value_company FROM erp.stock_movements "
                    "WHERE company_id=%s AND occurred_at<=%s GROUP BY warehouse_id,item_id,lot_id "
                    "HAVING (%s::uuid IS NULL OR min(id::text)::uuid>%s::uuid) "
                    "ORDER BY min(id::text)::uuid LIMIT %s",
                    [company, as_of, after, after, limit + 1],
                )
            ),
        )
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT "
                "p.id,p.warehouse_id,p.item_id,p.lot_id,p.on_hand_quantity,p.reserved_q"
                "uantity,"
                "a.available_quantity,p.value_company FROM erp.inventory_positions p "
                "JOIN erp.v_inventory_availability a ON a.company_id=p.company_id "
                "AND a.warehouse_id=p.warehouse_id AND a.item_id=p.item_id "
                "AND a.lot_id IS NOT DISTINCT FROM p.lot_id WHERE p.company_id=%s "
                "AND (%s::uuid IS NULL OR p.id>%s::uuid) ORDER BY p.id LIMIT %s",
                [company, after, after, limit + 1],
            )
        ),
    )


def reconciliation(company: uuid.UUID) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                """
        WITH stock AS (SELECT warehouse_id,item_id,lot_id,sum(quantity_delta) quantity,
            sum(value_delta_company) value FROM erp.stock_movements WHERE company_id=%s
            GROUP BY warehouse_id,item_id,lot_id), reserved AS (
            SELECT warehouse_id,item_id,lot_id,sum(quantity) quantity
            FROM erp.inventory_reservations WHERE company_id=%s AND status='active'
            GROUP BY warehouse_id,item_id,lot_id), scopes AS (
            SELECT warehouse_id,item_id,lot_id FROM stock UNION
            SELECT warehouse_id,item_id,lot_id FROM reserved UNION
            SELECT warehouse_id,item_id,lot_id FROM erp.inventory_positions WHERE company_id=%s)
        SELECT x.*,coalesce(s.quantity,0) expected_quantity,coalesce(s.value,0) expected_value,
            coalesce(r.quantity,0) expected_reserved FROM scopes x
        LEFT JOIN stock s ON s.warehouse_id=x.warehouse_id AND s.item_id=x.item_id
            AND s.lot_id IS NOT DISTINCT FROM x.lot_id
        LEFT JOIN reserved r ON r.warehouse_id=x.warehouse_id AND r.item_id=x.item_id
            AND r.lot_id IS NOT DISTINCT FROM x.lot_id
        LEFT JOIN erp.inventory_positions p ON p.company_id=%s AND p.warehouse_id=x.warehouse_id
            AND p.item_id=x.item_id AND p.lot_id IS NOT DISTINCT FROM x.lot_id
        WHERE p.id IS NULL OR p.on_hand_quantity<>coalesce(s.quantity,0)
            OR p.reserved_quantity<>coalesce(r.quantity,0) OR p.value_company<>coalesce(s.value,0)
        ORDER BY x.warehouse_id,x.item_id,x.lot_id LIMIT 201
    """,
                [company] * 4,
            )
        ),
    )
