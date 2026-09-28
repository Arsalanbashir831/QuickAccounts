import pytest
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_warehouse_migration_reverse_forward_contract() -> None:
    # SQL-owned ERP rows survive Django's model-only flush. Exercise empty-history
    # schema reversal without disabling production guards or losing other fixtures.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE erp.inventory_cost_allocations,erp.inventory_cost_layers,"
                "erp.inventory_return_cost_basis,erp.inventory_cost_basis_snapshots,"
                "erp.inventory_cost_checkpoint_movements,erp.production_material_issues,"
                "erp.production_outputs,erp.stock_movements"
            )
        _warehouse_reverse_forward()
        transaction.set_rollback(True)


def _warehouse_reverse_forward() -> None:
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([("database", "0015_phase5_payment_settlement")])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='erp' AND table_name='warehouses' "
                "AND column_name='stock_category'"
            )
            assert cursor.fetchone()[0] == 0
    finally:
        MigrationExecutor(connection).migrate(targets)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='erp' AND table_name='warehouses' "
            "AND column_name IN ('stock_category','row_version')"
        )
        assert {row[0] for row in cursor.fetchall()} == {"stock_category", "row_version"}
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN "
            "('trg_warehouse_configuration','trg_warehouse_audit',"
            "'trg_stock_warehouse','trg_reservation_warehouse')"
        )
        assert len(cursor.fetchall()) == 4
