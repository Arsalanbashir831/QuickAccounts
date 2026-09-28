import concurrent.futures
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.stock_commands import post_document
from apps.sales.services import post_sales_invoice
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_inventory_cost_basis_api import _transfer
from tests.api.test_inventory_cost_layers_api import _adopt
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_invoice_posting_api import _create_calculated_invoice
from tests.api.test_sales_returns_api import _command, _posting, _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("failure", ["missing", "raised"])
def test_immediate_costing_failure_rolls_back_sales_and_gl(
    accounting_context: dict, failure: str
) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    invoice = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], "COST-FAIL"
    )
    with patch(
        "apps.sales.services.record_layer_uses",
        **(
            {"return_value": None}
            if failure == "missing"
            else {"side_effect": DatabaseError("injected")}
        ),
    ):
        response = _command(
            setup["client"],
            f"{_root(accounting_context)}/sales/invoices/{invoice['id']}/post",
            _posting(accounting_context)
            | {
                "journal_id": str(setup["posting"]["journal"]),
                "warehouse_id": str(setup["warehouse"]),
            },
            invoice["row_version"],
            "cost-failure",
        )
    assert response.status_code == 409, response.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,journal_entry_id FROM erp.sales_invoices WHERE id=%s", [invoice["id"]]
        )
        assert cursor.fetchone() == ("draft", None)
        for table in ("journal_entries", "stock_movements"):
            cursor.execute(f"SELECT count(*) FROM erp.{table} WHERE source_id=%s", [invoice["id"]])
            assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_layers WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions "
            "WHERE company_id=%s AND warehouse_id=%s",
            [accounting_context["company"], setup["warehouse"]],
        )
        assert cursor.fetchone() == (D(8), D(800))


@pytest.mark.concurrency
def test_immediate_sale_and_typed_transfer_share_layer_locking(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    invoice = _create_calculated_invoice(
        setup["client"], accounting_context, setup["ids"], "COST-RACE"
    )
    document = _transfer(accounting_context, setup)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def run(kind: str) -> str:
        close_old_connections()
        try:
            if kind == "sale":
                post_sales_invoice(
                    scope,
                    uuid.UUID(invoice["id"]),
                    _posting(accounting_context)
                    | {
                        "journal_id": setup["posting"]["journal"],
                        "warehouse_id": setup["warehouse"],
                    },
                    expected_revision=invoice["row_version"],
                    idempotency_key="sale-race",
                    request_id=None,
                )
            else:
                post_document(
                    scope,
                    uuid.UUID(document["id"]),
                    _posting(accounting_context),
                    revision=1,
                    key="transfer-race",
                )
            return "posted"
        except APIError as exc:
            return exc.default_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ["sale", "transfer"])) == ["posted", "posted"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT basis_layer_quantity FROM erp.inventory_cost_allocations "
            "WHERE company_id=%s ORDER BY basis_layer_quantity",
            [accounting_context["company"]],
        )
        assert cursor.fetchall() == [(D(6),), (D(8),)]


def test_cost_basis_cannot_forge_a_future_allocation_seal(accounting_context: dict) -> None:
    from tests.api.test_stock_commands_api import _post

    setup = _stock_setup(accounting_context)
    _adopt(accounting_context, setup)
    assert _post(accounting_context, setup, _transfer(accounting_context, setup)).status_code == 200
    with (
        pytest.raises(DatabaseError, match="seal must match"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "INSERT INTO erp.inventory_cost_basis_snapshots(company_id,movement_id,policy_id,"
            "currency_code,currency_precision,basis_quantity,basis_value_company,reserved_quantity,"
            "issue_quantity,issue_value_company,unit_cost_company,allocation_seal_xid) "
            "SELECT company_id,movement_id,policy_id,currency_code,currency_precision,"
            "basis_quantity,"
            "basis_value_company,reserved_quantity,issue_quantity,issue_value_company,unit_cost_company,"
            "'0'::xid8 FROM erp.inventory_cost_basis_snapshots WHERE company_id=%s",
            [accounting_context["company"]],
        )
