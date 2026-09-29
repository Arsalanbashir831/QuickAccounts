from importlib import import_module

import pytest
from django.db import connection, transaction

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_execution_schema_has_rls_composite_keys_and_deferred_reconciliation():
    with connection.cursor() as cursor:
        for table in ("production_execution_batches", "production_material_returns"):
            cursor.execute(
                "SELECT relrowsecurity FROM pg_class WHERE oid=%s::regclass", [f"erp.{table}"]
            )
            assert cursor.fetchone()[0] is True
        cursor.execute(
            "SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_production_execution_complete',"
            "'trg_production_batch_complete','trg_production_movement_complete') AND "
            "tgdeferrable AND tginitdeferred"
        )
        assert cursor.fetchone()[0] == 3
        cursor.execute(
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('fk_production_issue_reservation',"
            "'fk_production_issue_batch','fk_production_output_batch','fk_production_wip') "
            "AND contype='f'"
        )
        assert cursor.fetchone()[0] == 4


def test_execution_empty_history_reverse_forward():
    migration = import_module("apps.database.migrations.0032_manufacturing_execution")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "TRUNCATE "
            "erp.production_material_returns,erp.production_material_issues,erp.production_outputs,"
            "erp.production_execution_batches,erp.production_order_requirements,erp.production_orders,erp.inventory_reservations"
        )
        cursor.execute(migration.REVERSE)
        cursor.execute("SELECT to_regclass('erp.production_execution_batches')")
        assert cursor.fetchone()[0] is None
        cursor.execute(migration.SQL)
        cursor.execute("SELECT to_regclass('erp.production_execution_batches')")
        assert cursor.fetchone()[0] is not None
        transaction.set_rollback(True)
