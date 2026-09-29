import uuid
from importlib import import_module

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction

from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command
from tests.api.test_supplier_return_costing_api import _setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_claim_migration_reverse_forward(accounting_context):
    migration = import_module("apps.database.migrations.0038_supplier_claims")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(migration.reverse_sql())
        cursor.execute(migration.SQL)
        transaction.set_rollback(True)


def _claim(context, setup):
    credit = setup["credit"]
    body = {
        "claim_no": str(uuid.uuid4()),
        "source_bill_line_id": credit["lines"][0]["credit_of_bill_line_id"],
        "warehouse_id": str(setup["warehouse"]),
        "reason": "Supplier accepted physical return",
    }
    response = _command(setup["client"], f"{_root(context)}/purchasing/supplier-claims", body, 1)
    assert response.status_code == 201, response.json()
    return response.json()


def test_supplier_claim_is_nonfinancial_until_physical_credit_posts(accounting_context):
    setup = _setup(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        before_journals = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM erp.stock_movements")
        before_movements = cursor.fetchone()[0]
    claim = _claim(accounting_context, setup)
    url = f"{_root(accounting_context)}/purchasing/supplier-claims/{claim['id']}"
    assert (
        _command(
            setup["client"],
            url,
            {"action": "settle", "supplier_credit_id": setup["credit"]["id"]},
            1,
        ).status_code
        == 409
    )
    assert _command(setup["client"], url, {"action": "approve"}, 1).status_code == 409
    response = _command(
        setup["client"], url, {"action": "approve", "approve_supplier_claim": True}, 1, "approve"
    )
    assert response.status_code == 200, response.json()
    assert (
        _command(
            setup["client"],
            url,
            {"action": "approve", "approve_supplier_claim": True},
            1,
            "approve",
        ).json()
        == response.json()
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        assert cursor.fetchone()[0] == before_journals
        cursor.execute("SELECT count(*) FROM erp.stock_movements")
        assert cursor.fetchone()[0] == before_movements
    assert (
        _command(
            setup["client"],
            url,
            {"action": "settle", "supplier_credit_id": setup["credit"]["id"]},
            2,
        ).status_code
        == 409
    )
    posted = _command(setup["client"], setup["url"], setup["payload"], 1)
    assert posted.status_code == 200, posted.json()
    response = _command(
        setup["client"],
        url,
        {"action": "settle", "supplier_credit_id": setup["credit"]["id"]},
        2,
        "settle",
    )
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "settled"
    assert (
        _command(
            setup["client"],
            url,
            {"action": "settle", "supplier_credit_id": setup["credit"]["id"]},
            2,
            "settle",
        ).json()
        == response.json()
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        assert cursor.fetchone()[0] == before_journals + 1
        cursor.execute("SELECT count(*) FROM erp.stock_movements")
        assert cursor.fetchone()[0] == before_movements + 1


def test_claim_reject_and_database_immutability(accounting_context):
    setup = _setup(accounting_context)
    claim = _claim(accounting_context, setup)
    url = f"{_root(accounting_context)}/purchasing/supplier-claims/{claim['id']}"
    response = _command(setup["client"], url, {"action": "reject"}, 1)
    assert response.status_code == 200, response.json()
    assert (
        _command(
            setup["client"], url, {"action": "approve", "approve_supplier_claim": True}, 2
        ).status_code
        == 409
    )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.supplier_return_claims SET status='settled' WHERE id=%s", [claim["id"]]
        )


def test_claim_creation_under_deployed_runtime_role(accounting_context):
    from tests.api.test_phase5_cross_domain_release import _runtime_grants

    setup = _setup(accounting_context)
    _runtime_grants()
    setup["client"].force_authenticate(get_user_model().objects.get(pk=accounting_context["user"]))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE quickaccounts_runtime")
        claim = _claim(accounting_context, setup)
        assert claim["status"] == "draft"
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
