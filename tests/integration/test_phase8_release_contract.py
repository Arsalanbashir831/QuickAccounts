"""Release-critical catalog and routing invariants across recent domains."""

import pytest
from django.db import connection

from apps.accounting.models import Account
from common.db.primary_router import FinancialPrimaryRouter

pytestmark = [pytest.mark.integration, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_recent_release_tables_remain_rls_protected_and_indexed() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity "
            "FROM pg_class c WHERE c.oid IN "
            "('erp.inventory_serials'::regclass,'erp.serial_movements'::regclass,"
            "'erp.job_artifacts'::regclass,'erp.internal_event_deliveries'::regclass)"
        )
        rows = cursor.fetchall()
        assert len(rows) == 4
        assert all(enabled and forced for _, enabled, forced in rows)
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='erp' AND indexname IN "
            "('ix_serial_exact','ix_serial_movement_cursor','ix_report_sales_date',"
            "'ix_report_bills_date','ix_report_payments_date','ix_report_stock_cutoff')"
        )
        assert len(cursor.fetchall()) == 6
        cursor.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname='quickaccounts_runtime'"
        )
        assert cursor.fetchone() == (False,)


def test_financial_orm_reads_and_writes_are_pinned_to_primary() -> None:
    router = FinancialPrimaryRouter()
    assert router.db_for_read(Account) == "default"
    assert router.db_for_write(Account) == "default"
