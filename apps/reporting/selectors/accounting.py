import datetime as dt
import uuid
from typing import Any

from apps.accounting.selectors.accounting import fetch_all


def trial_balance(
    company_id: uuid.UUID,
    *,
    date_from: dt.date,
    date_to: dt.date,
    limit: int = 201,
    after: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT a.id AS account_id,a.code AS account_code,a.name AS account_name,
               a.account_type,a.normal_balance,c.functional_currency AS currency_code,
               coalesce(sum(l.debit_amount-l.credit_amount)
                   FILTER (WHERE e.entry_date < %s),0) AS opening_net_debit,
               coalesce(sum(l.debit_amount)
                   FILTER (WHERE e.entry_date BETWEEN %s AND %s),0) AS period_debits,
               coalesce(sum(l.credit_amount)
                   FILTER (WHERE e.entry_date BETWEEN %s AND %s),0) AS period_credits,
               coalesce(sum(l.debit_amount-l.credit_amount)
                   FILTER (WHERE e.entry_date <= %s),0) AS closing_net_debit
        FROM erp.accounts a
        JOIN erp.companies c ON c.id=a.company_id
        LEFT JOIN erp.journal_lines l ON l.company_id=a.company_id AND l.account_id=a.id
        LEFT JOIN erp.journal_entries e
          ON e.company_id=l.company_id AND e.id=l.journal_entry_id AND e.status='posted'
        WHERE a.company_id=%s AND (%s::uuid IS NULL OR (a.code,a.id) > (
          SELECT prior.code,prior.id FROM erp.accounts prior
          WHERE prior.company_id=a.company_id AND prior.id=%s))
        GROUP BY a.id,a.code,a.name,a.account_type,a.normal_balance,c.functional_currency
        ORDER BY a.code,a.id LIMIT %s
        """,
        [date_from, date_from, date_to, date_from, date_to, date_to, company_id,
         after, after, limit],
    )


def period_activity(company_id: uuid.UUID, period_id: uuid.UUID) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT fiscal_period_id,fiscal_period_code,account_id,account_code,
               account_name,account_type,debits,credits,net_debit
        FROM erp.v_period_account_activity
        WHERE company_id=%s AND fiscal_period_id=%s
        ORDER BY account_code,account_id
        """,
        [company_id, period_id],
    )


def general_ledger(
    company_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    date_from: dt.date,
    date_to: dt.date,
    limit: int,
    after_date: dt.date | None = None,
    after_entry: uuid.UUID | None = None,
    after_line: int | None = None,
) -> list[dict[str, Any]]:
    return fetch_all(
        """
        WITH opening AS (
            SELECT coalesce(sum(l.debit_amount-l.credit_amount),0) AS amount
            FROM erp.journal_entries e
            JOIN erp.journal_lines l
              ON l.company_id=e.company_id AND l.journal_entry_id=e.id
            WHERE e.company_id=%s AND l.account_id=%s AND e.status='posted'
              AND (e.entry_date<%s OR (%s::date IS NOT NULL AND e.entry_date<=%s
                AND (e.entry_date,e.id,l.line_no)<=(%s::date,%s::uuid,%s)))
        ), movement AS (
            SELECT e.id AS entry_id,e.entry_number,e.entry_date,e.description AS entry_description,
                   l.id AS line_id,l.line_no,l.description,l.transaction_currency,
                   l.exchange_rate,l.transaction_debit,l.transaction_credit,
                   l.debit_amount,l.credit_amount
            FROM erp.journal_entries e
            JOIN erp.journal_lines l
              ON l.company_id=e.company_id AND l.journal_entry_id=e.id
            WHERE e.company_id=%s AND l.account_id=%s AND e.status='posted'
              AND e.entry_date BETWEEN %s AND %s
              AND (%s::date IS NULL OR (e.entry_date,e.id,l.line_no)>(%s::date,%s::uuid,%s))
            ORDER BY e.entry_date,e.id,l.line_no
            LIMIT %s
        )
        SELECT m.*,o.amount AS opening_balance,
               o.amount+sum(m.debit_amount-m.credit_amount) OVER (
                   ORDER BY m.entry_date,m.entry_id,m.line_no
                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS running_balance
        FROM movement m CROSS JOIN opening o
        ORDER BY m.entry_date,m.entry_id,m.line_no
        """,
        [
            company_id,
            account_id,
            date_from,
            after_date,
            date_to,
            after_date,
            after_entry,
            after_line,
            company_id,
            account_id,
            date_from,
            date_to,
            after_date,
            after_date,
            after_entry,
            after_line,
            limit,
        ],
    )


def tax_components(
    company_id: uuid.UUID,
    *,
    date_from: dt.date,
    date_to: dt.date,
    limit: int = 201,
    after: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT x.id,x.source_type,x.document_id,x.document_no,x.document_date,
               x.tax_jurisdiction_id,x.tax_rate_version_id,x.tax_code_component_id,
               x.taxable_base_amount,x.rate_snapshot,x.tax_amount,
               x.recoverable_amount,x.tax_treatment
        FROM (
          SELECT t.id,'sales_invoice'::text source_type,t.sales_invoice_id document_id,
            d.invoice_no document_no,d.issue_date document_date,
            t.tax_jurisdiction_id,t.tax_rate_version_id,t.tax_code_component_id,
            t.taxable_base_amount,t.rate_snapshot,t.tax_amount,
            0::numeric recoverable_amount,NULL::text tax_treatment
          FROM erp.sales_invoice_tax_components t JOIN erp.sales_invoices d
            ON d.company_id=t.company_id AND d.id=t.sales_invoice_id
          WHERE t.company_id=%s AND d.status='posted'
            AND d.issue_date BETWEEN %s AND %s
          UNION ALL
          SELECT t.id,'purchase_bill',t.purchase_bill_id,d.bill_no,d.bill_date,
            t.tax_jurisdiction_id,t.tax_rate_version_id,t.tax_code_component_id,
            t.taxable_base_amount,t.rate_snapshot,t.tax_amount,
            t.recoverable_amount,NULL::text
          FROM erp.purchase_bill_tax_components t JOIN erp.purchase_bills d
            ON d.company_id=t.company_id AND d.id=t.purchase_bill_id
          WHERE t.company_id=%s AND d.status='posted'
            AND d.bill_date BETWEEN %s AND %s
          UNION ALL
          SELECT t.id,'payment',t.payment_id,d.payment_no,d.payment_date,
            t.tax_jurisdiction_id,t.tax_rate_version_id,t.tax_code_component_id,
            t.taxable_base_amount,t.rate_snapshot,t.tax_amount,
            0::numeric,t.tax_treatment
          FROM erp.payment_tax_components t JOIN erp.payments d
            ON d.company_id=t.company_id AND d.id=t.payment_id
          WHERE t.company_id=%s AND d.status='posted'
            AND d.payment_date BETWEEN %s AND %s
        ) x WHERE (%s::uuid IS NULL OR x.id>%s) ORDER BY x.id LIMIT %s
        """,
        [company_id, date_from, date_to, company_id, date_from, date_to,
         company_id, date_from, date_to, after, after, limit],
    )
