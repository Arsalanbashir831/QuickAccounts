import concurrent.futures
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.cost_checkpoints import create_cost_checkpoint
from common.access.scopes import CompanyScope
from common.api.errors import Conflict
from tests.api.test_purchase_bill_drafts_api import _root
from tests.api.test_sales_returns_api import _stock_setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def test_checkpoint_adopts_balance_without_rewriting_history(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    url = f"{_root(accounting_context)}/inventory/cost-checkpoints"
    data = {
        "warehouse_id": str(setup["warehouse"]),
        "item_id": str(setup["ids"]["item"]),
        "reason": "Reviewed current balance",
    }
    response = setup["client"].post(url, data, format="json", HTTP_IDEMPOTENCY_KEY="adopt")
    assert response.status_code == 201, response.json()
    checkpoint = response.json()
    assert Decimal(checkpoint["quantity"]) == 8
    assert Decimal(checkpoint["value_company"]) == 800
    assert checkpoint["movement_count"] == 2
    assert (
        setup["client"].post(url, data, format="json", HTTP_IDEMPOTENCY_KEY="adopt").json()
        == checkpoint
    )
    assert (
        setup["client"].post(url, data, format="json", HTTP_IDEMPOTENCY_KEY="again").status_code
        == 409
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_checkpoint_movements WHERE checkpoint_id=%s",
            [checkpoint["id"]],
        )
        assert cursor.fetchone()[0] == 2
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_allocations WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0
    for table in ("inventory_cost_checkpoints", "inventory_cost_checkpoint_movements"):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                f"DELETE FROM erp.{table} WHERE company_id=%s", [accounting_context["company"]]
            )
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.inventory_cost_checkpoint_movements "
            "SELECT company_id,checkpoint_id,%s "
            "FROM erp.inventory_cost_checkpoint_movements LIMIT 1",
            [uuid.uuid4()],
        )


def test_checkpoint_missing_members_rolls_back_everything(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    with (
        patch("apps.inventory.cost_checkpoints._run", return_value=None),
        pytest.raises(DatabaseError),
    ):
        create_cost_checkpoint(
            scope,
            {
                "warehouse_id": setup["warehouse"],
                "item_id": setup["ids"]["item"],
                "reason": "Reviewed",
            },
            key="missing-members",
        )
    with connection.cursor() as cursor:
        for table in ("inventory_cost_checkpoints", "inventory_cost_policies"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0


def test_checkpoint_concurrent_adoption_has_one_winner(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    data = {
        "warehouse_id": setup["warehouse"],
        "item_id": setup["ids"]["item"],
        "reason": "Reviewed",
    }

    def run(key: str) -> str:
        close_old_connections()
        try:
            create_cost_checkpoint(scope, data, key=key)
            return "created"
        except Conflict as exc:
            return exc.default_code
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["first", "second"]))
    assert sorted(results) == ["STOCK_COST_CHECKPOINT_EXISTS", "created"]


def test_checkpoint_rejects_projection_drift(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.inventory_positions SET value_company=801 WHERE company_id=%s",
            [accounting_context["company"]],
        )
    response = setup["client"].post(
        f"{_root(accounting_context)}/inventory/cost-checkpoints",
        {
            "warehouse_id": str(setup["warehouse"]),
            "item_id": str(setup["ids"]["item"]),
            "reason": "Reviewed",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="drift",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "STOCK_CHECKPOINT_DRIFT"


@pytest.mark.security
def test_checkpoint_rls_hides_both_tables(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    create_cost_checkpoint(
        scope,
        {"warehouse_id": setup["warehouse"], "item_id": setup["ids"]["item"], "reason": "Reviewed"},
        key="rls",
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute(
            "GRANT SELECT ON erp.companies,erp.inventory_cost_checkpoints,"
            "erp.inventory_cost_checkpoint_movements TO quickaccounts_runtime"
        )
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        for table, expected in [
            ("inventory_cost_checkpoints", 1),
            ("inventory_cost_checkpoint_movements", 2),
        ]:
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == expected
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        for table in ("inventory_cost_checkpoints", "inventory_cost_checkpoint_movements"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0
        transaction.set_rollback(True)
