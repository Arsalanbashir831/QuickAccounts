from importlib import import_module

from django.db import migrations


def return_ar_views() -> str:
    statements = []
    for v2 in (False, True):
        view = "v_open_ar_items_v2" if v2 else "v_open_ar_invoices"
        total_name = "signed_document_total" if v2 else "document_total"
        open_name = "signed_open_amount" if v2 else "open_amount"
        sign = "CASE WHEN i.document_kind='invoice' THEN 1 ELSE -1 END"
        total = f"({sign})*coalesce(t.amount,0)" if v2 else "coalesce(t.amount,0)"
        outstanding = f"{total}-coalesce(p.amount,0)-coalesce(a.amount,0)"
        extra = (
            """
        UNION ALL SELECT r.company_id,r.id,r.return_no,'sales_return'::text,i.partner_id,
            r.return_date,NULL::date,i.currency_code,-r.credit_total,
            -(coalesce(f.amount,0)+coalesce(a.amount,0)),
            -r.credit_total+coalesce(f.amount,0)+coalesce(a.amount,0)
        FROM erp.sales_returns r JOIN erp.sales_invoices i
            ON i.company_id=r.company_id AND i.id=r.sales_invoice_id
        LEFT JOIN LATERAL(SELECT sum(amount) amount FROM erp.customer_refunds
            WHERE company_id=r.company_id AND sales_return_id=r.id
            AND refund_date<=CURRENT_DATE) f ON true
        LEFT JOIN LATERAL(SELECT sum(amount) amount FROM erp.sales_return_credit_applications
            WHERE company_id=r.company_id AND sales_return_id=r.id
            AND effective_date<=CURRENT_DATE) a ON true
        WHERE r.status='posted' AND r.return_date<=CURRENT_DATE
        """
            if v2
            else ""
        )
        statements.append(f"""
        CREATE OR REPLACE VIEW erp.{view} WITH(security_invoker=true) AS
        SELECT i.company_id,i.id AS sales_invoice_id,i.invoice_no,i.document_kind,
            i.partner_id,i.issue_date,i.due_date,i.currency_code,{total} AS {total_name},
            coalesce(p.amount,0)+coalesce(a.amount,0) AS applied_amount,
            {outstanding} AS {open_name}
        FROM erp.sales_invoices i LEFT JOIN LATERAL(
            SELECT sum(gross_amount) amount FROM erp.sales_invoice_lines
            WHERE company_id=i.company_id AND sales_invoice_id=i.id
        ) t ON true LEFT JOIN LATERAL(
            SELECT sum(x.applied_document_amount) amount FROM erp.ar_receipt_allocations x
            JOIN erp.payments p ON p.company_id=x.company_id AND p.id=x.payment_id
            WHERE x.company_id=i.company_id AND x.sales_invoice_id=i.id AND p.status='posted'
            AND p.payment_date<=CURRENT_DATE AND NOT EXISTS(SELECT 1 FROM erp.payments rv
                WHERE rv.company_id=p.company_id AND rv.reversal_of_payment_id=p.id
                AND rv.status='posted' AND rv.payment_date<=CURRENT_DATE)
        ) p ON true LEFT JOIN LATERAL(
            SELECT sum(amount) amount FROM erp.sales_return_credit_applications
            WHERE company_id=i.company_id AND sales_invoice_id=i.id AND effective_date<=CURRENT_DATE
        ) a ON true WHERE i.status='posted' {extra};
        """)
    return "\n".join(statements)


class Migration(migrations.Migration):
    dependencies = [("database", "0018_return_stock_dispositions")]
    operations = [
        migrations.RunSQL(
            return_ar_views(),
            import_module(
                "apps.database.migrations.0015_phase5_payment_settlement"
            ).open_item_views(True),
        )
    ]
