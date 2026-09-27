import importlib

import pytest
from django.db import connection, transaction

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_payment_schema_guards_permissions_and_rls() -> None:
    with connection.cursor() as c:
        c.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN "
            "('trg_ar_payment_immutable','trg_ap_payment_immutable',"
            "'trg_payment_compensation_guard','trg_payment_settlement_check',"
            "'trg_payment_audit','trg_payment_posted_link')"
        )
        assert len(c.fetchall()) == 6
        c.execute(
            "SELECT code FROM identity.permissions WHERE code IN "
            "('payments.view','payments.edit_draft','payments.post','payments.reverse')"
        )
        assert len(c.fetchall()) == 4
        c.execute(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE oid='erp.payment_allocation_reversals'::regclass"
        )
        assert c.fetchone() == (True,)
        c.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid="
            "'erp.payment_allocation_reversals'::regclass AND contype='f'"
        )
        assert c.fetchone() == (4,)
        for view in (
            "v_open_ar_invoices",
            "v_open_ap_bills",
            "v_open_ar_items_v2",
            "v_open_ap_items_v2",
        ):
            c.execute("SELECT pg_get_viewdef(%s::regclass)", [f"erp.{view}"])
            assert "reversal_of_payment_id" in c.fetchone()[0]


def test_payment_migration_backward_forward_round_trip() -> None:
    migration = importlib.import_module("apps.database.migrations.0015_phase5_payment_settlement")
    with transaction.atomic():
        with connection.cursor() as c:
            c.execute(migration.open_item_views(False))
            c.execute(migration.REVERSE_SQL)
            c.execute(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_schema='erp' AND table_name='payments' "
                "AND column_name='reversal_of_payment_id'"
            )
            assert c.fetchone() == (0,)
            c.execute(migration.SQL)
            c.execute(migration.open_item_views(True))
            c.execute(
                "SELECT count(*) FROM information_schema.columns WHERE "
                "table_schema='erp' AND table_name='payments' "
                "AND column_name='reversal_of_payment_id'"
            )
            assert c.fetchone() == (1,)
