import concurrent.futures
import uuid
from decimal import Decimal as D
from importlib import import_module
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.cost_checkpoints import create_cost_checkpoint
from apps.inventory.stock_commands import post_document
from common.access.scopes import CompanyScope
from tests.api.test_inventory_cost_basis_api import _transfer
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command, _posting, _stock_setup
from tests.api.test_stock_commands_api import _document, _post

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _adopt(context: dict, setup: dict) -> dict:
    return create_cost_checkpoint(
        CompanyScope(
            tenant_id=context["tenant"], company_id=context["company"], user_id=context["user"]
        ),
        {"warehouse_id": setup["warehouse"], "item_id": setup["ids"]["item"], "reason": "Reviewed"},
        key=str(uuid.uuid4()),
    )


def test_inv009_average_layers_receipts_full_depletion_replay_and_immutability(
    accounting_context: dict,
) -> None:
    setup = _stock_setup(accounting_context)
    checkpoint = _adopt(accounting_context, setup)
    receipt = _document(accounting_context, setup, "receipt", unit_cost_company="200")
    changed = setup["client"].patch(
        f"{_root(accounting_context)}/inventory/documents/{receipt['id']}",
        {
            "lines": [
                {
                    "item_id": str(setup["ids"]["item"]),
                    "quantity": "2",
                    "to_warehouse_id": str(setup["warehouse"]),
                    "unit_cost_company": "200",
                }
            ]
        },
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="mixed-cost-receipt",
    )
    assert changed.status_code == 200, changed.json()
    receipt = changed.json()
    assert (
        _post(
            accounting_context, setup, receipt, offset_account_id=str(setup["posting"]["cogs"])
        ).status_code
        == 200
    )
    transfer = _transfer(accounting_context, setup)
    destination = transfer["lines"][0]["to_warehouse_id"]
    document = _document(
        accounting_context, setup, "transfer", quantity="5", to_warehouse_id=destination
    )
    url = f"{_root(accounting_context)}/inventory/documents/{document['id']}/post"
    response = _command(setup["client"], url, _posting(accounting_context), 1, "layer-post")
    assert response.status_code == 200, response.json()
    assert (
        _command(setup["client"], url, _posting(accounting_context), 1, "layer-post").json()
        == response.json()
    )
    allocations = response.json()["cost_allocations"]
    assert len(allocations) == 2
    opening = next(a for a in allocations if a["opening_checkpoint_id"])
    incoming = next(a for a in allocations if a["receipt_movement_id"])
    assert opening["opening_checkpoint_id"] == checkpoint["id"]
    assert opening["cost_layer_id"] == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"inventory-layer:{accounting_context['company']}:checkpoint:{checkpoint['id']}",
        )
    )
    assert incoming["cost_layer_id"] == str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"inventory-layer:{accounting_context['company']}:receipt:{incoming['receipt_movement_id']}",
        )
    )
    assert (D(opening["quantity"]), D(opening["value_company"])) == (D(4), D(400))
    assert (D(incoming["quantity"]), D(incoming["value_company"])) == (D(1), D(200))
    drain = _document(
        accounting_context, setup, "transfer", quantity="5", to_warehouse_id=destination
    )
    drained = _post(accounting_context, setup, drain)
    assert drained.status_code == 200, drained.json()
    assert sum(D(a["value_company"]) for a in drained.json()["cost_allocations"]) == 600
    for table in ("inventory_cost_layers", "inventory_cost_allocations"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM erp.{table} WHERE company_id=%s", [accounting_context["company"]]
            )
    # Even a new allocation cannot be appended to a sealed issue after posting.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.inventory_cost_allocations"
            "(company_id,issue_movement_id,receipt_movement_id,"
            "opening_checkpoint_id,cost_layer_id,quantity,value_company,unit_cost_company,"
            "basis_layer_quantity,basis_layer_value,preceding_layer_value) "
            "SELECT company_id,issue_movement_id,receipt_movement_id,"
            "opening_checkpoint_id,cost_layer_id,"
            "quantity,value_company,unit_cost_company,basis_layer_quantity,basis_layer_value,"
            "preceding_layer_value FROM erp.inventory_cost_allocations WHERE id=%s",
            [opening["id"]],
        )


@pytest.mark.parametrize("failure", ["raised", "missing"])
def test_layer_allocation_failure_rolls_back_entire_post(
    accounting_context: dict, failure: str
) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    document = _document(accounting_context, setup, "adjustment_out")
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    with (
        patch(
            "apps.inventory.stock_commands.record_layer_uses",
            **(
                {"side_effect": DatabaseError("injected")}
                if failure == "raised"
                else {"return_value": None}
            ),
        ),
        pytest.raises(DatabaseError),
    ):
        post_document(
            scope,
            uuid.UUID(document["id"]),
            _posting(accounting_context)
            | {"offset_account_id": setup["posting"]["cogs"], "approve_loss": True},
            revision=1,
            key="layer-failure",
        )
    with connection.cursor() as cursor:
        for table in ("inventory_cost_layers", "inventory_cost_allocations"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT status FROM erp.inventory_documents WHERE id=%s", [document["id"]])
        assert cursor.fetchone()[0] == "draft"
        for table in ("stock_movements", "journal_entries"):
            cursor.execute(f"SELECT count(*) FROM erp.{table} WHERE source_id=%s", [document["id"]])
            assert cursor.fetchone()[0] == 0


@pytest.mark.concurrency
def test_concurrent_layer_consumption_serializes(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    first = _transfer(accounting_context, setup)
    second = _document(
        accounting_context, setup, "transfer", to_warehouse_id=first["lines"][0]["to_warehouse_id"]
    )
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def run(document: dict) -> dict:
        close_old_connections()
        try:
            return post_document(
                scope,
                uuid.UUID(document["id"]),
                _posting(accounting_context),
                revision=1,
                key=str(uuid.uuid4()),
            )
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        posted = list(pool.map(run, [first, second]))
    assert sorted(D(doc["cost_allocations"][0]["basis_layer_quantity"]) for doc in posted) == [
        D(6),
        D(8),
    ]
    assert sum(D(doc["cost_allocations"][0]["value_company"]) for doc in posted) == 400


def test_fractional_layer_quantity_fails_without_effects(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    receipt = _document(accounting_context, setup, "receipt", quantity="1")
    assert (
        _post(
            accounting_context, setup, receipt, offset_account_id=str(setup["posting"]["cogs"])
        ).status_code
        == 200
    )
    document = _transfer(accounting_context, setup)
    response = _post(accounting_context, setup, document)
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "STOCK_LAYER_ROUNDING_REQUIRED"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_layers WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.security
def test_layers_and_allocations_rls(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    assert _post(accounting_context, setup, _transfer(accounting_context, setup)).status_code == 200
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute(
            "GRANT SELECT ON erp.companies,erp.inventory_cost_layers,"
            "erp.inventory_cost_allocations TO quickaccounts_runtime"
        )
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        for table in ("inventory_cost_layers", "inventory_cost_allocations"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        for table in ("inventory_cost_layers", "inventory_cost_allocations"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0
        transaction.set_rollback(True)


def test_adopted_stock_cannot_bypass_cost_history(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    with (
        pytest.raises(DatabaseError, match="requires retained cost basis"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,"
            "movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
            "VALUES (%s,%s,clock_timestamp(),%s,%s,'issue',-1,100,-100)",
            [
                accounting_context["company"],
                str(uuid.uuid4()),
                setup["warehouse"],
                setup["ids"]["item"],
            ],
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s",
            [accounting_context["company"], setup["warehouse"]],
        )
        assert cursor.fetchone() == (D(8), D(800))


def test_adopted_invoice_immediate_and_deferred_costing(
    accounting_context: dict,
) -> None:
    from tests.api.test_sales_invoice_posting_api import _create_calculated_invoice

    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    invoice = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], "ADOPTED"
    )
    url = f"{_root(accounting_context)}/sales/invoices/{invoice['id']}/post"
    data = _posting(accounting_context) | {
        "journal_id": str(setup["posting"]["journal"]),
        "warehouse_id": str(setup["warehouse"]),
    }
    immediate = _command(setup["client"], url, data, invoice["row_version"], "immediate-cost")
    assert immediate.status_code == 200, immediate.json()
    assert (
        _command(setup["client"], url, data, invoice["row_version"], "immediate-cost").json()
        == immediate.json()
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(a.quantity),sum(a.value_company) FROM erp.inventory_cost_allocations a "
            "JOIN erp.stock_movements m ON m.company_id=a.company_id AND m.id=a.issue_movement_id "
            "WHERE m.company_id=%s AND m.source_type='sales_invoice' AND m.source_id=%s",
            [accounting_context["company"], invoice["id"]],
        )
        assert cursor.fetchone() == (D(2), D(200))
    invoice = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], "ADOPTED-DEFERRED"
    )
    url = f"{_root(accounting_context)}/sales/invoices/{invoice['id']}/post"
    deferred = _command(
        setup["client"],
        url,
        {k: v for k, v in data.items() if k != "warehouse_id"} | {"stock_fulfillment": "deferred"},
        invoice["row_version"],
    )
    assert deferred.status_code == 200, deferred.json()
    document = _document(
        accounting_context,
        setup,
        "shipment",
        sales_invoice_line_id=deferred.json()["lines"][0]["id"],
    )
    shipment = _post(accounting_context, setup, document)
    assert shipment.status_code == 200, shipment.json()
    assert D(shipment.json()["cost_allocations"][0]["value_company"]) == 200
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(debit_amount),sum(credit_amount) FROM erp.journal_lines "
            "WHERE company_id=%s AND journal_entry_id=%s",
            [accounting_context["company"], shipment.json()["journal_entry_id"]],
        )
        assert cursor.fetchone() == (D(200), D(200))


def test_repeated_lines_use_sequential_layer_residuals(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    document = _transfer(accounting_context, setup)
    changed = setup["client"].patch(
        f"{_root(accounting_context)}/inventory/documents/{document['id']}",
        {
            "lines": [
                {
                    "item_id": str(setup["ids"]["item"]),
                    "quantity": "2",
                    "from_warehouse_id": str(setup["warehouse"]),
                    "to_warehouse_id": document["lines"][0]["to_warehouse_id"],
                }
            ]
            * 2
        },
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_IDEMPOTENCY_KEY="multi-line",
    )
    assert changed.status_code == 200, changed.json()
    response = _post(accounting_context, setup, changed.json())
    assert response.status_code == 200, response.json()
    assert [D(a["basis_layer_quantity"]) for a in response.json()["cost_allocations"]] == [
        D(8),
        D(6),
    ]


def test_costing_migration_refuses_to_discard_posted_history(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    assert _post(accounting_context, setup, _transfer(accounting_context, setup)).status_code == 200
    reverse = import_module("apps.database.migrations.0025_inventory_cost_layers").REVERSE
    with (
        pytest.raises(DatabaseError, match="retained costing history"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(reverse)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_allocations WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 1
