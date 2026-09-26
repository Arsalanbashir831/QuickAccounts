import pytest
from django.db import connection


@pytest.mark.integration
@pytest.mark.p0
@pytest.mark.django_db(transaction=True)
def test_phase5_item_schema_contract() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_default, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'erp'
              AND table_name = 'items'
              AND column_name = 'row_version'
            """
        )
        row_version = cursor.fetchone()
        cursor.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'erp.items'::regclass
            """
        )
        constraints = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgrelid IN (
                'erp.items'::regclass,
                'erp.item_accounting_profiles'::regclass
            ) AND NOT tgisinternal
            """
        )
        triggers = {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT code FROM identity.permissions WHERE code LIKE 'item.%' ORDER BY code"
        )
        permissions = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT prosecdef
            FROM pg_proc
            WHERE oid = 'erp.audit_item_configuration_change()'::regprocedure
            """
        )
        audit_is_security_definer = cursor.fetchone()[0]

    assert row_version == ("1", "NO")
    assert "ck_items_tracking_requires_stock" in constraints
    assert "trg_item_version" in triggers
    assert "trg_item_audit" in triggers
    assert "trg_item_accounting_profile_audit" in triggers
    assert permissions == ["item.manage", "item.view"]
    assert audit_is_security_definer is True
