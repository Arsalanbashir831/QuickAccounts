from importlib import import_module

import pytest
from django.db import connection, transaction

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_manufacturing_planning_schema_rls_indexes_permissions_and_guards() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name,is_nullable FROM information_schema.columns "
            "WHERE table_schema='erp' AND table_name='production_orders' AND column_name IN "
            "('bom_revision_snapshot','bom_row_version_snapshot','bom_output_quantity_snapshot',"
            "'output_uom_id_snapshot','bom_effective_date','planning_edit_xid')"
        )
        columns = dict(cursor.fetchall())
        assert len(columns) == 6 and set(columns.values()) == {"NO"}
        cursor.execute(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE oid='erp.production_order_requirements'::regclass"
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='erp' AND indexname IN "
            "('uq_bom_component','ix_production_requirement_component',"
            "'ix_production_requirement_bom_line')"
        )
        assert len(cursor.fetchall()) == 3
        cursor.execute(
            "SELECT code FROM identity.permissions WHERE code IN "
            "('manufacturing.bom.view','manufacturing.bom.manage','manufacturing.bom.activate',"
            "'manufacturing.order.view','manufacturing.order.manage')"
        )
        assert len(cursor.fetchall()) == 5
        cursor.execute(
            "SELECT tgname,tgdeferrable,tginitdeferred FROM pg_trigger WHERE tgname IN "
            "('trg_manufacturing_bom_complete','trg_manufacturing_bom_lines_complete',"
            "'trg_production_plan_complete','trg_production_requirements_complete')"
        )
        triggers = cursor.fetchall()
        assert len(triggers) == 4 and all(row[1] and row[2] for row in triggers)


def test_manufacturing_empty_history_reverse_forward_preserves_baseline() -> None:
    migration = import_module("apps.database.migrations.0031_manufacturing_drafts")
    execution = import_module("apps.database.migrations.0032_manufacturing_execution")
    with transaction.atomic(), connection.cursor() as cursor:
        # SQL-owned fixtures are cleared only inside this rollback-only test transaction.
        cursor.execute(
            "TRUNCATE erp.production_execution_batches,erp.production_material_returns,"
            "erp.inventory_reservations,erp.production_order_requirements,erp.production_material_issues,"
            "erp.production_outputs,erp.production_orders,erp.bom_lines,erp.boms"
        )
        cursor.execute(execution.REVERSE)
        cursor.execute(migration.REVERSE)
        cursor.execute("SELECT to_regclass('erp.production_order_requirements')")
        assert cursor.fetchone()[0] is None
        cursor.execute(migration.SQL)
        cursor.execute(execution.SQL)
        cursor.execute("SELECT to_regclass('erp.production_order_requirements')")
        assert cursor.fetchone()[0] is not None
        cursor.execute(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgname IN ('trg_bom_version','trg_production_order_version')"
        )
        assert len(cursor.fetchall()) == 2
        transaction.set_rollback(True)
