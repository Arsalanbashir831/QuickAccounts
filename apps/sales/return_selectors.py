import uuid
from typing import Any, cast

from apps.payments.services import _json, _rows
from common.api.errors import ScopeNotFound


def return_detail(company_id: uuid.UUID, return_id: uuid.UUID) -> dict[str, Any]:
    rows = _rows(
        "SELECT * FROM erp.sales_returns WHERE company_id=%s AND id=%s", [company_id, return_id]
    )
    if not rows:
        raise ScopeNotFound()
    result = rows[0]
    result["lines"] = _rows(
        "SELECT * FROM erp.sales_return_lines WHERE company_id=%s AND sales_return_id=%s "
        "ORDER BY id",
        [company_id, return_id],
    )
    result["tax_components"] = _rows(
        "SELECT t.* FROM erp.sales_return_tax_components t JOIN erp.sales_return_lines l "
        "ON l.company_id=t.company_id AND l.id=t.sales_return_line_id WHERE "
        "l.company_id=%s AND l.sales_return_id=%s ORDER BY t.id",
        [company_id, return_id],
    )
    result["refunds"] = _rows(
        "SELECT * FROM erp.customer_refunds WHERE company_id=%s AND sales_return_id=%s "
        "ORDER BY posted_at DESC,id DESC LIMIT 200",
        [company_id, return_id],
    )
    result["credit_applications"] = _rows(
        "SELECT * FROM erp.sales_return_credit_applications WHERE company_id=%s AND "
        "sales_return_id=%s ORDER BY posted_at DESC,id DESC LIMIT 200",
        [company_id, return_id],
    )
    result["replacements"] = _rows(
        "SELECT * FROM erp.sales_return_replacements WHERE company_id=%s AND sales_return_id=%s",
        [company_id, return_id],
    )
    used = _rows(
        "SELECT coalesce((SELECT sum(amount) FROM erp.customer_refunds "
        "WHERE company_id=%s AND sales_return_id=%s),0) + "
        "coalesce((SELECT sum(amount) FROM erp.sales_return_credit_applications "
        "WHERE company_id=%s AND sales_return_id=%s),0) AS amount",
        [company_id, return_id, company_id, return_id],
    )[0]["amount"]
    result["history_limit"] = 200
    result["remaining_credit"] = result["credit_total"] - used
    result["settlement_status"] = (
        "settled" if result["status"] == "posted" and result["remaining_credit"] == 0 else "open"
    )
    result["sale_summary"] = invoice_return_summary(company_id, result["sales_invoice_id"])
    result["stock_dispositions"] = _rows(
        "SELECT a.* FROM erp.sales_return_stock_actions a "
        "JOIN erp.sales_return_lines l ON l.company_id=a.company_id AND "
        "l.id=a.sales_return_line_id "
        "WHERE l.company_id=%s AND l.sales_return_id=%s "
        "ORDER BY a.posted_at DESC,a.id DESC LIMIT 200",
        [company_id, return_id],
    )
    return cast(dict[str, Any], _json(result))


def invoice_return_summary(company_id: uuid.UUID, invoice_id: uuid.UUID) -> dict[str, Any]:
    source = _rows(
        "SELECT id,status,document_kind FROM erp.sales_invoices WHERE company_id=%s AND id=%s",
        [company_id, invoice_id],
    )
    if not source:
        raise ScopeNotFound()
    lines = _rows(
        """
        SELECT l.id,l.item_id,l.quantity AS sold_quantity,l.gross_amount,
            coalesce(r.returned_quantity,0) AS returned_quantity,
            l.quantity-coalesce(r.returned_quantity,0) AS returnable_quantity,
            coalesce(r.credited,0)+coalesce(c.credited,0) AS credited_amount,
            l.gross_amount-coalesce(r.credited,0)-coalesce(c.credited,0) AS creditable_amount,
            l.inventory_account_id_snapshot IS NOT NULL AS is_stock
        FROM erp.sales_invoice_lines l LEFT JOIN LATERAL(
            SELECT sum(rl.quantity) returned_quantity,sum(rl.credit_gross) credited
            FROM erp.sales_return_lines rl JOIN erp.sales_returns sr
                ON sr.company_id=rl.company_id AND sr.id=rl.sales_return_id
            WHERE rl.company_id=l.company_id AND rl.sales_invoice_line_id=l.id
                AND sr.status='posted'
        ) r ON true LEFT JOIN LATERAL(
            SELECT sum(cl.gross_amount) credited FROM erp.sales_invoice_lines cl
            JOIN erp.sales_invoices ci ON ci.company_id=cl.company_id AND ci.id=cl.sales_invoice_id
            WHERE cl.company_id=l.company_id AND cl.credit_of_invoice_line_id=l.id
                AND ci.status='posted'
        ) c ON true WHERE l.company_id=%s AND l.sales_invoice_id=%s ORDER BY l.line_no
    """,
        [company_id, invoice_id],
    )
    refunds = _rows(
        """
        SELECT coalesce(sum(f.amount),0) amount FROM erp.customer_refunds f
        JOIN erp.sales_returns r ON r.company_id=f.company_id AND r.id=f.sales_return_id
        WHERE r.company_id=%s AND r.sales_invoice_id=%s
    """,
        [company_id, invoice_id],
    )[0]["amount"]
    cash = _rows(
        """
        SELECT coalesce(sum(a.applied_document_amount),0) amount FROM erp.ar_receipt_allocations a
        JOIN erp.payments p ON p.company_id=a.company_id AND p.id=a.payment_id
        WHERE a.company_id=%s AND a.sales_invoice_id=%s AND p.direction='receipt'
        AND p.status='posted' AND p.withholding_total=0 AND p.reversal_of_payment_id IS NULL
        AND NOT EXISTS(SELECT 1 FROM erp.payments rv WHERE rv.company_id=p.company_id
            AND rv.reversal_of_payment_id=p.id AND rv.status='posted')
    """,
        [company_id, invoice_id],
    )[0]["amount"]
    stock = [line for line in lines if line["is_stock"]]
    returned = sum((line["returned_quantity"] for line in stock), start=0)
    sold = sum((line["sold_quantity"] for line in stock), start=0)
    credited = sum((line["credited_amount"] for line in lines), start=0)
    status = (
        "fully_returned"
        if sold and returned == sold
        else "partially_returned"
        if returned
        else "adjusted"
        if credited
        else "unchanged"
    )
    return cast(
        dict[str, Any],
        _json(
            {
                "invoice_id": invoice_id,
                "adjustment_status": status,
                "lines": lines,
                "credited_total": credited,
                "refunded_total": refunds,
                "remaining_received_cash": max(cash - refunds, 0),
                "note": "Cash refund eligibility also requires an unconsumed posted return credit.",
            }
        ),
    )


def list_returns(
    company_id: uuid.UUID, invoice_id: uuid.UUID, *, after: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        _json(
            _rows(
                "SELECT id,return_no,kind,status,return_date,row_version,credit_total FROM "
                "erp.sales_returns WHERE company_id=%s AND sales_invoice_id=%s AND (%s::uuid IS "
                "NULL OR id>%s) ORDER BY id LIMIT %s",
                [company_id, invoice_id, after, after, limit],
            )
        ),
    )
