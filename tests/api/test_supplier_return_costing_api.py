import concurrent.futures
import uuid
from importlib import import_module
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.purchasing.services import post_purchase_bill
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_inventory_cost_layers_api import _adopt
from tests.api.test_purchase_bill_drafts_api import (
    _bind_license,
    _change_module,
    _enable_purchasing,
    _root,
    _seed_posting_configuration,
    _seed_purchase_facts,
)
from tests.api.test_purchase_bill_posting_api import _create_calculated_bill, _post_payload
from tests.api.test_sales_returns_api import _command

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _setup(
    context: dict, *, legacy: str | None = None, partial_quantity: str | None = None
) -> dict:
    client = context["client"]
    _enable_purchasing(client, context)
    _bind_license(context, ("module.purchasing", "module.inventory"))
    _change_module(client, context, "inventory", "enabled")
    ids = _seed_purchase_facts(context)
    posting = _seed_posting_configuration(context, ids)
    warehouse = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute("UPDATE erp.items SET item_kind='stock' WHERE id=%s", [ids["item"]])
        cursor.execute(
            "UPDATE erp.item_accounting_profiles SET inventory_account_id=%s "
            "WHERE company_id=%s AND item_id=%s",
            [posting["inventory"], context["company"], ids["item"]],
        )
        cursor.execute(
            "INSERT INTO erp.warehouses(id,company_id,code,name) VALUES (%s,%s,'MAIN','Main')",
            [warehouse, context["company"]],
        )
    draft = _create_calculated_bill(client, context, ids, "DRAFT-ORIGINAL")
    payload = _post_payload(context, posting) | {"warehouse_id": str(warehouse)}
    base = f"{_root(context)}/purchasing/bills"
    posted = _command(client, f"{base}/{draft['id']}/post", payload, draft["row_version"])
    assert posted.status_code == 200, posted.json()
    if legacy:
        quantity, cost, value = (2, 100, 200) if legacy == "variance" else (-1, 50, -50)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(context["tenant"])])
            cursor.execute(
                "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,"
                "warehouse_id,item_id,movement_kind,quantity_delta,unit_cost_company,"
                "value_delta_company) VALUES (%s,%s,clock_timestamp(),%s,%s,%s,%s,%s,%s)",
                [
                    context["company"],
                    str(uuid.uuid4()),
                    warehouse,
                    ids["item"],
                    "receipt" if quantity > 0 else "issue",
                    quantity,
                    cost,
                    value,
                ],
            )
    _adopt(context, {"warehouse": warehouse, "ids": ids})
    credit = client.post(
        f"{base}/{posted.json()['id']}/credit-notes",
        {
            "bill_no": "DRAFT-RETURN",
            "bill_date": "2026-09-28",
            "source_line_ids": [posted.json()["lines"][0]["id"]],
            **({"partial_quantities": [partial_quantity]} if partial_quantity else {}),
        },
        format="json",
    )
    assert credit.status_code == 201, credit.json()
    return {
        "client": client,
        "ids": ids,
        "posting": posting,
        "warehouse": warehouse,
        "credit": credit.json(),
        "payload": payload,
        "url": f"{base}/{credit.json()['id']}/post",
    }


@pytest.mark.parametrize(
    "failure", ["variance", "unavailable", "allocations", "missing", "effects"]
)
def test_supplier_return_rejects_invalid_costing_without_partial_effects(
    accounting_context: dict, failure: str
) -> None:
    setup = _setup(
        accounting_context, legacy=failure if failure in {"variance", "unavailable"} else None
    )
    if failure in {"allocations", "missing", "effects"}:
        options = (
            {"side_effect": DatabaseError("injected")}
            if failure == "allocations"
            else {"return_value": None}
        )
        target = (
            "apps.inventory.supplier_returns.issue_supplier_return"
            if failure == "effects"
            else "apps.inventory.supplier_returns.record_layer_uses"
        )
        with patch(target, **options):
            response = _command(setup["client"], setup["url"], setup["payload"], 1)
    else:
        response = _command(setup["client"], setup["url"], setup["payload"], 1)
    assert response.status_code == 409, response.json()
    if failure == "variance":
        assert response.json()["error"]["code"] == "SUPPLIER_RETURN_VARIANCE_POLICY_REQUIRED"
    if failure == "unavailable":
        assert response.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    with connection.cursor() as cursor:
        for table in ("stock_movements", "journal_entries"):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s AND source_id=%s",
                [accounting_context["company"], setup["credit"]["id"]],
            )
            assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT status FROM erp.purchase_bills WHERE company_id=%s AND id=%s",
            [accounting_context["company"], setup["credit"]["id"]],
        )
        assert cursor.fetchone()[0] == "draft"
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_allocations WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0
    if failure in {"allocations", "missing", "effects"}:
        assert _command(setup["client"], setup["url"], setup["payload"], 1).status_code == 200


def test_concurrent_supplier_return_posts_once_and_retains_allocations(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def post(_: int) -> str:
        close_old_connections()
        try:
            post_purchase_bill(
                scope,
                uuid.UUID(setup["credit"]["id"]),
                {
                    "warehouse_id": setup["warehouse"],
                    "journal_id": setup["posting"]["journal"],
                    "fiscal_period_id": accounting_context["period"],
                },
                expected_revision=1,
                idempotency_key=str(uuid.uuid4()),
                request_id=None,
            )
            return "posted"
        except APIError:
            return "rejected"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(post, range(2))) == ["posted", "rejected"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*),sum(quantity),sum(value_company) "
            "FROM erp.inventory_cost_allocations WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone() == (1, 2, 100)
    with (
        pytest.raises(DatabaseError, match="Cannot reverse retained physical"),
        transaction.atomic(),
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                import_module("apps.database.migrations.0029_physical_supplier_returns").REVERSE
            )
