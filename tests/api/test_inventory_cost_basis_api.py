import concurrent.futures
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.stock_commands import post_document
from common.access.scopes import CompanyScope
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _command, _posting, _stock_setup
from tests.api.test_stock_commands_api import _document, _post

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _transfer(context: dict, setup: dict) -> dict:
    warehouse = (
        setup["client"]
        .post(
            f"{_root(context)}/inventory/warehouses",
            {"code": "COST", "name": "Cost destination"},
            format="json",
        )
        .json()
    )
    return _document(context, setup, "transfer", to_warehouse_id=warehouse["id"])


def test_versioned_cost_basis_replay_reads_and_immutability(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    root = _root(accounting_context)
    assert setup["client"].get(f"{root}/inventory/cost-policy").json()["configured"] is False
    doc = _transfer(accounting_context, setup)
    url = f"{root}/inventory/documents/{doc['id']}/post"
    posted = _command(setup["client"], url, _posting(accounting_context), 1, "cost")
    assert posted.status_code == 200, posted.json()
    assert (
        _command(setup["client"], url, _posting(accounting_context), 1, "cost").json()
        == posted.json()
    )
    basis = posted.json()["cost_basis"]
    assert len(basis) == 1
    snapshot = basis[0]
    assert D(snapshot["basis_quantity"]) == 8
    assert D(snapshot["basis_value_company"]) == 800
    assert D(snapshot["issue_quantity"]) == 2
    assert D(snapshot["issue_value_company"]) == 200
    assert D(snapshot["unit_cost_company"]) == 100
    policy = setup["client"].get(f"{root}/inventory/cost-policy").json()["policy"]
    assert policy["version"] == 1
    assert policy["method"] == "scope_average"
    assert policy["rounding_policy"] == "reject_fractional_gl"
    assert policy["id"] == snapshot["policy_id"]
    read = setup["client"].get(f"{root}/inventory/movements/{snapshot['movement_id']}/cost-basis")
    assert read.status_code == 200, read.json()
    assert read.json()["snapshot"]["policy_version"] == 1
    assert (
        setup["client"].get(f"{root}/inventory/movements/{uuid.uuid4()}/cost-basis").status_code
        == 404
    )
    for table, object_id in [
        ("inventory_cost_policies", policy["id"]),
        ("inventory_cost_basis_snapshots", snapshot["id"]),
    ]:
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as c:
            c.execute(f"DELETE FROM erp.{table} WHERE id=%s", [object_id])
    with connection.cursor() as c:
        c.execute(
            "SELECT id FROM erp.stock_movements WHERE source_id=%s AND source_type='sales_invoice'",
            [setup["source"]["id"]],
        )
        legacy_movement = c.fetchone()[0]
    assert (
        setup["client"]
        .get(f"{root}/inventory/movements/{legacy_movement}/cost-basis")
        .json()["available"]
        is False
    )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as c:
        c.execute(
            "INSERT INTO erp.inventory_cost_basis_snapshots(company_id,movement_id,policy_id,"
            "currency_code,currency_precision,basis_quantity,basis_value_company,reserved_quantity,"
            "issue_quantity,issue_value_company,unit_cost_company) "
            "VALUES (%s,%s,%s,'USD',2,8,900,0,2,200,100)",
            [accounting_context["company"], legacy_movement, policy["id"]],
        )


@pytest.mark.parametrize("failure", ["raised", "missing"])
def test_cost_snapshot_failure_rolls_back_policy_ledger_and_receipt(
    accounting_context: dict,
    failure: str,
) -> None:
    setup = _stock_setup(accounting_context)
    doc = _document(accounting_context, setup, "adjustment_out")
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    with (
        patch(
            "apps.inventory.stock_commands.record_cost_basis",
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
            uuid.UUID(doc["id"]),
            {
                "journal_id": accounting_context["journal"],
                "fiscal_period_id": accounting_context["period"],
                "offset_account_id": setup["posting"]["cogs"],
                "approve_loss": True,
            },
            revision=1,
            key="failure",
        )
    with connection.cursor() as c:
        c.execute("SELECT status FROM erp.inventory_documents WHERE id=%s", [doc["id"]])
        assert c.fetchone()[0] == "draft"
        c.execute(
            "SELECT count(*) FROM erp.inventory_cost_policies WHERE company_id=%s",
            [scope.company_id],
        )
        assert c.fetchone()[0] == 0
        c.execute(
            "SELECT count(*) FROM erp.stock_movements WHERE inventory_document_line_id=%s",
            [doc["lines"][0]["id"]],
        )
        assert c.fetchone()[0] == 0
        c.execute("SELECT count(*) FROM erp.journal_entries WHERE source_id=%s", [doc["id"]])
        assert c.fetchone()[0] == 0


@pytest.mark.security
def test_cost_basis_rls_hides_other_tenant_context(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    assert _post(accounting_context, setup, _transfer(accounting_context, setup)).status_code == 200
    with transaction.atomic(), connection.cursor() as c:
        c.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        c.execute(
            "GRANT SELECT ON erp.companies,erp.inventory_cost_policies,"
            "erp.inventory_cost_basis_snapshots TO quickaccounts_runtime"
        )
        c.execute("SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])])
        c.execute("SET LOCAL ROLE quickaccounts_runtime")
        c.execute(
            "SELECT count(*) FROM erp.inventory_cost_basis_snapshots WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert c.fetchone()[0] == 1
        c.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        c.execute(
            "SELECT count(*) FROM erp.inventory_cost_basis_snapshots WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert c.fetchone()[0] == 0
        transaction.set_rollback(True)


@pytest.mark.concurrency
def test_policy_creation_and_cost_bases_serialize_across_two_documents(
    accounting_context: dict,
) -> None:
    setup = _stock_setup(accounting_context)
    first = _transfer(accounting_context, setup)
    second = _document(
        accounting_context, setup, "transfer", to_warehouse_id=first["lines"][0]["to_warehouse_id"]
    )
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def post(doc: dict) -> dict:
        close_old_connections()
        try:
            return post_document(
                scope,
                uuid.UUID(doc["id"]),
                {
                    "journal_id": accounting_context["journal"],
                    "fiscal_period_id": accounting_context["period"],
                },
                revision=1,
                key=doc["id"],
            )
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(post, [first, second]))
    assert len({row["cost_basis"][0]["policy_id"] for row in results}) == 1
    assert sorted(D(row["cost_basis"][0]["basis_quantity"]) for row in results) == [D(6), D(8)]
    with connection.cursor() as c:
        c.execute(
            "SELECT count(*) FROM erp.inventory_cost_policies WHERE company_id=%s",
            [scope.company_id],
        )
        assert c.fetchone()[0] == 1
