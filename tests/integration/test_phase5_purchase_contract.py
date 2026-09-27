import pytest
from django.db import connection


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_purchase_bill_schema_contract() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema='erp' AND table_name='purchase_bill_tax_components'
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
            SELECT tgname FROM pg_trigger
            WHERE NOT tgisinternal AND (
                (tgrelid='erp.purchase_bill_lines'::regclass
                 AND tgname='trg_purchase_bill_line_limit') OR
                (tgrelid='erp.purchase_bills'::regclass
                 AND tgname IN ('trg_purchase_bill_audit','trg_purchase_bill_credit_link')) OR
                (tgrelid='erp.purchase_bill_lines'::regclass
                 AND tgname='trg_purchase_bill_credit_line')
            )
            """
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "trg_purchase_bill_line_limit",
            "trg_purchase_bill_audit",
            "trg_purchase_bill_credit_link",
            "trg_purchase_bill_credit_line",
        }
        cursor.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname='erp' AND indexname='uq_purchase_bill_line_credited_once'
            """
        )
        assert cursor.fetchone() == ("uq_purchase_bill_line_credited_once",)


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_purchase_bill_permissions_seeded() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT code FROM identity.permissions
            WHERE code IN (
                'purchasing.bill.view','purchasing.bill.edit_draft','purchasing.bill.post'
            )
            """
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "purchasing.bill.view",
            "purchasing.bill.edit_draft",
            "purchasing.bill.post",
        }
