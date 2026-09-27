import uuid
from typing import Any

from django.db import connection

from apps.inventory.selectors import _rows
from common.api.errors import ScopeNotFound


def warehouse_detail(company_id: uuid.UUID, warehouse_id: uuid.UUID) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,code,name,address_text,stock_category,is_active,row_version,"
            "created_at,updated_at FROM erp.warehouses WHERE company_id=%s AND id=%s",
            [company_id, warehouse_id],
        )
        rows = _rows(cursor)
    if not rows:
        raise ScopeNotFound()
    return rows[0]


def list_warehouses(
    company_id: uuid.UUID, *, after_id: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,code,name,address_text,stock_category,is_active,row_version "
            "FROM erp.warehouses WHERE company_id=%s "
            "AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s",
            [company_id, after_id, after_id, limit + 1],
        )
        return _rows(cursor)


def warehouse_balances(
    company_id: uuid.UUID, warehouse_id: uuid.UUID, *, after_id: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.id,p.item_id,i.sku,i.name,p.lot_id,p.on_hand_quantity,"
            "p.reserved_quantity,a.available_quantity,p.value_company,p.updated_at,"
            "c.functional_currency AS currency_code "
            "FROM erp.inventory_positions p JOIN erp.items i "
            "ON i.company_id=p.company_id AND i.id=p.item_id "
            "JOIN erp.companies c ON c.id=p.company_id "
            "JOIN erp.v_inventory_availability a ON a.company_id=p.company_id "
            "AND a.warehouse_id=p.warehouse_id AND a.item_id=p.item_id "
            "AND a.lot_id IS NOT DISTINCT FROM p.lot_id "
            "WHERE p.company_id=%s AND p.warehouse_id=%s "
            "AND (%s::uuid IS NULL OR p.id>%s::uuid) ORDER BY p.id LIMIT %s",
            [company_id, warehouse_id, after_id, after_id, limit + 1],
        )
        return _rows(cursor)
