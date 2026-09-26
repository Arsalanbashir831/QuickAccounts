import datetime as dt
import uuid
from typing import Any

from apps.accounting.selectors.accounting import fetch_all


def trial_balance(
    company_id: uuid.UUID,
    *,
    date_from: dt.date,
    date_to: dt.date,
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
        WHERE a.company_id=%s
        GROUP BY a.id,a.code,a.name,a.account_type,a.normal_balance,c.functional_currency
        ORDER BY a.code,a.id
        """,
        [date_from, date_from, date_to, date_from, date_to, date_to, company_id],
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
) -> list[dict[str, Any]]:
    return fetch_all(
        """
        WITH opening AS (
            SELECT coalesce(sum(l.debit_amount-l.credit_amount),0) AS amount
            FROM erp.journal_entries e
            JOIN erp.journal_lines l
              ON l.company_id=e.company_id AND l.journal_entry_id=e.id
            WHERE e.company_id=%s AND l.account_id=%s AND e.status='posted'
              AND e.entry_date<%s
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
            company_id,
            account_id,
            date_from,
            date_to,
            limit,
        ],
    )
