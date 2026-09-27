import pytest
from django.db import connection


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_sales_invoice_draft_schema_contract() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema='erp' AND table_name='sales_invoice_tax_components'
              AND column_name IN (
                'rate_version_code_snapshot','calculation_method_snapshot',
                'calculation_base_snapshot','recovery_percent_snapshot',
                'rounding_method_snapshot','rounding_precision_snapshot',
                'currency_code_snapshot'
              )
            """
        )
        columns = dict(cursor.fetchall())
        assert len(columns) == 7
        assert set(columns.values()) == {"NO"}
        cursor.execute(
            """
            SELECT 1 FROM pg_trigger
            WHERE tgrelid='erp.sales_invoice_lines'::regclass
              AND tgname='trg_sales_invoice_line_limit' AND NOT tgisinternal
            """
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            """
            SELECT 1 FROM pg_trigger
            WHERE tgrelid='erp.sales_invoices'::regclass
              AND tgname='trg_sales_invoice_draft_audit' AND NOT tgisinternal
            """
        )
        assert cursor.fetchone() == (1,)


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_sales_posting_and_credit_schema_contract() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name,column_name
            FROM information_schema.columns
            WHERE table_schema='erp' AND (
                (table_name='sales_invoice_lines' AND column_name IN (
                    'credit_of_invoice_line_id','revenue_account_id_snapshot',
                    'inventory_account_id_snapshot','cogs_account_id_snapshot'
                )) OR
                (table_name='sales_invoice_tax_components' AND column_name IN (
                    'output_tax_account_id_snapshot','account_role_snapshot',
                    'polarity_snapshot'
                ))
            )
            """
        )
        assert set(cursor.fetchall()) == {
            ("sales_invoice_lines", "credit_of_invoice_line_id"),
            ("sales_invoice_lines", "revenue_account_id_snapshot"),
            ("sales_invoice_lines", "inventory_account_id_snapshot"),
            ("sales_invoice_lines", "cogs_account_id_snapshot"),
            ("sales_invoice_tax_components", "output_tax_account_id_snapshot"),
            ("sales_invoice_tax_components", "account_role_snapshot"),
            ("sales_invoice_tax_components", "polarity_snapshot"),
        }
        cursor.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE NOT tgisinternal AND (
                (tgrelid='erp.sales_invoices'::regclass
                 AND tgname='trg_sales_invoice_credit_link') OR
                (tgrelid='erp.sales_invoice_lines'::regclass
                 AND tgname='trg_sales_invoice_credit_line')
            )
            """
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "trg_sales_invoice_credit_link",
            "trg_sales_invoice_credit_line",
        }
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname='erp' AND indexname IN (
                'uq_sales_invoice_line_credited_once',
                'uq_journal_entry_source_document'
            )
            """
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "uq_sales_invoice_line_credited_once",
            "uq_journal_entry_source_document",
        }
        cursor.execute(
            "SELECT pg_get_functiondef('erp.project_stock_insert()'::regprocedure)"
        )
        function_definition = cursor.fetchone()[0]
        assert "VALUES (NEW.company_id,NEW.warehouse_id,NEW.item_id,NEW.lot_id,0,0)" in (
            " ".join(function_definition.split())
        )
