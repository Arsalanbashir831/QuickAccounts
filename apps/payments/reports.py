import datetime as dt
import uuid
from typing import Any, cast

from django.db import connection

from apps.payments.services import _json, _tables


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
    kind = "invoice" if direction == "receipt" else "bill"
    # Effective date and immutable posting timestamp both constrain historical facts.
    sql = f"""
    WITH active_payments AS (
        SELECT p.* FROM erp.payments p WHERE p.company_id=%(company)s AND p.status='posted'
        AND p.payment_date<=%(cutoff)s
        AND p.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
        AND NOT EXISTS(SELECT 1 FROM erp.payments rv WHERE rv.company_id=p.company_id
            AND rv.reversal_of_payment_id=p.id AND rv.status='posted'
            AND rv.payment_date<=%(cutoff)s
            AND rv.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC'))
    ), return_applications AS (
        SELECT a.sales_invoice_id,sum(a.amount) amount
        FROM erp.sales_return_credit_applications a JOIN erp.sales_returns r
            ON r.company_id=a.company_id AND r.id=a.sales_return_id
        WHERE a.company_id=%(company)s AND %(direction)s='receipt'
            AND a.effective_date<=%(cutoff)s AND r.return_date<=%(cutoff)s
            AND a.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
            AND r.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
        GROUP BY a.sales_invoice_id
    ), document_items AS (
        SELECT d.id,d.{number} AS document_no,d.{partner} AS partner_id,d.currency_code,
            d.{date_column} AS document_date,d.due_date,d.document_kind AS item_kind,
            t.gross_total,coalesce(a.applied,0)+coalesce(c.amount,0) AS applied_amount,
            t.gross_total-coalesce(a.applied,0)-coalesce(c.amount,0) AS open_amount
        FROM erp.{documents} d JOIN LATERAL(
            SELECT sum(gross_amount)*CASE WHEN d.document_kind='{kind}' THEN 1 ELSE -1 END
                AS gross_total FROM erp.{lines} WHERE company_id=d.company_id AND {column}=d.id
        ) t ON true LEFT JOIN LATERAL(
            SELECT sum(x.applied_document_amount) AS applied FROM erp.{table} x
            JOIN active_payments p ON p.company_id=x.company_id AND p.id=x.payment_id
            WHERE x.company_id=d.company_id AND x.{column}=d.id
        ) a ON true LEFT JOIN return_applications c ON c.sales_invoice_id=d.id
        WHERE d.company_id=%(company)s AND d.status='posted' AND d.{date_column}<=%(cutoff)s
            AND d.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
    ), unapplied_items AS (
        SELECT p.id,p.payment_no,p.partner_id,p.currency_code,p.payment_date,NULL::date,
            'unallocated_payment'::text,-(p.amount+p.withholding_total-coalesce(a.applied,0)),
            0::numeric,-(p.amount+p.withholding_total-coalesce(a.applied,0))
        FROM active_payments p LEFT JOIN LATERAL(
            SELECT sum(applied_payment_amount) applied FROM erp.{table}
            WHERE company_id=p.company_id AND payment_id=p.id
        ) a ON true WHERE p.direction=%(direction)s AND p.reversal_of_payment_id IS NULL
    ), return_items AS (
        SELECT r.id,r.return_no,i.partner_id,i.currency_code,r.return_date,NULL::date,
            'sales_return'::text,-r.credit_total,-coalesce(s.amount,0),
            -r.credit_total+coalesce(s.amount,0)
        FROM erp.sales_returns r JOIN erp.sales_invoices i
            ON i.company_id=r.company_id AND i.id=r.sales_invoice_id
        LEFT JOIN LATERAL(
            SELECT sum(amount) amount FROM (
                SELECT amount FROM erp.customer_refunds f WHERE f.company_id=r.company_id
                    AND f.sales_return_id=r.id AND f.refund_date<=%(cutoff)s
                    AND f.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
                UNION ALL SELECT amount FROM erp.sales_return_credit_applications a
                    WHERE a.company_id=r.company_id AND a.sales_return_id=r.id
                    AND a.effective_date<=%(cutoff)s
                    AND a.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
            ) settled
        ) s ON true WHERE r.company_id=%(company)s AND %(direction)s='receipt'
            AND r.status='posted' AND r.return_date<=%(cutoff)s
            AND r.posted_at<((%(cutoff)s::date+1)::timestamp AT TIME ZONE 'UTC')
    ) SELECT * FROM (
        SELECT * FROM document_items UNION ALL SELECT * FROM unapplied_items
        UNION ALL SELECT * FROM return_items
    ) items WHERE open_amount<>0 AND (%(after)s::uuid IS NULL OR id>%(after)s)
    ORDER BY id LIMIT %(limit)s
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            {
                "company": company_id,
                "cutoff": cutoff,
                "direction": direction,
                "after": after,
                "limit": limit,
            },
        )
        names = [col.name for col in cursor.description]
        rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
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
