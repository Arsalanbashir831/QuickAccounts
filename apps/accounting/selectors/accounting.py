import base64
import datetime as dt
import decimal
import json
import uuid
from typing import Any

from django.db import connection

from common.api.errors import APIError


def _json_value(value: Any) -> Any:
    if isinstance(value, (uuid.UUID, dt.date, dt.datetime, decimal.Decimal)):
        return str(value)
    return value


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column.name for column in cursor.description]
    return [
        {name: _json_value(value) for name, value in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


def fetch_all(sql: str, params: list[object]) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return _rows(cursor)


def fetch_one(sql: str, params: list[object]) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


CONFIG_QUERIES = {
    "accounts": """
        SELECT id, parent_account_id, code, name, account_type, normal_balance,
               is_control_account, allow_posting, is_active, source_template_code,
               source_template_account_code, created_at, updated_at
        FROM erp.accounts WHERE company_id = %s ORDER BY code, id LIMIT %s
    """,
    "journals": """
        SELECT id, code, name, journal_type, is_active, created_at, updated_at
        FROM erp.journals WHERE company_id = %s ORDER BY code, id LIMIT %s
    """,
    "periods": """
        SELECT id, code, starts_on, ends_on, state, row_version, created_at
        FROM erp.fiscal_periods WHERE company_id = %s ORDER BY starts_on DESC, id DESC LIMIT %s
    """,
    "dimension-types": """
        SELECT id, code, name, is_required, is_active
        FROM erp.dimension_types WHERE company_id = %s ORDER BY code, id LIMIT %s
    """,
    "dimension-values": """
        SELECT id, dimension_type_id, code, name, is_active
        FROM erp.dimension_values WHERE company_id = %s
        ORDER BY dimension_type_id, code, id LIMIT %s
    """,
    "posting-rules": """
        SELECT id, event_code, role_code, account_id, created_at, updated_at
        FROM erp.accounting_posting_rules WHERE company_id = %s
        ORDER BY event_code, role_code, id LIMIT %s
    """,
}


def list_config(company_id: uuid.UUID, resource: str, limit: int) -> list[dict[str, Any]]:
    return fetch_all(CONFIG_QUERIES[resource], [company_id, limit])


def account_detail(company_id: uuid.UUID, account_id: uuid.UUID) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT id, parent_account_id, code, name, account_type, normal_balance,
               is_control_account, allow_posting, is_active, source_template_code,
               source_template_account_code, created_at, updated_at
        FROM erp.accounts WHERE company_id = %s AND id = %s
        """,
        [company_id, account_id],
    )


BUSINESS_TYPES_BY_PROFILE = {
    "retail_wholesale": ["retail", "wholesale"],
    "ecommerce": ["ecommerce"],
    "manufacturing": ["manufacturing"],
}


def list_chart_templates(company_id: uuid.UUID) -> dict[str, Any]:
    company = fetch_one(
        """
        SELECT business_type, chart_template_code, chart_template_applied_at
        FROM erp.companies WHERE id = %s
        """,
        [company_id],
    )
    templates = fetch_all(
        """
        SELECT t.code, t.name, t.business_profile, t.version, t.is_active,
               count(a.code) AS account_count
        FROM erp.chart_of_account_templates t
        LEFT JOIN erp.chart_of_account_template_accounts a ON a.template_code = t.code
        WHERE t.is_active
        GROUP BY t.code, t.name, t.business_profile, t.version, t.is_active
        ORDER BY t.business_profile, t.version DESC
        """,
        [],
    )
    for template in templates:
        template["compatible_business_types"] = BUSINESS_TYPES_BY_PROFILE[
            template["business_profile"]
        ]
    return {"company": company, "results": templates}


def chart_template_detail(template_code: str) -> dict[str, Any] | None:
    template = fetch_one(
        """
        SELECT code, name, business_profile, version, is_active, created_at
        FROM erp.chart_of_account_templates WHERE code = %s
        """,
        [template_code],
    )
    if template is None:
        return None
    template["compatible_business_types"] = BUSINESS_TYPES_BY_PROFILE[
        template["business_profile"]
    ]
    template["accounts"] = fetch_all(
        """
        SELECT code, parent_code, name, account_type, normal_balance,
               is_control_account, allow_posting, sort_order
        FROM erp.chart_of_account_template_accounts
        WHERE template_code = %s ORDER BY sort_order, code
        """,
        [template_code],
    )
    return template


def period_detail(company_id: uuid.UUID, period_id: uuid.UUID) -> dict[str, Any] | None:
    return fetch_one(
        """
        SELECT id, code, starts_on, ends_on, state, row_version, created_at
        FROM erp.fiscal_periods WHERE company_id = %s AND id = %s
        """,
        [company_id, period_id],
    )


def encode_cursor(entry_date: str, entry_id: str) -> str:
    payload = json.dumps([entry_date, entry_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        date_value, id_value = json.loads(base64.urlsafe_b64decode(padded))
        dt.date.fromisoformat(date_value)
        uuid.UUID(id_value)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise APIError(code="INVALID_CURSOR", message="The pagination cursor is invalid.") from exc
    return date_value, id_value


def list_entries(
    company_id: uuid.UUID,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = ["e.company_id = %s"]
    params: list[object] = [company_id]
    if status:
        where.append("e.status = %s")
        params.append(status)
    if cursor:
        entry_date, entry_id = decode_cursor(cursor)
        where.append("(e.entry_date, e.id) < (%s, %s)")
        params.extend([entry_date, entry_id])
    params.append(limit + 1)
    rows = fetch_all(
        f"""
        SELECT e.id, e.journal_id, e.fiscal_period_id, e.entry_number,
               e.entry_date, e.status, e.description, e.source_type, e.source_id,
               e.reversal_of_entry_id, e.row_version, e.created_at, e.posted_at
        FROM erp.journal_entries e
        WHERE {" AND ".join(where)}
        ORDER BY e.entry_date DESC, e.id DESC LIMIT %s
        """,
        params,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = encode_cursor(page[-1]["entry_date"], page[-1]["id"])
    return page, next_cursor


def entry_detail(company_id: uuid.UUID, entry_id: uuid.UUID) -> dict[str, Any] | None:
    entry = fetch_one(
        """
        SELECT id, journal_id, fiscal_period_id, entry_number, entry_date, status,
               description, source_type, source_id, reversal_of_entry_id,
               row_version, created_at, posted_at
        FROM erp.journal_entries WHERE company_id = %s AND id = %s
        """,
        [company_id, entry_id],
    )
    if entry is None:
        return None
    lines = fetch_all(
        """
        SELECT id, line_no, account_id, business_partner_id, sales_channel_id,
               description, transaction_currency, exchange_rate,
               transaction_debit, transaction_credit, debit_amount, credit_amount
        FROM erp.journal_lines
        WHERE company_id = %s AND journal_entry_id = %s ORDER BY line_no
        """,
        [company_id, entry_id],
    )
    dimensions = fetch_all(
        """
        SELECT d.journal_line_id,d.dimension_type_id,d.dimension_value_id
        FROM erp.journal_line_dimensions d
        JOIN erp.journal_lines l
          ON l.company_id=d.company_id AND l.id=d.journal_line_id
        WHERE d.company_id=%s AND l.journal_entry_id=%s
        ORDER BY d.journal_line_id,d.dimension_type_id
        """,
        [company_id, entry_id],
    )
    by_line: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        line_id = dimension.pop("journal_line_id")
        by_line.setdefault(line_id, []).append(dimension)
    for line in lines:
        line["dimensions"] = by_line.get(line["id"], [])
    entry["lines"] = lines
    return entry


def ledger(
    company_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None,
    date_from: dt.date | None,
    date_to: dt.date | None,
    limit: int,
) -> list[dict[str, Any]]:
    where = ["e.company_id = %s", "e.status = 'posted'"]
    params: list[object] = [company_id]
    if account_id:
        where.append("l.account_id = %s")
        params.append(account_id)
    if date_from:
        where.append("e.entry_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("e.entry_date <= %s")
        params.append(date_to)
    params.append(limit)
    return fetch_all(
        f"""
        SELECT e.id AS entry_id, e.entry_number, e.entry_date, e.description AS entry_description,
               l.id AS line_id, l.line_no, l.account_id, a.code AS account_code,
               a.name AS account_name, l.description, l.transaction_currency,
               l.exchange_rate, l.transaction_debit, l.transaction_credit,
               l.debit_amount, l.credit_amount
        FROM erp.journal_entries e
        JOIN erp.journal_lines l
          ON l.company_id = e.company_id AND l.journal_entry_id = e.id
        JOIN erp.accounts a ON a.company_id = l.company_id AND a.id = l.account_id
        WHERE {" AND ".join(where)}
        ORDER BY e.entry_date DESC, e.id DESC, l.line_no LIMIT %s
        """,
        params,
    )
