import concurrent.futures
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.stock_commands import post_document, reserve_stock
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command, _posting, _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _document(context: dict, setup: dict, kind: str, quantity: str = "2", **extra: object) -> dict:
    line = {"item_id": str(setup["ids"]["item"]), "quantity": quantity, **extra}
    if kind in {"receipt", "adjustment_in"}:
        line |= {"to_warehouse_id": str(setup["warehouse"]), "unit_cost_company": "100"}
    else:
        line |= {"from_warehouse_id": str(setup["warehouse"])}
    response = setup["client"].post(
        f"{_root(context)}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": kind,
            "document_date": "2026-09-27",
            "reason": "Stock check",
            "lines": [line],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _post(context: dict, setup: dict, document: dict, **extra: object):
    return _command(
        setup["client"],
        f"{_root(context)}/inventory/documents/{document['id']}/post",
        _posting(context) | extra,
        document["row_version"],
    )


def test_inv001_receipt_post_replay_and_immutable(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    missing_cost = setup["client"].post(
        f"{_root(accounting_context)}/inventory/documents",
        {
            "document_no": "MISSING-COST",
            "document_kind": "receipt",
            "document_date": "2026-09-27",
            "lines": [
                {
                    "item_id": str(setup["ids"]["item"]),
                    "quantity": "1",
                    "to_warehouse_id": str(setup["warehouse"]),
                }
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="missing-cost",
    )
    assert missing_cost.status_code == 409
    assert missing_cost.json()["error"]["code"] == "STOCK_RECEIPT_COST_REQUIRED"
    document = _document(accounting_context, setup, "receipt")
    data = _posting(accounting_context) | {"offset_account_id": str(setup["posting"]["cogs"])}
    url = f"{_root(accounting_context)}/inventory/documents/{document['id']}"
    posted = _command(setup["client"], f"{url}/post", data, 1, "receipt")
    assert posted.status_code == 200, posted.json()
    assert _command(setup["client"], f"{url}/post", data, 1, "receipt").json() == posted.json()
    assert (
        setup["client"]
        .patch(
            url,
            {"reason": "changed"},
            format="json",
            HTTP_IF_MATCH='"2"',
            HTTP_IDEMPOTENCY_KEY="edit",
        )
        .status_code
        == 409
    )
    with connection.cursor() as c:
        c.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s",
            [accounting_context["company"], setup["warehouse"]],
        )
        assert c.fetchone() == (D(10), D(1000))
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as c:
        c.execute(
            "UPDATE erp.stock_movements SET quantity_delta=5 WHERE inventory_document_line_id=%s",
            [document["lines"][0]["id"]],
        )


def test_inv005_transfer_reservation_consumption_and_reconciliation(
    accounting_context: dict,
) -> None:
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    warehouse = (
        setup["client"]
        .post(f"{root}/inventory/warehouses", {"code": "OTHER", "name": "Other"}, format="json")
        .json()
    )
    document = _document(accounting_context, setup, "transfer", to_warehouse_id=warehouse["id"])
    reservation = setup["client"].post(
        f"{root}/inventory/reservations",
        {
            "line_id": document["lines"][0]["id"],
            "document_revision": 1,
            "quantity": "2",
            "reservation_key": "reserve",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="reserve",
    )
    assert reservation.status_code == 201, reservation.json()
    posted = _post(accounting_context, setup, document)
    assert posted.status_code == 200, posted.json()
    assert posted.json()["journal_entry_id"] is None
    with connection.cursor() as c:
        c.execute(
            "SELECT status FROM erp.inventory_reservations WHERE id=%s", [reservation.json()["id"]]
        )
        assert c.fetchone()[0] == "consumed"
        c.execute(
            "SELECT quantity_delta,value_delta_company FROM erp.stock_movements "
            "WHERE inventory_document_line_id=%s ORDER BY quantity_delta",
            [document["lines"][0]["id"]],
        )
        assert c.fetchall() == [(D(-2), D(-200)), (D(2), D(200))]
    assert setup["client"].get(f"{root}/inventory/reconciliation").json()["matches"] is True
    rebuilt = setup["client"].post(
        f"{root}/inventory/rebuild", {}, format="json", HTTP_IDEMPOTENCY_KEY="rebuild"
    )
    assert rebuilt.status_code == 200, rebuilt.json()
    assert rebuilt.json()["scope_count"] == 2
    assert (
        setup["client"]
        .post(f"{root}/inventory/rebuild", {}, format="json", HTTP_IDEMPOTENCY_KEY="rebuild")
        .json()
        == rebuilt.json()
    )
    assert setup["client"].get(f"{root}/inventory/reconciliation").json()["matches"] is True


def test_inv002_loss_approval_negative_stock_and_release(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    doc = _document(accounting_context, setup, "adjustment_out", "9")
    assert (
        _post(
            accounting_context,
            setup,
            doc,
            offset_account_id=str(setup["posting"]["cogs"]),
            approve_loss=True,
        ).status_code
        == 409
    )
    doc = _document(accounting_context, setup, "adjustment_out")
    assert (
        _post(
            accounting_context, setup, doc, offset_account_id=str(setup["posting"]["cogs"])
        ).status_code
        == 409
    )
    reservation = (
        setup["client"]
        .post(
            f"{root}/inventory/reservations",
            {
                "line_id": doc["lines"][0]["id"],
                "document_revision": 1,
                "quantity": "2",
                "reservation_key": "release",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="release",
        )
        .json()
    )
    released = _command(
        setup["client"],
        f"{root}/inventory/reservations/{reservation['id']}/release",
        {},
        1,
        "release",
    )
    assert released.status_code == 200, released.json()
    assert released.json()["status"] == "released"
    assert (
        _post(
            accounting_context,
            setup,
            doc,
            approve_loss=True,
            offset_account_id=str(setup["posting"]["cogs"]),
        ).status_code
        == 200
    )


@pytest.mark.concurrency
def test_inv003_reservation_race(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    docs = [_document(accounting_context, setup, "adjustment_out", "6") for _ in range(2)]
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def reserve(doc: dict) -> str:
        close_old_connections()
        try:
            reserve_stock(
                scope,
                {
                    "line_id": uuid.UUID(doc["lines"][0]["id"]),
                    "document_revision": 1,
                    "quantity": D(6),
                    "reservation_key": doc["id"],
                },
                key=doc["id"],
            )
            return "reserved"
        except (APIError, DatabaseError):
            return "rejected"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, docs)) == ["rejected", "reserved"]


def test_stock_post_failure_rolls_back(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    doc = _document(accounting_context, setup, "receipt")
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    with (
        patch("apps.inventory.stock_commands._line", side_effect=DatabaseError("injected")),
        pytest.raises(DatabaseError),
    ):
        post_document(
            scope,
            uuid.UUID(doc["id"]),
            {
                "journal_id": accounting_context["journal"],
                "fiscal_period_id": accounting_context["period"],
                "offset_account_id": setup["posting"]["cogs"],
            },
            revision=1,
            key="failure",
        )
    with connection.cursor() as c:
        c.execute(
            "SELECT status,journal_entry_id FROM erp.inventory_documents WHERE id=%s", [doc["id"]]
        )
        assert c.fetchone() == ("draft", None)
        c.execute(
            "SELECT count(*) FROM erp.stock_movements WHERE inventory_document_line_id=%s",
            [doc["lines"][0]["id"]],
        )
        assert c.fetchone()[0] == 0


def test_inv008_deferred_invoice_shipments_are_linked_and_capped(accounting_context: dict) -> None:
    from tests.api.test_sales_invoice_posting_api import _create_calculated_invoice

    setup = _stock_setup(accounting_context)
    invoice = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], "DEFERRED"
    )
    posted = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/invoices/{invoice['id']}/post",
        _posting(accounting_context)
        | {"stock_fulfillment": "deferred", "journal_id": str(setup["posting"]["journal"])},
        invoice["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    with connection.cursor() as c:
        c.execute("SELECT count(*) FROM erp.stock_movements WHERE source_id=%s", [invoice["id"]])
        assert c.fetchone()[0] == 0
    doc = _document(
        accounting_context, setup, "shipment", sales_invoice_line_id=posted.json()["lines"][0]["id"]
    )
    data = _posting(accounting_context)
    url = f"{_root(accounting_context)}/inventory/documents/{doc['id']}/post"
    shipment = _command(setup["client"], url, data, 1, "ship")
    assert shipment.status_code == 200, shipment.json()
    assert _command(setup["client"], url, data, 1, "ship").json() == shipment.json()
    extra = _document(
        accounting_context, setup, "shipment", sales_invoice_line_id=posted.json()["lines"][0]["id"]
    )
    assert _post(accounting_context, setup, extra).status_code == 409
    with connection.cursor() as c:
        c.execute(
            "SELECT sum(-quantity_delta) FROM erp.stock_movements "
            "WHERE source_id=%s AND source_type='sales_invoice'",
            [invoice["id"]],
        )
        assert c.fetchone()[0] == D(2)


@pytest.mark.concurrency
def test_concurrent_shipments_cannot_over_fulfill_invoice(accounting_context: dict) -> None:
    from tests.api.test_sales_invoice_posting_api import _create_calculated_invoice

    setup = _stock_setup(accounting_context)
    invoice = _create_calculated_invoice(setup["client"], accounting_context, setup["ids"], "RACE")
    posted = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/invoices/{invoice['id']}/post",
        _posting(accounting_context)
        | {"stock_fulfillment": "deferred", "journal_id": str(setup["posting"]["journal"])},
        invoice["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    docs = [
        _document(
            accounting_context,
            setup,
            "shipment",
            sales_invoice_line_id=posted.json()["lines"][0]["id"],
        )
        for _ in range(2)
    ]
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def post(doc: dict) -> str:
        close_old_connections()
        try:
            post_document(
                scope,
                uuid.UUID(doc["id"]),
                {
                    "journal_id": accounting_context["journal"],
                    "fiscal_period_id": accounting_context["period"],
                },
                revision=1,
                key=doc["id"],
            )
            return "posted"
        except APIError:
            return "rejected"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(post, docs)) == ["posted", "rejected"]
    with connection.cursor() as c:
        c.execute(
            "SELECT sum(-quantity_delta) FROM erp.stock_movements "
            "WHERE source_type='sales_invoice' AND source_id=%s",
            [invoice["id"]],
        )
        assert c.fetchone()[0] == D(2)


def test_stock_draft_revision_void_and_history(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    doc = _document(accounting_context, setup, "adjustment_out")
    url = f"{root}/inventory/documents/{doc['id']}"
    changed = setup["client"].patch(
        url,
        {"reason": "Count corrected"},
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="edit",
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["row_version"] == 2
    assert (
        setup["client"]
        .patch(
            url,
            {"reason": "stale"},
            format="json",
            HTTP_IF_MATCH='"1"',
            HTTP_IDEMPOTENCY_KEY="stale",
        )
        .status_code
        == 412
    )
    reservation = setup["client"].post(
        f"{root}/inventory/reservations",
        {
            "line_id": doc["lines"][0]["id"],
            "document_revision": 2,
            "quantity": "2",
            "reservation_key": "void",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="reserve",
    )
    assert reservation.status_code == 201, reservation.json()
    voided = _command(setup["client"], f"{url}/void", {}, 2, "void")
    assert voided.status_code == 200, voided.json()
    assert voided.json()["status"] == "void"
    assert _command(setup["client"], f"{url}/void", {}, 2, "void").json() == voided.json()
    with connection.cursor() as c:
        c.execute(
            "SELECT status FROM erp.inventory_reservations WHERE id=%s", [reservation.json()["id"]]
        )
        assert c.fetchone()[0] == "released"
    assert setup["client"].get(f"{root}/inventory/availability").status_code == 200
    assert setup["client"].get(f"{root}/inventory/availability?as_of=2026-09-27").status_code == 400
    history = setup["client"].get(f"{root}/inventory/availability?as_of=2026-09-27T23:59:59Z")
    assert history.status_code == 200, history.json()
    assert history.json()["historical_on_hand_only"] is True


def test_serial_receipt_cannot_duplicate_unit(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    with connection.cursor() as c:
        c.execute(
            "UPDATE erp.items SET track_lots=false,track_serials=true WHERE id=%s",
            [setup["ids"]["item"]],
        )
    lot = setup["client"].post(
        f"{root}/inventory/lots",
        {"item_id": str(setup["ids"]["item"]), "lot_code": "SER-1", "serial_code": "ONE"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="serial",
    )
    assert lot.status_code == 201, lot.json()
    for index in range(2):
        doc = _document(accounting_context, setup, "receipt", "1", lot_id=lot.json()["id"])
        response = _post(
            accounting_context, setup, doc, offset_account_id=str(setup["posting"]["cogs"])
        )
        assert response.status_code == (200 if index == 0 else 409), response.json()


@pytest.mark.security
def test_stock_commands_scope_permissions_and_module_modes(accounting_context: dict) -> None:
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient

    from tests.api.test_purchase_bill_drafts_api import _change_module

    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    doc = _document(accounting_context, setup, "receipt")
    assert setup["client"].get(f"{root}/inventory/documents/{uuid.uuid4()}").status_code == 404
    member = get_user_model().objects.create_user(email=f"stock-{uuid.uuid4()}@example.com")
    with connection.cursor() as c:
        c.execute(
            "INSERT INTO identity.tenant_memberships(tenant_id,user_id,tenant_role) "
            "VALUES (%s,%s,'member')",
            [accounting_context["tenant"], member.pk],
        )
        c.execute(
            "INSERT INTO identity.company_memberships(tenant_id,company_id,user_id) "
            "VALUES (%s,%s,%s)",
            [accounting_context["tenant"], accounting_context["company"], member.pk],
        )
    denied = APIClient()
    denied.force_login(member)
    assert denied.get(f"{root}/inventory/documents").status_code == 403
    assert (
        _command(
            denied, f"{root}/inventory/documents/{doc['id']}/post", _posting(accounting_context), 1
        ).status_code
        == 403
    )
    _change_module(setup["client"], accounting_context, "inventory", "read_only")
    assert setup["client"].get(f"{root}/inventory/documents").status_code == 200
    assert (
        _post(
            accounting_context, setup, doc, offset_account_id=str(setup["posting"]["cogs"])
        ).status_code
        == 403
    )


@pytest.mark.security
def test_inventory_runtime_audit_and_rebuild_without_audit_dml_grants(
    accounting_context: dict,
) -> None:
    from common.access.scopes import resolve_product_id

    _stock_setup(accounting_context)
    product_id = resolve_product_id()
    # All test-only grants/context are rolled back; deployment privileges are untouched.
    with transaction.atomic(), connection.cursor() as c:
        c.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        c.execute("GRANT SELECT ON erp.companies TO quickaccounts_runtime")
        c.execute("GRANT SELECT,INSERT ON erp.inventory_documents TO quickaccounts_runtime")
        c.execute(
            "GRANT EXECUTE ON FUNCTION erp.rebuild_inventory_positions(uuid,uuid) "
            "TO quickaccounts_runtime"
        )
        c.execute(
            "REVOKE INSERT,UPDATE,DELETE ON erp.company_audit_events FROM quickaccounts_runtime"
        )
        c.execute(
            "SELECT set_config('app.tenant_id',%s,true),set_config('app.user_id',%s,true)",
            [str(accounting_context["tenant"]), str(accounting_context["user"])],
        )
        c.execute("SET LOCAL ROLE quickaccounts_runtime")
        c.execute(
            "INSERT INTO erp.inventory_documents(company_id,document_no,document_kind,"
            "document_date) "
            "VALUES (%s,'RUNTIME','receipt','2026-09-27')",
            [accounting_context["company"]],
        )
        c.execute(
            "SELECT erp.rebuild_inventory_positions(%s,%s)",
            [accounting_context["company"], product_id],
        )
        assert c.fetchone()[0] == 1
        c.execute("RESET ROLE")
        c.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE company_id=%s "
            "AND action='inventory.rebuilt'",
            [accounting_context["company"]],
        )
        assert c.fetchone()[0] == 1
        transaction.set_rollback(True)


def test_inv006_lot_validation_and_duplicate_codes(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    with connection.cursor() as c:
        c.execute("UPDATE erp.items SET track_lots=true WHERE id=%s", [setup["ids"]["item"]])
    payload = {"item_id": str(setup["ids"]["item"]), "lot_code": "LOT-1"}
    lot = setup["client"].post(
        f"{root}/inventory/lots", payload, format="json", HTTP_IDEMPOTENCY_KEY="lot"
    )
    assert lot.status_code == 201, lot.json()
    assert (
        setup["client"]
        .post(f"{root}/inventory/lots", payload, format="json", HTTP_IDEMPOTENCY_KEY="other")
        .status_code
        == 409
    )
    # Missing lot is rejected before draft/stock effects commit.
    response = setup["client"].post(
        f"{root}/inventory/documents",
        {
            "document_no": "BAD-LOT",
            "document_kind": "receipt",
            "document_date": "2026-09-27",
            "lines": [
                {
                    "item_id": payload["item_id"],
                    "quantity": "1",
                    "to_warehouse_id": str(setup["warehouse"]),
                }
            ],
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="bad-lot",
    )
    assert response.status_code == 409
    doc = _document(accounting_context, setup, "receipt", lot_id=lot.json()["id"])
    assert (
        _post(
            accounting_context, setup, doc, offset_account_id=str(setup["posting"]["cogs"])
        ).status_code
        == 200
    )
