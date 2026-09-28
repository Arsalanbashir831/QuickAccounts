from importlib import import_module

import pytest
from django.db import connection, transaction


@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
def test_checkpoint_rebuild_reverse_forward_preserves_security_contract() -> None:
    migration = import_module("apps.database.migrations.0030_checkpoint_scope_rebuild")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT proacl::text,prosecdef,proconfig FROM pg_proc "
            "WHERE oid='erp.rebuild_inventory_positions(uuid,uuid)'::regprocedure"
        )
        security = cursor.fetchone()
        assert security[1] is True and "search_path=pg_catalog" in security[2]
        cursor.execute(migration._function)
        cursor.execute(
            "SELECT pg_get_functiondef('erp.rebuild_inventory_positions(uuid,uuid)'::regprocedure)"
        )
        assert "inventory_cost_checkpoints" not in cursor.fetchone()[0]
        cursor.execute(migration.SQL)
        cursor.execute(
            "SELECT pg_get_functiondef('erp.rebuild_inventory_positions(uuid,uuid)'::regprocedure),"
            "proacl::text,prosecdef,proconfig FROM pg_proc "
            "WHERE oid='erp.rebuild_inventory_positions(uuid,uuid)'::regprocedure"
        )
        definition, *restored = cursor.fetchone()
        assert "inventory_cost_checkpoints" in definition
        assert "inventory_cost_layers" in definition
        assert tuple(restored) == security
        transaction.set_rollback(True)
