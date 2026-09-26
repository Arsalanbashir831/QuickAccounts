import base64
import datetime as dt
import decimal
import hashlib
import json
import uuid
from typing import Any, cast

from django.db import connection

from common.api.errors import APIError


def _json_value(value: object) -> object:
    if isinstance(value, (uuid.UUID, dt.date, dt.datetime, decimal.Decimal)):
        return str(value)
    return value


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [
        {name: _json_value(value) for name, value in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


def _fingerprint(filters: dict[str, object]) -> str:
    payload = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _encode_cursor(values: list[str]) -> str:
    payload = json.dumps(values, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str, *, size: int) -> list[str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != size
        or not all(isinstance(item, str) for item in decoded)
    ):
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.")
    return cast(list[str], decoded)


def item_detail(company_id: uuid.UUID, item_id: uuid.UUID) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT i.id, i.sku, i.name, i.item_kind, i.base_uom_id,
                   u.code AS base_uom_code, u.name AS base_uom_name,
                   c.code AS uom_category_code, c.name AS uom_category_name,
                   i.track_lots, i.track_serials, i.is_active, i.row_version,
                   i.created_at, i.updated_at
            FROM erp.items i
            JOIN erp.uoms u ON u.id = i.base_uom_id
            JOIN erp.uom_categories c ON c.id = u.category_id
            WHERE i.company_id = %s AND i.id = %s
            """,
            [company_id, item_id],
        )
        rows = _rows(cursor)
    return rows[0] if rows else None


def list_items(
    company_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    q: str | None,
    kind: str | None,
    is_active: bool | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["i.company_id = %s"]
    params: list[object] = [company_id]
    filters = {"is_active": is_active, "item_kind": kind, "q": q}
    if q:
        where.append(
            "(starts_with(lower(i.sku), lower(%s)) OR starts_with(lower(i.name), lower(%s)))"
        )
        params.extend([q, q])
    if kind:
        where.append("i.item_kind = %s")
        params.append(kind)
    if is_active is not None:
        where.append("i.is_active = %s")
        params.append(is_active)
    if cursor:
        sku, item_id, fingerprint = _decode_cursor(cursor, size=3)
        try:
            parsed_id = uuid.UUID(item_id)
        except ValueError as exc:
            raise APIError(
                code="INVALID_CURSOR", message="The pagination cursor is invalid."
            ) from exc
        if fingerprint != _fingerprint(filters):
            raise APIError(
                code="INVALID_CURSOR",
                message="The pagination cursor does not match the active filters.",
            )
        where.append("(i.sku, i.id) > (%s, %s)")
        params.extend([sku, parsed_id])
    params.append(limit + 1)
    with connection.cursor() as db_cursor:
        db_cursor.execute(
            f"""
            SELECT i.id, i.sku, i.name, i.item_kind, i.base_uom_id,
                   u.code AS base_uom_code, u.name AS base_uom_name,
                   c.code AS uom_category_code, c.name AS uom_category_name,
                   i.track_lots, i.track_serials, i.is_active, i.row_version,
                   i.created_at, i.updated_at
            FROM erp.items i
            JOIN erp.uoms u ON u.id = i.base_uom_id
            JOIN erp.uom_categories c ON c.id = u.category_id
            WHERE {" AND ".join(where)}
            ORDER BY i.sku, i.id
            LIMIT %s
            """,
            params,
        )
        rows = _rows(db_cursor)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        next_cursor = _encode_cursor(
            [
                cast(str, page[-1]["sku"]),
                cast(str, page[-1]["id"]),
                _fingerprint(filters),
            ]
        )
    return page, next_cursor


def list_uoms(*, limit: int, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    params: list[object] = []
    where = ""
    if cursor:
        code, uom_id = _decode_cursor(cursor, size=2)
        try:
            parsed_id = uuid.UUID(uom_id)
        except ValueError as exc:
            raise APIError(
                code="INVALID_CURSOR", message="The pagination cursor is invalid."
            ) from exc
        where = "WHERE (u.code, u.id) > (%s, %s)"
        params.extend([code, parsed_id])
    params.append(limit + 1)
    with connection.cursor() as db_cursor:
        db_cursor.execute(
            f"""
            SELECT u.id, u.code, u.name, u.to_base_factor,
                   c.id AS category_id, c.code AS category_code, c.name AS category_name
            FROM erp.uoms u
            JOIN erp.uom_categories c ON c.id = u.category_id
            {where}
            ORDER BY u.code, u.id
            LIMIT %s
            """,
            params,
        )
        rows = _rows(db_cursor)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        next_cursor = _encode_cursor([cast(str, page[-1]["code"]), cast(str, page[-1]["id"])])
    return page, next_cursor


def item_accounting_profile_detail(
    company_id: uuid.UUID, item_id: uuid.UUID
) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.id, p.item_id,
                   p.inventory_account_id, inventory.code AS inventory_account_code,
                   inventory.name AS inventory_account_name,
                   p.revenue_account_id, revenue.code AS revenue_account_code,
                   revenue.name AS revenue_account_name,
                   p.cogs_account_id, cogs.code AS cogs_account_code,
                   cogs.name AS cogs_account_name,
                   p.purchase_account_id, purchase.code AS purchase_account_code,
                   purchase.name AS purchase_account_name,
                   p.created_at, p.updated_at
            FROM erp.item_accounting_profiles p
            LEFT JOIN erp.accounts inventory
              ON inventory.company_id = p.company_id
             AND inventory.id = p.inventory_account_id
            LEFT JOIN erp.accounts revenue
              ON revenue.company_id = p.company_id
             AND revenue.id = p.revenue_account_id
            LEFT JOIN erp.accounts cogs
              ON cogs.company_id = p.company_id AND cogs.id = p.cogs_account_id
            LEFT JOIN erp.accounts purchase
              ON purchase.company_id = p.company_id
             AND purchase.id = p.purchase_account_id
            WHERE p.company_id = %s AND p.item_id = %s
            """,
            [company_id, item_id],
        )
        rows = _rows(cursor)
    return rows[0] if rows else None
