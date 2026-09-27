import datetime as dt
import uuid
from typing import Any, cast

from apps.payments.services import _json, _rows, _tables


def open_items(
    company_id: uuid.UUID,
    direction: str,
    *,
    cutoff: dt.date,
    limit: int = 50,
    after: uuid.UUID | None = None,
    aging: bool = False,
) -> list[dict[str, Any]]:
    table, documents, lines, column = _tables(direction)
    date_column = "issue_date" if direction == "receipt" else "bill_date"
    partner = "partner_id" if direction == "receipt" else "supplier_id"
    number = "invoice_no" if direction == "receipt" else "bill_no"
    # posted_at defines whether a fact existed by the cutoff; effective dates define its period.
    kind = "invoice" if direction == "receipt" else "bill"
    rows = _rows(
        "WITH document_items AS ("
        f"SELECT d.id,d.{number} AS document_no,d.{partner} AS partner_id,d.currency_code,"
        f"d.{date_column} AS document_date,d.due_date,d.document_kind AS item_kind,t.gross_total,"
        "coalesce(a.applied,0) AS applied_amount,"
        "t.gross_total-coalesce(a.applied,0) AS open_amount "
        f"FROM erp.{documents} d JOIN LATERAL(SELECT sum(gross_amount)*"
        f"CASE WHEN d.document_kind='{kind}' THEN 1 ELSE -1 END AS gross_total "
        f"FROM erp.{lines} WHERE company_id=d.company_id AND {column}=d.id) t ON true "
        "LEFT JOIN LATERAL("
        f"SELECT sum(x.applied_document_amount) AS applied FROM erp.{table} x "
        "JOIN erp.payments p ON p.company_id=x.company_id AND p.id=x.payment_id "
        f"WHERE x.company_id=d.company_id AND x.{column}=d.id AND p.status='posted' "
        "AND p.posted_at<((%s::date+1)::timestamp AT TIME ZONE 'UTC') AND p.payment_date<=%s "
        "AND NOT EXISTS(SELECT 1 FROM erp.payments r WHERE r.company_id=p.company_id "
        "AND r.reversal_of_payment_id=p.id AND r.status='posted' "
        "AND r.posted_at<((%s::date+1)::timestamp AT TIME ZONE 'UTC') "
        "AND r.payment_date<=%s)) a ON true "
        "WHERE d.company_id=%s AND d.status='posted' "
        f"AND d.{date_column}<=%s AND d.posted_at<((%s::date+1)::timestamp AT TIME ZONE 'UTC') "
        "),unapplied_items AS ("
        "SELECT p.id,p.payment_no,p.partner_id,p.currency_code,p.payment_date,"
        "NULL::date AS due_date,'unallocated_payment'::text AS item_kind,"
        "-(p.amount+p.withholding_total-coalesce(a.applied,0)) AS gross_total,"
        "0::numeric AS applied_amount,"
        "-(p.amount+p.withholding_total-coalesce(a.applied,0)) AS open_amount "
        "FROM erp.payments p LEFT JOIN LATERAL("
        f"SELECT sum(applied_payment_amount) AS applied FROM erp.{table} "
        "WHERE company_id=p.company_id AND payment_id=p.id) a ON true "
        "WHERE p.company_id=%s AND p.direction=%s AND p.status='posted' "
        "AND p.reversal_of_payment_id IS NULL AND p.payment_date<=%s "
        "AND p.posted_at<((%s::date+1)::timestamp AT TIME ZONE 'UTC') "
        "AND NOT EXISTS(SELECT 1 FROM erp.payments r WHERE r.company_id=p.company_id "
        "AND r.reversal_of_payment_id=p.id AND r.status='posted' AND r.payment_date<=%s "
        "AND r.posted_at<((%s::date+1)::timestamp AT TIME ZONE 'UTC'))"
        ") SELECT * FROM (SELECT * FROM document_items "
        "UNION ALL SELECT * FROM unapplied_items) items "
        "WHERE open_amount<>0 AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
        [
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            company_id,
            cutoff,
            cutoff,
            company_id,
            direction,
            cutoff,
            cutoff,
            cutoff,
            cutoff,
            after,
            after,
            limit,
        ],
    )
    for row in rows:
        days = max((cutoff - (row["due_date"] or row["document_date"])).days, 0)
        row["days_overdue"] = days
        if aging:
            row["aging_bucket"] = (
                "current"
                if days == 0
                else "1-30"
                if days <= 30
                else "31-60"
                if days <= 60
                else "61-90"
                if days <= 90
                else "90+"
            )
    return cast(list[dict[str, Any]], _json(rows))
