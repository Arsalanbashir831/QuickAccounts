import datetime as dt
import uuid
from importlib import import_module

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction

from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command, _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_order_delivery_migration_reverse_forward(accounting_context):
    migration = import_module("apps.database.migrations.0037_sales_order_delivery")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(migration.REVERSE)
        cursor.execute(migration.SQL)
        transaction.set_rollback(True)


def _order(context, setup):
    root = _root(context)
    response = _command(
        setup["client"],
        f"{root}/sales/channels",
        {"code": str(uuid.uuid4()), "name": "Retail counter", "channel_kind": "retail"},
        1,
    )
    assert response.status_code == 201, response.json()
    response = _command(
        setup["client"],
        f"{root}/sales/orders",
        {
            "order_no": str(uuid.uuid4()),
            "partner_id": str(setup["source"]["partner_id"]),
            "channel_id": response.json()["id"],
            "order_date": "2026-09-27",
            "currency_code": setup["source"]["currency_code"],
            "lines": [
                {
                    "item_id": str(setup["ids"]["item"]),
                    "description": "Customer order",
                    "quantity": "1",
                    "unit_price": "100",
                    "tax_code_id": str(setup["ids"]["tax_code"]),
                }
            ],
        },
        1,
    )
    assert response.status_code == 201, response.json()
    return response.json()


def test_order_confirm_convert_post_and_delivery_without_duplicate_accounting(accounting_context):
    setup = _stock_setup(accounting_context)
    order = _order(accounting_context, setup)
    url = f"{_root(accounting_context)}/sales/orders/{order['id']}"
    response = _command(setup["client"], url, {"action": "confirm"}, 1)
    assert response.status_code == 200, response.json()
    data = {
        "action": "invoice",
        "invoice_no": str(uuid.uuid4()),
        "issue_date": "2026-09-27",
        "tax_jurisdiction_id": setup["source"]["tax_jurisdiction_id"],
    }
    response = _command(setup["client"], url, data, 2, "convert")
    assert response.status_code == 200, response.json()
    assert _command(setup["client"], url, data, 2, "convert").json() == response.json()
    assert (
        _command(setup["client"], url, data | {"invoice_no": str(uuid.uuid4())}, 2).status_code
        == 409
    )
    invoice = response.json()["invoices"][0]
    invoice_url = f"{_root(accounting_context)}/sales/invoices/{invoice['id']}"
    response = _command(setup["client"], f"{invoice_url}/calculate", {}, invoice["row_version"])
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"],
        f"{invoice_url}/post",
        {
            "journal_id": str(setup["posting"]["journal"]),
            "fiscal_period_id": str(accounting_context["period"]),
            "warehouse_id": str(setup["warehouse"]),
        },
        response.json()["row_version"],
    )
    assert response.status_code == 200, response.json()
    posted = response.json()
    assert not posted["delivery_confirmed"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        journals = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM erp.stock_movements")
        movements = cursor.fetchone()[0]
    body = {
        "delivered_date": str(dt.datetime.now(dt.UTC).date()),
        "delivery_reference": "POD-001",
        "received_by": "Customer",
        "notes": "Customer signed delivery note",
    }
    response = _command(
        setup["client"], f"{invoice_url}/delivery", body, posted["row_version"], "delivery"
    )
    assert response.status_code == 200, response.json()
    assert (
        _command(
            setup["client"], f"{invoice_url}/delivery", body, posted["row_version"], "delivery"
        ).json()
        == response.json()
    )
    assert setup["client"].get(invoice_url).json()["delivery_confirmed"]
    assert setup["client"].get(url).json()["status"] == "fulfilled"
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        assert cursor.fetchone()[0] == journals
        cursor.execute("SELECT count(*) FROM erp.stock_movements")
        assert cursor.fetchone()[0] == movements
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE erp.sales_deliveries SET notes='Changed'")


def test_order_draft_revision_cancel_and_immutable_confirmed_lines(accounting_context):
    setup = _stock_setup(accounting_context)
    order = _order(accounting_context, setup)
    url = f"{_root(accounting_context)}/sales/orders/{order['id']}"
    response = setup["client"].patch(
        url,
        {"external_ref": "ORDER-REF"},
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="edit",
    )
    assert response.status_code == 200, response.json()
    assert _command(setup["client"], url, {"action": "confirm"}, 1).status_code == 412
    response = _command(setup["client"], url, {"action": "confirm"}, 2)
    assert response.status_code == 200, response.json()
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.user_id',%s,true)", [str(accounting_context["user"])]
        )
        cursor.execute(
            "UPDATE erp.sales_order_lines SET ordered_quantity=2 WHERE sales_order_id=%s",
            [order["id"]],
        )
    response = _command(setup["client"], url, {"action": "cancel"}, 3)
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "cancelled"


def test_delivery_cannot_precede_stock_issue_or_invoice(accounting_context):
    setup = _stock_setup(accounting_context)
    source = setup["source"]
    response = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/invoices/{source['id']}/delivery",
        {
            "delivered_date": "2020-01-01",
            "delivery_reference": "fake",
            "received_by": "customer",
            "notes": "Too early",
        },
        source["row_version"],
    )
    assert response.status_code == 409, response.json()


def test_order_creation_under_deployed_runtime_role(accounting_context):
    from tests.api.test_phase5_cross_domain_release import _runtime_grants

    setup = _stock_setup(accounting_context)
    _runtime_grants()
    setup["client"].force_authenticate(get_user_model().objects.get(pk=accounting_context["user"]))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE quickaccounts_runtime")
        order = _order(accounting_context, setup)
        assert order["status"] == "draft"
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
