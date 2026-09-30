"""Date and cursor indexes for bounded primary-database financial reports."""

from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("database", "0043_serial_supplier_returns")]
    operations = [
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_report_sales_date "
            "ON erp.sales_invoices(company_id,issue_date,id) WHERE status='posted'",
            "DROP INDEX CONCURRENTLY IF EXISTS erp.ix_report_sales_date",
        ),
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_report_bills_date "
            "ON erp.purchase_bills(company_id,bill_date,id) WHERE status='posted'",
            "DROP INDEX CONCURRENTLY IF EXISTS erp.ix_report_bills_date",
        ),
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_report_payments_date "
            "ON erp.payments(company_id,payment_date,id) WHERE status='posted'",
            "DROP INDEX CONCURRENTLY IF EXISTS erp.ix_report_payments_date",
        ),
        migrations.RunSQL(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_report_stock_cutoff "
            "ON erp.stock_movements(company_id,occurred_at,id)",
            "DROP INDEX CONCURRENTLY IF EXISTS erp.ix_report_stock_cutoff",
        ),
    ]
