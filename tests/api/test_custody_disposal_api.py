import uuid
from decimal import Decimal
from importlib import import_module

import pytest
from django.db import DatabaseError, connection, transaction

from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_return_workflow_extensions import _segregated
from tests.api.test_sales_invoice_drafts_api import _payload
from tests.api.test_sales_returns_api import _command, _draft, _posting, _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_new_migrations_reverse_forward(accounting_context):
    custody = import_module("apps.database.migrations.0035_return_custody_intake")
    disposal = import_module("apps.database.migrations.0036_stock_disposal_cases")
    order = import_module("apps.database.migrations.0037_sales_order_delivery")
    claim = import_module("apps.database.migrations.0038_supplier_claims")
    partial = import_module("apps.database.migrations.0039_partial_supplier_returns")
    transfer = import_module("apps.database.migrations.0040_supplier_transfer_provenance")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(transfer.REVERSE)
        cursor.execute(partial.reverse_sql())
        cursor.execute(claim.reverse_sql())
        cursor.execute(order.REVERSE)
        cursor.execute(disposal.reverse_sql())
        cursor.execute(custody.REVERSE)
        cursor.execute(custody.SQL)
        cursor.execute(disposal.SQL)
        cursor.execute(order.SQL)
        cursor.execute(claim.SQL)
        cursor.execute(partial.SQL)
        cursor.execute(transfer.SQL)
        transaction.set_rollback(True)


def _warehouse(context, setup, role, category):
    response = setup["client"].post(
        f"{_root(context)}/inventory/warehouses",
        {
            "code": str(uuid.uuid4()),
            "name": role,
            "operational_role": role,
            "stock_category": category,
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _intake(context, setup, warehouse, quantity="1"):
    response = _command(
        setup["client"],
        f"{_root(context)}/inventory/return-intakes",
        {
            "intake_no": str(uuid.uuid4()),
            "item_id": str(setup["ids"]["item"]),
            "warehouse_id": warehouse["id"],
            "quantity": quantity,
            "received_date": "2026-09-27",
            "reason": "Customer brought goods without invoice",
        },
        1,
    )
    assert response.status_code == 201, response.json()
    return response.json()


def test_unmatched_custody_is_nonfinancial_and_rejectable(accounting_context):
    setup = _stock_setup(accounting_context)
    warehouse = _warehouse(accounting_context, setup, "inspection", "quarantine")
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        journals = cursor.fetchone()[0]
    intake = _intake(accounting_context, setup, warehouse)
    assert not intake["owned_inventory"] and Decimal(intake["custody_quantity"]) == 1
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM erp.journal_entries")
        assert cursor.fetchone()[0] == journals
        cursor.execute(
            "SELECT count(*) FROM erp.stock_movements WHERE warehouse_id=%s", [warehouse["id"]]
        )
        assert cursor.fetchone()[0] == 0
    url = f"{_root(accounting_context)}/inventory/return-intakes/{intake['id']}"
    response = _command(setup["client"], url, {"status": "rejected"}, 1, "reject")
    assert response.status_code == 200, response.json()
    assert Decimal(response.json()["custody_quantity"]) == 0
    assert (
        _command(setup["client"], url, {"status": "rejected"}, 1, "reject").json()
        == response.json()
    )
    assert _command(setup["client"], url, {"status": "rejected"}, 2).status_code == 409


def test_custody_match_and_return_post_receives_owned_stock_once(accounting_context):
    setup = _stock_setup(accounting_context)
    warehouse = _warehouse(accounting_context, setup, "inspection", "quarantine")
    intake = _intake(accounting_context, setup, warehouse)
    url = f"{_root(accounting_context)}/inventory/return-intakes/{intake['id']}"
    response = _command(
        setup["client"],
        url,
        {
            "status": "inspected",
            "condition": "resellable",
            "requested_resolution": "credit",
            "inspection_notes": "Original SKU checked",
        },
        1,
    )
    assert response.status_code == 200, response.json()
    document = _draft(accounting_context, setup, physical=True)
    response = _command(
        setup["client"],
        url,
        {
            "status": "matched",
            "sales_return_line_id": document["lines"][0]["id"],
            "return_revision": document["row_version"],
        },
        2,
    )
    assert response.status_code == 200, response.json()
    return_url = f"{_root(accounting_context)}/sales/returns/{document['id']}"
    assert _command(setup["client"], f"{return_url}/void", {}, 1).status_code == 409
    response = _command(
        setup["client"],
        f"{return_url}/inspect",
        {
            "lines": [
                {
                    "line_id": document["lines"][0]["id"],
                    "received_quantity": "1",
                    "condition": "resellable",
                    "disposition": "quarantine",
                    "warehouse_id": warehouse["id"],
                }
            ]
        },
        1,
    )
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"],
        f"{return_url}/post",
        _posting(accounting_context),
        response.json()["row_version"],
    )
    assert response.status_code == 200, response.json()
    result = setup["client"].get(url).json()
    assert result["owned_inventory"] and Decimal(result["custody_quantity"]) == 0


def _case(context, setup, warehouse, method, return_line=None):
    body = {
        "case_no": str(uuid.uuid4()),
        "item_id": str(setup["ids"]["item"]),
        "warehouse_id": str(warehouse),
        "quantity": "1",
        "reason": "Disposition decision",
        "method": method,
    }
    if return_line:
        body["sales_return_line_id"] = return_line
    response = _command(setup["client"], f"{_root(context)}/inventory/disposal-cases", body, 1)
    assert response.status_code == 201, response.json()
    return response.json()


def test_ordinary_write_off_requires_approval_and_is_atomic(accounting_context):
    setup = _stock_setup(accounting_context)
    case = _case(accounting_context, setup, setup["warehouse"], "write_off")
    url = f"{_root(accounting_context)}/inventory/disposal-cases/{case['id']}"
    assert _command(setup["client"], url, {"action": "approve"}, 1).status_code == 409
    response = _command(setup["client"], url, {"action": "approve", "approve_loss": True}, 1)
    assert response.status_code == 200, response.json()
    body = _posting(accounting_context) | {
        "action": "execute",
        "action_date": "2026-09-27",
        "loss_account_id": str(setup["posting"]["cogs"]),
    }
    bad = _command(setup["client"], url, body | {"loss_account_id": str(uuid.uuid4())}, 2)
    assert bad.status_code in (404, 409), bad.json()
    assert setup["client"].get(url).json()["stock_document_id"] is None
    response = _command(setup["client"], url, body, 2, "execute")
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "executed"
    assert _command(setup["client"], url, body, 2, "execute").json() == response.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(quantity_delta) FROM erp.stock_movements WHERE warehouse_id=%s",
            [setup["warehouse"]],
        )
        assert cursor.fetchone()[0] == 7


def test_linked_return_disposal_moves_to_repair_without_loss(accounting_context):
    setup, document, warehouse, _ = _segregated(accounting_context)
    target = _warehouse(accounting_context, setup, "repair", "damaged")
    case = _case(
        accounting_context, setup, warehouse["id"], "repair_later", document["lines"][0]["id"]
    )
    url = f"{_root(accounting_context)}/inventory/disposal-cases/{case['id']}"
    response = _command(setup["client"], url, {"action": "approve"}, 1)
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"],
        url,
        _posting(accounting_context)
        | {"action": "execute", "action_date": "2026-09-27", "to_warehouse_id": target["id"]},
        2,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["return_stock_action_id"] and response.json()["status"] == "executed"


def test_dead_stock_clearance_requires_qc_and_matching_posted_sale(accounting_context):
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    dead = _warehouse(accounting_context, setup, "dead_stock", "damaged")
    clearance = _warehouse(accounting_context, setup, "clearance", "sellable")
    response = _command(
        setup["client"],
        f"{root}/inventory/documents",
        {
            "document_no": str(uuid.uuid4()),
            "document_kind": "transfer",
            "document_date": "2026-09-27",
            "reason": "Segregate dead stock",
            "lines": [
                {
                    "item_id": str(setup["ids"]["item"]),
                    "quantity": "1",
                    "from_warehouse_id": str(setup["warehouse"]),
                    "to_warehouse_id": dead["id"],
                }
            ],
        },
        1,
    )
    assert response.status_code == 201, response.json()
    stock = response.json()
    response = _command(
        setup["client"],
        f"{root}/inventory/documents/{stock['id']}/post",
        _posting(accounting_context),
        stock["row_version"],
    )
    assert response.status_code == 200, response.json()
    case = _case(accounting_context, setup, dead["id"], "liquidation")
    url = f"{root}/inventory/disposal-cases/{case['id']}"
    assert _command(setup["client"], url, {"action": "approve"}, 1).status_code == 409
    response = _command(
        setup["client"],
        url,
        {
            "action": "approve",
            "approve_quality_release": True,
            "qc_notes": "Verified safe for discounted sale",
        },
        1,
    )
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"],
        url,
        _posting(accounting_context)
        | {"action": "execute", "action_date": "2026-09-27", "to_warehouse_id": clearance["id"]},
        response.json()["row_version"],
    )
    assert response.status_code == 200, response.json()
    case = response.json()
    assert case["status"] == "executed" and case["stock_document_id"]
    assert (
        _command(
            setup["client"],
            url,
            {"action": "link-sale", "sales_invoice_id": setup["source"]["id"]},
            case["row_version"],
        ).status_code
        == 409
    )
    invoice_body = _payload(setup["ids"], str(uuid.uuid4()))
    invoice_body["lines"] = [
        {
            "item_id": str(setup["ids"]["item"]),
            "description": "Clearance stock",
            "quantity": "1",
            "unit_price": "25",
            "discount_amount": "0",
            "tax_code_id": str(setup["ids"]["tax_code"]),
        }
    ]
    response = setup["client"].post(f"{root}/sales/invoices", invoice_body, format="json")
    assert response.status_code == 201, response.json()
    invoice = response.json()
    response = _command(
        setup["client"],
        f"{root}/sales/invoices/{invoice['id']}/calculate",
        {},
        invoice["row_version"],
    )
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"],
        f"{root}/sales/invoices/{invoice['id']}/post",
        _posting(accounting_context)
        | {"journal_id": str(setup["posting"]["journal"]), "warehouse_id": clearance["id"]},
        response.json()["row_version"],
    )
    assert response.status_code == 200, response.json()
    response = _command(
        setup["client"],
        url,
        {"action": "link-sale", "sales_invoice_id": invoice["id"]},
        case["row_version"],
    )
    assert response.status_code == 200, response.json()
    assert response.json()["sales_invoice_id"] == invoice["id"]


def test_database_cannot_fake_disposal_completion(accounting_context):
    setup = _stock_setup(accounting_context)
    case = _case(accounting_context, setup, setup["warehouse"], "supplier_claim")
    url = f"{_root(accounting_context)}/inventory/disposal-cases/{case['id']}"
    assert _command(setup["client"], url, {"action": "approve"}, 1).status_code == 200
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.user_id',%s,true)", [str(accounting_context["user"])]
        )
        cursor.execute(
            "UPDATE erp.stock_disposal_cases SET status='executed' WHERE id=%s", [case["id"]]
        )
