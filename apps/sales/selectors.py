import base64
import datetime as dt
import decimal
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


def _encode_cursor(created_at: str, invoice_id: str) -> str:
    raw = json.dumps([created_at, invoice_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return dt.datetime.fromisoformat(decoded[0]), uuid.UUID(decoded[1])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.") from exc


def sales_invoice_detail(company_id: uuid.UUID, invoice_id: uuid.UUID) -> dict[str, Any] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT i.id, i.invoice_no, i.document_kind, i.partner_id,
                   p.partner_code, p.display_name AS partner_name,
                   i.issue_date, i.tax_point_date, i.tax_jurisdiction_id,
                   j.jurisdiction_code, j.name AS tax_jurisdiction_name,
                   i.due_date, i.currency_code, i.exchange_rate, i.status,
                   i.row_version, i.calculated_at, i.journal_entry_id,
                   i.credit_of_invoice_id, i.posted_at, i.created_at, i.updated_at,
                   coalesce(t.net_total, 0)::numeric(20,6) AS net_total,
                   coalesce(t.tax_total, 0)::numeric(20,6) AS tax_total,
                   coalesce(t.gross_total, 0)::numeric(20,6) AS gross_total
            FROM erp.sales_invoices i
            LEFT JOIN erp.business_partners p
              ON p.company_id = i.company_id AND p.id = i.partner_id
            LEFT JOIN erp.tax_jurisdictions j ON j.id = i.tax_jurisdiction_id
            LEFT JOIN LATERAL (
                SELECT sum(net_amount) AS net_total, sum(tax_amount) AS tax_total,
                       sum(gross_amount) AS gross_total
                FROM erp.sales_invoice_lines
                WHERE company_id = i.company_id AND sales_invoice_id = i.id
            ) t ON true
            WHERE i.company_id = %s AND i.id = %s
            """,
            [company_id, invoice_id],
        )
        headers = _rows(cursor)
        if not headers:
            return None
        result = headers[0]
        cursor.execute(
            """
            SELECT id, address_kind, recipient_name, tax_registration_no,
                   line_1, line_2, city, region, postal_code, country_code
            FROM erp.sales_invoice_address_snapshots
            WHERE company_id = %s AND sales_invoice_id = %s
            ORDER BY address_kind
            """,
            [company_id, invoice_id],
        )
        result["addresses"] = _rows(cursor)
        cursor.execute(
            """
            SELECT l.id, l.line_no, l.item_id, i.sku AS item_sku,
                   l.line_account_id, a.code AS line_account_code,
                   l.description, l.quantity, l.unit_price, l.discount_amount,
                   l.net_amount, l.tax_code_id, tc.code AS tax_code,
                   l.credit_of_invoice_line_id,
                   l.revenue_account_id_snapshot,
                   l.inventory_account_id_snapshot,l.cogs_account_id_snapshot,
                   l.tax_amount, l.gross_amount
            FROM erp.sales_invoice_lines l
            LEFT JOIN erp.items i ON i.company_id = l.company_id AND i.id = l.item_id
            LEFT JOIN erp.accounts a
              ON a.company_id = l.company_id AND a.id = l.line_account_id
            LEFT JOIN erp.tax_codes tc
              ON tc.company_id = l.company_id AND tc.id = l.tax_code_id
            WHERE l.company_id = %s AND l.sales_invoice_id = %s
            ORDER BY l.line_no
            """,
            [company_id, invoice_id],
        )
        lines = _rows(cursor)
        cursor.execute(
            """
            SELECT c.id, c.sales_invoice_line_id, c.tax_code_id,
                   c.tax_code_component_id, c.tax_jurisdiction_id,
                   j.jurisdiction_code, j.name AS jurisdiction_name,
                   c.rate_schedule_id, rs.schedule_code,
                   c.tax_rate_version_id, c.rate_version_code_snapshot,
                   c.taxable_base_amount, c.rate_snapshot,
                   c.calculation_method_snapshot, c.calculation_base_snapshot,
                   c.fixed_amount_snapshot, c.tax_inclusive_snapshot,
                   c.tax_amount, c.recovery_percent_snapshot,
                   c.rounding_method_snapshot, c.rounding_precision_snapshot,
                   c.currency_code_snapshot, c.output_tax_account_id_snapshot,
                   a.code AS output_tax_account_code,
                   c.account_role_snapshot, c.polarity_snapshot
            FROM erp.sales_invoice_tax_components c
            JOIN erp.tax_jurisdictions j ON j.id = c.tax_jurisdiction_id
            JOIN erp.tax_rate_schedules rs
              ON rs.jurisdiction_id = c.tax_jurisdiction_id
             AND rs.id = c.rate_schedule_id
            LEFT JOIN erp.accounts a
              ON a.company_id = c.company_id
             AND a.id = c.output_tax_account_id_snapshot
            WHERE c.company_id = %s AND c.sales_invoice_id = %s
            ORDER BY c.sales_invoice_line_id, c.created_at, c.id
            """,
            [company_id, invoice_id],
        )
        by_line: dict[str, list[dict[str, Any]]] = {}
        for component in _rows(cursor):
            by_line.setdefault(cast(str, component["sales_invoice_line_id"]), []).append(component)
        for line in lines:
            line["tax_components"] = by_line.get(cast(str, line["id"]), [])
        result["lines"] = lines
        return result


def list_sales_invoices(
    company_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
    partner_id: uuid.UUID | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["i.company_id = %s"]
    params: list[Any] = [company_id]
    if status:
        where.append("i.status = %s")
        params.append(status)
    if partner_id:
        where.append("i.partner_id = %s")
        params.append(partner_id)
    if cursor:
        created_at, invoice_id = _decode_cursor(cursor)
        where.append("(i.created_at, i.id) < (%s, %s)")
        params.extend([created_at, invoice_id])
    params.append(limit + 1)
    with connection.cursor() as db_cursor:
        db_cursor.execute(
            f"""
            SELECT i.id, i.invoice_no, i.partner_id, p.partner_code,
                   p.display_name AS partner_name, i.issue_date, i.due_date,
                   i.currency_code, i.status, i.row_version, i.calculated_at,
                   i.created_at, i.updated_at,
                   coalesce(sum(l.net_amount),0)::numeric(20,6) AS net_total,
                   coalesce(sum(l.tax_amount),0)::numeric(20,6) AS tax_total,
                   coalesce(sum(l.gross_amount),0)::numeric(20,6) AS gross_total
            FROM erp.sales_invoices i
            LEFT JOIN erp.business_partners p
              ON p.company_id = i.company_id AND p.id = i.partner_id
            LEFT JOIN erp.sales_invoice_lines l
              ON l.company_id = i.company_id AND l.sales_invoice_id = i.id
            WHERE {" AND ".join(where)}
            GROUP BY i.id, p.partner_code, p.display_name
            ORDER BY i.created_at DESC, i.id DESC
            LIMIT %s
            """,
            params,
        )
        rows = _rows(db_cursor)
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        next_cursor = _encode_cursor(cast(str, page[-1]["created_at"]), cast(str, page[-1]["id"]))
    return page, next_cursor
