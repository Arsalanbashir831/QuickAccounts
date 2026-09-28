import concurrent.futures
import datetime as dt
import uuid
from decimal import Decimal as D
from importlib import import_module
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.cost_reconciliation import cost_reconciliation
from apps.inventory.return_stock import dispose_return_stock
from apps.inventory.stock_commands import rebuild_positions
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_inventory_cost_layers_api import _adopt
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command, _draft, _posting, _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _returned(context: dict, *, ambiguous: bool = False) -> tuple[dict, dict, dict]:
    setup = _stock_setup(context)
    warehouse = (
        setup["client"]
        .post(
            f"{_root(context)}/inventory/warehouses",
            {"code": "DAM", "name": "Damaged", "stock_category": "damaged"},
            format="json",
        )
        .json()
    )
    draft = _draft(context, setup, physical=True)
    url = f"{_root(context)}/sales/returns/{draft['id']}"
    inspected = _command(
        setup["client"],
        f"{url}/inspect",
        {
            "lines": [
                {
                    "line_id": draft["lines"][0]["id"],
                    "received_quantity": "1",
                    "condition": "damaged",
                    "disposition": "damaged",
                    "warehouse_id": warehouse["id"],
                }
            ]
        },
        draft["row_version"],
    )
    assert inspected.status_code == 200, inspected.json()
    posted = _command(
        setup["client"], f"{url}/post", _posting(context), inspected.json()["row_version"]
    )
    assert posted.status_code == 200, posted.json()
    # Retained pre-adoption stock at a different cost must not reprice the return.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(context["tenant"])])
        cursor.execute(
            "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,"
            "item_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
            "VALUES (%s,%s,'2020-01-01',%s,%s,'receipt',1,200,200)",
            [context["company"], str(uuid.uuid4()), warehouse["id"], setup["ids"]["item"]],
        )
    if ambiguous:
        # A legacy unlinked loss cannot establish which receipt it consumed.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(context["tenant"])])
            cursor.execute(
                "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,"
                "item_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
                "VALUES (%s,%s,'2020-01-02',%s,%s,'issue',-0.5,100,-50)",
                [context["company"], str(uuid.uuid4()), warehouse["id"], setup["ids"]["item"]],
            )
    _adopt(context, setup | {"warehouse": uuid.UUID(warehouse["id"])})
    return setup, posted.json(), warehouse


def _dispose(context: dict, setup: dict, document: dict, warehouse: dict, quantity: str = "0.5"):
    return _command(
        setup["client"],
        f"{_root(context)}/sales/returns/{document['id']}/stock-dispositions",
        _posting(context)
        | {
            "line_id": document["lines"][0]["id"],
            "from_warehouse_id": warehouse["id"],
            "quantity": quantity,
            "action_date": "2026-09-27",
            "reason": "Approved loss",
            "approve_write_off": True,
            "loss_account_id": str(setup["posting"]["cogs"]),
        },
        document["row_version"],
    )


def test_linked_return_cost_is_not_mixed_warehouse_average(accounting_context: dict) -> None:
    setup, document, warehouse = _returned(accounting_context)
    response = _dispose(accounting_context, setup, document, warehouse)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert D(result["historical_cost"]) == 50  # Not the warehouse-average cost of 75.
    basis = result["cost_basis"]
    assert basis["method"] == "linked_return_history"
    assert D(basis["scope_value_company"]) == 300
    assert D(basis["basis_value_company"]) == 100
    assert "allocation_seal_xid" not in basis
    assert sum(D(row["value_company"]) for row in result["cost_allocations"]) == 50
    second = _dispose(accounting_context, setup, document, warehouse)
    assert second.status_code == 200, second.json()
    assert D(second.json()["historical_cost"]) == 50
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s",
            [accounting_context["company"], warehouse["id"]],
        )
        assert cursor.fetchone() == (D(1), D(200))
        cursor.execute(
            "SELECT sum(debit_amount),sum(credit_amount) FROM erp.journal_lines "
            "WHERE company_id=%s AND journal_entry_id=%s",
            [accounting_context["company"], result["journal_entry_id"]],
        )
        assert cursor.fetchone() == (D(50), D(50))
    for table in ("inventory_return_cost_basis", "inventory_cost_allocations"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM erp.{table} WHERE company_id=%s", [accounting_context["company"]]
            )


def test_return_cost_allocation_failure_rolls_back_action_and_journal(
    accounting_context: dict,
) -> None:
    setup, document, warehouse = _returned(accounting_context)
    with patch(
        "apps.inventory.return_costing.record_layer_uses", side_effect=DatabaseError("injected")
    ):
        response = _dispose(accounting_context, setup, document, warehouse)
    assert response.status_code == 409, response.json()
    with connection.cursor() as cursor:
        for table in (
            "sales_return_stock_actions",
            "inventory_return_cost_basis",
            "inventory_cost_layers",
        ):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s",
            [accounting_context["company"], warehouse["id"]],
        )
        assert cursor.fetchone() == (D(2), D(300))
    assert _dispose(accounting_context, setup, document, warehouse).status_code == 200


def test_concurrent_historical_dispositions_cannot_consume_same_return(
    accounting_context: dict,
) -> None:
    setup, document, warehouse = _returned(accounting_context)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def consume(_: int) -> str:
        close_old_connections()
        try:
            dispose_return_stock(
                scope,
                uuid.UUID(document["id"]),
                {
                    "line_id": uuid.UUID(document["lines"][0]["id"]),
                    "from_warehouse_id": uuid.UUID(warehouse["id"]),
                    "quantity": D(1),
                    "action_date": dt.date(2026, 9, 27),
                    "reason": "Approved loss",
                    "approve_write_off": True,
                    "loss_account_id": setup["posting"]["cogs"],
                    "journal_id": accounting_context["journal"],
                    "fiscal_period_id": accounting_context["period"],
                },
                revision=document["row_version"],
                key=str(uuid.uuid4()),
                request_id=None,
            )
            return "posted"
        except APIError:
            return "rejected"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(consume, range(2))) == ["posted", "rejected"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*),sum(issue_value_company) FROM erp.inventory_return_cost_basis "
            "WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone() == (1, D(100))


def test_ambiguous_checkpoint_ownership_requires_provenance(accounting_context: dict) -> None:
    setup, document, warehouse = _returned(accounting_context, ambiguous=True)
    response = _dispose(accounting_context, setup, document, warehouse)
    assert response.status_code == 409, response.json()
    assert "RETURN_COST_PROVENANCE_REQUIRED" in str(response.json())
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.sales_return_stock_actions WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.parametrize("fail_allocations", [False, True])
def test_inline_writeoff_retains_new_return_receipt_cost(
    accounting_context: dict, fail_allocations: bool
) -> None:
    setup, _, warehouse = _returned(accounting_context)
    draft = _draft(accounting_context, setup, physical=True)
    url = f"{_root(accounting_context)}/sales/returns/{draft['id']}"
    inspected = _command(
        setup["client"],
        f"{url}/inspect",
        {
            "lines": [
                {
                    "line_id": draft["lines"][0]["id"],
                    "received_quantity": "1",
                    "condition": "damaged",
                    "disposition": "write_off",
                    "warehouse_id": warehouse["id"],
                    "approve_write_off": True,
                    "loss_account_id": str(setup["posting"]["cogs"]),
                }
            ]
        },
        draft["row_version"],
    )
    assert inspected.status_code == 200, inspected.json()
    key = str(uuid.uuid4())
    if fail_allocations:
        with patch(
            "apps.inventory.return_costing.record_layer_uses", side_effect=DatabaseError("injected")
        ):
            failed = _command(
                setup["client"],
                f"{url}/post",
                _posting(accounting_context),
                inspected.json()["row_version"],
                key,
            )
        assert failed.status_code == 409, failed.json()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM erp.stock_movements WHERE company_id=%s AND source_id=%s",
                [accounting_context["company"], draft["id"]],
            )
            assert cursor.fetchone()[0] == 0
    posted = _command(
        setup["client"],
        f"{url}/post",
        _posting(accounting_context),
        inspected.json()["row_version"],
        key,
    )
    assert posted.status_code == 200, posted.json()
    assert (
        _command(
            setup["client"],
            f"{url}/post",
            _posting(accounting_context),
            inspected.json()["row_version"],
            key,
        ).json()
        == posted.json()
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT basis_quantity,basis_value_company,scope_value_company,"
            "issue_value_company,return_stock_action_id FROM erp.inventory_return_cost_basis "
            "WHERE company_id=%s AND return_line_id=%s",
            [accounting_context["company"], draft["lines"][0]["id"]],
        )
        assert cursor.fetchone() == (D(1), D(100), D(400), D(100), None)
        cursor.execute(
            "SELECT a.receipt_movement_id,m.source_line_id,a.value_company "
            "FROM erp.inventory_cost_allocations a JOIN erp.stock_movements m "
            "ON m.company_id=a.company_id AND m.id=a.receipt_movement_id "
            "JOIN erp.inventory_return_cost_basis b ON b.company_id=a.company_id "
            "AND b.id=a.historical_basis_id WHERE b.company_id=%s AND b.return_line_id=%s",
            [accounting_context["company"], draft["lines"][0]["id"]],
        )
        receipt, line, value = cursor.fetchone()
        assert receipt is not None and str(line) == draft["lines"][0]["id"] and value == D(100)
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s",
            [accounting_context["company"], warehouse["id"]],
        )
        assert cursor.fetchone() == (D(2), D(300))
    with pytest.raises(DatabaseError, match="Cannot reverse retained inline"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                import_module("apps.database.migrations.0028_inline_return_costing").REVERSE
            )
    reconciled = cost_reconciliation(accounting_context["company"])
    assert reconciled["cost_history_matches"] is True and reconciled["projection_matches"] is True
    rebuilt = rebuild_positions(
        CompanyScope(
            accounting_context["tenant"], accounting_context["company"], accounting_context["user"]
        ),
        key="inline-cost-rebuild",
    )
    assert rebuilt["cost_history_verified"] is True
    assert cost_reconciliation(accounting_context["company"]) == reconciled
