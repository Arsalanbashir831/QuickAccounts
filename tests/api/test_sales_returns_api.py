import concurrent.futures
import datetime as dt
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction
from rest_framework.test import APIClient

from apps.sales.return_services import post_return, refund_return
from common.access.scopes import CompanyScope
from common.api.errors import APIError
from tests.api.test_payments_api import _allocate, _create, _post, _setup
from tests.api.test_purchase_bill_drafts_api import _bind_license, _change_module, _root
from tests.api.test_sales_invoice_drafts_api import _seed_sales_facts
from tests.api.test_sales_invoice_posting_api import (
    _create_calculated_invoice,
    _seed_posting_configuration,
)

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _command(client: APIClient, url: str, data: dict, revision: int, key: str | None = None):
    return client.post(
        url,
        data,
        format="json",
        HTTP_IF_MATCH=f'"{revision}"',
        HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()),
    )


def _draft(
    context: dict, setup: dict, *, physical: bool = False, amount: str = "22", quantity: str = "1"
) -> dict:
    response = _command(
        setup["client"],
        f"{_root(context)}/sales/invoices/{setup['source']['id']}/returns",
        {
            "return_no": f"RET-{uuid.uuid4()}",
            "kind": "return" if physical else "cashback",
            "return_date": "2026-09-27",
            "reason": "Customer return" if physical else "Price reduction",
            "lines": [
                {
                    "sales_invoice_line_id": setup["source"]["lines"][0]["id"],
                    **({"quantity": quantity} if physical else {"credit_amount": amount}),
                }
            ],
        },
        1,
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _posting(context: dict) -> dict:
    return {"journal_id": str(context["journal"]), "fiscal_period_id": str(context["period"])}


def _quality_approved(
    context: dict, setup: dict, document: dict, warehouse_id: str, quantity: str = "1"
) -> dict:
    root = f"{_root(context)}/inventory/repairs"
    response = _command(
        setup["client"],
        root,
        {
            "line_id": document["lines"][0]["id"],
            "warehouse_id": warehouse_id,
            "quantity": quantity,
            "diagnosis": "Inspect and restore returned item",
            "estimated_cost": "10",
        },
        document["row_version"],
    )
    assert response.status_code == 201, response.json()
    job = response.json()
    for status in ("in_progress", "repaired", "qc_passed"):
        response = _command(
            setup["client"],
            f"{root}/{job['id']}",
            {"status": status, "qc_notes": "Inspected, safe for resale"},
            job["row_version"],
        )
        assert response.status_code == 200, response.json()
        job = response.json()
    return job


def _paid(context: dict, setup: dict) -> dict:
    amount = setup["source"]["gross_total"]
    payment = _create(context, setup, amount)
    allocation = _allocate(context, setup, payment, amount)
    assert allocation.status_code == 200, allocation.json()
    response = _post(context, setup, allocation.json())
    assert response.status_code == 200, response.json()
    return response.json()


def test_cashback_partial_refund_idempotency_immutability_and_aging(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    payment = _paid(accounting_context, setup)
    document = _draft(accounting_context, setup)
    url = f"{_root(accounting_context)}/sales/returns/{document['id']}"
    posted = _command(
        setup["client"], f"{url}/post", _posting(accounting_context), 1, "return-post"
    )
    assert posted.status_code == 200, posted.json()
    result = posted.json()
    assert D(result["credit_total"]) == 22
    assert D(result["lines"][0]["credit_tax"]) == 2
    assert D(result["lines"][0]["credit_net"]) == 20
    assert (
        _command(
            setup["client"], f"{url}/post", _posting(accounting_context), 1, "return-post"
        ).json()
        == result
    )
    data = _posting(accounting_context) | {
        "receipt_payment_id": payment["id"],
        "cash_account_id": str(accounting_context["cash"]),
        "amount": "10",
        "refund_date": "2026-09-27",
        "refund_method": "cash",
    }
    refund = _command(setup["client"], f"{url}/refund", data, result["row_version"], "refund")
    assert refund.status_code == 200, refund.json()
    assert D(refund.json()["remaining_credit"]) == 12
    assert (
        _command(setup["client"], f"{url}/refund", data, result["row_version"], "refund").json()
        == refund.json()
    )
    invalid = _command(
        setup["client"], f"{url}/refund", data | {"amount": "13"}, result["row_version"]
    )
    assert invalid.status_code == 409
    assert (
        setup["client"]
        .patch(
            url, {"reason": "Changed"}, format="json", HTTP_IF_MATCH=f'"{result["row_version"]}"'
        )
        .status_code
        == 409
    )
    report = setup["client"].get(
        f"{_root(accounting_context)}/reports/open-receivables?cutoff={setup['cutoff']}"
    )
    assert report.status_code == 200, report.json()
    assert sum(D(row["open_amount"]) for row in report.json()["results"]) == -12
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.stock_movements WHERE company_id=%s AND "
            "source_type='sales_return'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM erp.customer_refunds WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 1
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.sales_return_lines SET credit_net=0 WHERE company_id=%s AND "
            "sales_return_id=%s",
            [accounting_context["company"], document["id"]],
        )


def test_unpaid_cashback_reduces_remaining_invoice_and_blocks_overallocation(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    document = _draft(accounting_context, setup)
    posted = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/returns/{document['id']}/post",
        _posting(accounting_context),
        1,
    )
    assert posted.status_code == 200, posted.json()
    assert D(posted.json()["remaining_credit"]) == 0
    assert D(posted.json()["credit_applications"][0]["amount"]) == 22
    payment = _create(accounting_context, setup, "100")
    assert _allocate(accounting_context, setup, payment, "100").status_code == 409
    report = (
        setup["client"]
        .get(f"{_root(accounting_context)}/reports/open-receivables?cutoff={setup['cutoff']}")
        .json()
    )
    assert sum(D(row["open_amount"]) for row in report["results"]) == D("82.50")


def _stock_setup(context: dict) -> dict:
    client = context["client"]
    _bind_license(context, ("module.sales", "module.inventory", "module.payments"))
    for module in ("sales", "inventory", "payments"):
        _change_module(client, context, module, "enabled")
    ids = _seed_sales_facts(context)
    posting = _seed_posting_configuration(context, ids)
    warehouse = uuid.uuid4()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.document_sequences(company_id,fiscal_period_id,sequence_code,"
            "prefix,next_value,padding_length) VALUES (%s,%s,'PAYMENT_JOURNAL','PAY-',1,6)",
            [context["company"], context["period"]],
        )
        cursor.execute("UPDATE erp.items SET item_kind='stock' WHERE id=%s", [ids["item"]])
        cursor.execute(
            "UPDATE erp.item_accounting_profiles SET "
            "inventory_account_id=%s,cogs_account_id=%s WHERE company_id=%s AND item_id=%s",
            [posting["inventory"], posting["cogs"], context["company"], ids["item"]],
        )
        cursor.execute(
            "INSERT INTO erp.warehouses(id,company_id,code,name) VALUES (%s,%s,'MAIN','Main')",
            [warehouse, context["company"]],
        )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(context["tenant"])])
        cursor.execute(
            "INSERT INTO "
            "erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item"
            "_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company"
            ") VALUES (%s,'opening','2020-01-01',%s,%s,'receipt',10,100,1000)",
            [context["company"], warehouse, ids["item"]],
        )
    draft = _create_calculated_invoice(client, context, ids, f"DRAFT-{uuid.uuid4()}")
    posted = _command(
        client,
        f"{_root(context)}/sales/invoices/{draft['id']}/post",
        {
            "journal_id": str(posting["journal"]),
            "fiscal_period_id": str(context["period"]),
            "warehouse_id": str(warehouse),
        },
        draft["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    return {
        "client": client,
        "ids": ids,
        "source": posted.json(),
        "posting": posting,
        "warehouse": warehouse,
        "direction": "receipt",
    }


def _inspect_stock(context: dict, setup: dict, document: dict) -> dict:
    response = _command(
        setup["client"],
        f"{_root(context)}/sales/returns/{document['id']}/inspect",
        {
            "lines": [
                {
                    "line_id": line["id"],
                    "received_quantity": line["quantity"],
                    "condition": "resellable",
                    "disposition": "restock",
                    "warehouse_id": str(setup["warehouse"]),
                }
                for line in document["lines"]
            ],
        },
        document["row_version"],
    )
    assert response.status_code == 200, response.json()
    return response.json()


@pytest.mark.parametrize("price,difference", [("25", "-55"), ("50", "0"), ("75", "55")])
@pytest.mark.parametrize("adopted", [False, True])
def test_atomic_replacement_price_difference_and_stock(
    accounting_context: dict, price: str, difference: str, adopted: bool
) -> None:
    from tests.api.test_sales_invoice_drafts_api import _payload

    setup = _stock_setup(accounting_context)
    if adopted:
        from tests.api.test_inventory_cost_layers_api import _adopt

        _adopt(accounting_context, setup)
    original_receipt = _paid(accounting_context, setup)
    document = _inspect_stock(
        accounting_context, setup, _draft(accounting_context, setup, physical=True, quantity="2")
    )
    url = f"{_root(accounting_context)}/sales/returns/{document['id']}"
    posted = _command(
        setup["client"], f"{url}/post", _posting(accounting_context), document["row_version"]
    )
    assert posted.status_code == 200, posted.json()
    payload = _payload(setup["ids"], f"DRAFT-{uuid.uuid4()}")
    payload["lines"][0]["unit_price"] = price
    invoice_url = f"{_root(accounting_context)}/sales/invoices"
    replacement = setup["client"].post(invoice_url, payload, format="json").json()
    calculated = _command(
        setup["client"],
        f"{invoice_url}/{replacement['id']}/calculate",
        {},
        replacement["row_version"],
    )
    assert calculated.status_code == 200, calculated.json()
    command = _posting(accounting_context) | {
        "journal_id": str(setup["posting"]["journal"]),
        "replacement_invoice_id": replacement["id"],
        "replacement_revision": calculated.json()["row_version"],
        "warehouse_id": str(setup["warehouse"]),
    }
    result = _command(
        setup["client"], f"{url}/replace", command, posted.json()["row_version"], "replacement"
    )
    assert result.status_code == 200, result.json()
    settlement = result.json()["replacement_settlement"]
    assert D(settlement["collect_amount"]) == max(D(difference), D(0))
    assert D(settlement["refund_or_credit"]) == max(-D(difference), D(0))
    assert (
        _command(
            setup["client"], f"{url}/replace", command, posted.json()["row_version"], "replacement"
        ).json()
        == result.json()
    )
    balance = (
        setup["client"]
        .get(f"{_root(accounting_context)}/inventory/warehouses/{setup['warehouse']}/balances")
        .json()["results"][0]
    )
    assert D(balance["on_hand_quantity"]) == 8
    if D(difference) < 0:
        refunded = _command(
            setup["client"],
            f"{url}/refund",
            _posting(accounting_context)
            | {
                "receipt_payment_id": original_receipt["id"],
                "cash_account_id": str(accounting_context["cash"]),
                "amount": str(-D(difference)),
                "refund_date": "2026-09-27",
                "refund_method": "cash",
            },
            posted.json()["row_version"],
        )
        assert refunded.status_code == 200, refunded.json()
        assert D(refunded.json()["remaining_credit"]) == 0
    elif D(difference) > 0:
        payment = _create(accounting_context, setup, difference)
        allocated = _allocate(accounting_context, setup, payment, difference, replacement["id"])
        assert allocated.status_code == 200, allocated.json()
        collected = _post(accounting_context, setup, allocated.json())
        assert collected.status_code == 200, collected.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(debit_amount-credit_amount) FROM erp.journal_lines WHERE "
            "company_id=%s AND account_id=%s",
            [accounting_context["company"], setup["posting"]["receivable"]],
        )
        assert cursor.fetchone()[0] == 0
    if adopted:
        from apps.inventory.cost_reconciliation import cost_reconciliation
        from apps.inventory.stock_commands import rebuild_positions

        reconciled = cost_reconciliation(accounting_context["company"])
        assert reconciled["matches"] is True
        rebuilt = rebuild_positions(
            CompanyScope(
                accounting_context["tenant"],
                accounting_context["company"],
                accounting_context["user"],
            ),
            key="replacement-cost-rebuild",
        )
        assert rebuilt["cost_history_verified"] is True
        assert cost_reconciliation(accounting_context["company"]) == reconciled


def test_repeated_partial_returns_and_dead_stock_report(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    for _ in range(2):
        document = _inspect_stock(
            accounting_context, setup, _draft(accounting_context, setup, physical=True)
        )
        posted = _command(
            setup["client"],
            f"{_root(accounting_context)}/sales/returns/{document['id']}/post",
            _posting(accounting_context),
            document["row_version"],
        )
        assert posted.status_code == 200, posted.json()
    excessive = _inspect_stock(
        accounting_context, setup, _draft(accounting_context, setup, physical=True)
    )
    failed = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/returns/{excessive['id']}/post",
        _posting(accounting_context),
        excessive["row_version"],
    )
    assert failed.status_code == 409
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.warehouses(company_id,code,name,stock_category) VALUES "
            "(%s,'OLD','Old damaged','damaged') RETURNING id",
            [accounting_context["company"]],
        )
        old = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO "
            "erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,m"
            "ovement_kind,quantity_delta,unit_cost_company,value_delta_company) VALUES "
            "(%s,'old-opening','2020-01-01',%s,%s,'receipt',1,100,100)",
            [accounting_context["company"], old, setup["ids"]["item"]],
        )
    response = setup["client"].get(
        f"{_root(accounting_context)}/reports/dead-stock?inactive_days=365"
    )
    assert response.status_code == 200, response.json()
    rows = [r for r in response.json()["results"] if r["warehouse_id"] == str(old)]
    assert len(rows) == 1
    assert D(rows[0]["value_company"]) == 100
    assert response.json()["automatic_write_off"] is False
    assert setup["client"].get(f"{_root(accounting_context)}/reports/dead-stock").status_code == 400


@pytest.mark.parametrize(
    "disposition,category",
    [
        ("restock", "sellable"),
        ("quarantine", "quarantine"),
        ("damaged", "damaged"),
        ("supplier_return", "supplier_return"),
        ("write_off", "damaged"),
    ],
)
@pytest.mark.parametrize("adopted", [False, True])
def test_physical_inspection_disposition_and_historical_cost(
    accounting_context: dict, disposition: str, category: str, adopted: bool
) -> None:
    setup = _stock_setup(accounting_context)
    document = _draft(accounting_context, setup, physical=True)
    url = f"{_root(accounting_context)}/sales/returns/{document['id']}"
    assert (
        _command(setup["client"], f"{url}/post", _posting(accounting_context), 1).status_code == 409
    )
    dest = (
        setup["client"]
        .post(
            f"{_root(accounting_context)}/inventory/warehouses",
            {"code": "RET", "name": "Return stock", "stock_category": category},
            format="json",
        )
        .json()
    )
    inspection = {
        "lines": [
            {
                "line_id": document["lines"][0]["id"],
                "received_quantity": "1",
                "condition": "resellable" if disposition == "restock" else "damaged",
                "disposition": disposition,
                "warehouse_id": dest["id"],
                **(
                    {"approve_write_off": True, "loss_account_id": str(setup["posting"]["cogs"])}
                    if disposition == "write_off"
                    else {}
                ),
            }
        ]
    }
    inspected = _command(setup["client"], f"{url}/inspect", inspection, 1)
    assert inspected.status_code == 200, inspected.json()
    posted = _command(
        setup["client"],
        f"{url}/post",
        _posting(accounting_context),
        inspected.json()["row_version"],
    )
    assert posted.status_code == 200, posted.json()
    assert D(posted.json()["lines"][0]["historical_cost"]) == 100
    assert D(posted.json()["credit_total"]) == D("52.25")
    balances = (
        setup["client"]
        .get(f"{_root(accounting_context)}/inventory/warehouses/{dest['id']}/balances")
        .json()["results"][0]
    )
    assert D(balances["on_hand_quantity"]) == (0 if disposition == "write_off" else 1)
    assert D(balances["value_company"]) == (0 if disposition == "write_off" else 100)
    if adopted:
        from tests.api.test_inventory_cost_layers_api import _adopt

        _adopt(accounting_context, setup | {"warehouse": uuid.UUID(dest["id"])})
    assert D(balances["available_quantity"]) == (1 if disposition == "restock" else 0)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(debit_amount),sum(credit_amount) FROM erp.journal_lines WHERE "
            "company_id=%s AND journal_entry_id=%s",
            [accounting_context["company"], posted.json()["journal_entry_id"]],
        )
        debit, credit = cursor.fetchone()
        assert debit == credit

    if disposition in {"quarantine", "damaged", "supplier_return"}:
        job = _quality_approved(accounting_context, setup, posted.json(), dest["id"])
        # Release half without changing value, then explicitly write off the remainder.
        data = _posting(accounting_context) | {
            "line_id": document["lines"][0]["id"],
            "from_warehouse_id": dest["id"],
            "to_warehouse_id": str(setup["warehouse"]),
            "quantity": "0.5",
            "action_date": str(dt.datetime.now(dt.UTC).date()),
            "reason": "Inspection approved release",
            "repair_job_id": job["id"],
        }
        revision = posted.json()["row_version"]
        moved = _command(setup["client"], f"{url}/stock-dispositions", data, revision, "release")
        assert moved.status_code == 200, moved.json()
        assert D(moved.json()["historical_cost"]) == 50
        assert moved.json()["journal_entry_id"] is None
        assert (
            _command(setup["client"], f"{url}/stock-dispositions", data, revision, "release").json()
            == moved.json()
        )
        loss = {k: v for k, v in data.items() if k not in {"to_warehouse_id", "repair_job_id"}}
        loss |= {"approve_write_off": True, "loss_account_id": str(setup["posting"]["cogs"])}
        written = _command(setup["client"], f"{url}/stock-dispositions", loss, revision)
        assert written.status_code == 200, written.json()
        assert D(written.json()["historical_cost"]) == 50
        assert written.json()["journal_entry_id"] is not None
        assert (
            _command(setup["client"], f"{url}/stock-dispositions", loss, revision).status_code
            == 409
        )


def test_return_journal_failure_rolls_back_every_effect(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    document = _draft(accounting_context, setup)
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    with (
        patch("apps.sales.return_services._line", side_effect=DatabaseError("injected")),
        pytest.raises(DatabaseError),
    ):
        post_return(
            scope,
            uuid.UUID(document["id"]),
            {
                "fiscal_period_id": accounting_context["period"],
                "journal_id": accounting_context["journal"],
            },
            revision=1,
            key="fail",
            request_id=None,
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,credit_total FROM erp.sales_returns WHERE id=%s", [document["id"]]
        )
        assert cursor.fetchone() == ("draft", D(0))
        cursor.execute(
            "SELECT count(*) FROM erp.journal_entries WHERE source_type='sales_return' AND "
            "source_id=%s",
            [document["id"]],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM erp.sales_return_tax_components WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.concurrency
def test_refunds_cannot_double_spend_credit(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    payment = _paid(accounting_context, setup)
    document = _draft(accounting_context, setup)
    posted = _command(
        setup["client"],
        f"{_root(accounting_context)}/sales/returns/{document['id']}/post",
        _posting(accounting_context),
        1,
    ).json()
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )
    data = {
        "fiscal_period_id": accounting_context["period"],
        "journal_id": accounting_context["journal"],
        "receipt_payment_id": uuid.UUID(payment["id"]),
        "cash_account_id": accounting_context["cash"],
        "amount": D(15),
        "refund_date": dt.date(2026, 9, 27),
        "refund_method": "cash",
    }

    def refund(key: str) -> str:
        close_old_connections()
        try:
            refund_return(
                scope,
                uuid.UUID(document["id"]),
                data,
                revision=posted["row_version"],
                key=key,
                request_id=None,
            )
            return "posted"
        except APIError:
            return "rejected"
        finally:
            close_old_connections()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(refund, ["a", "b"]))
    assert sorted(results) == ["posted", "rejected"]


@pytest.mark.concurrency
def test_physical_returns_cannot_double_consume_sale(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    documents = [
        _inspect_stock(
            accounting_context,
            setup,
            _draft(accounting_context, setup, physical=True, quantity="2"),
        )
        for _ in range(2)
    ]
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def post(document: dict) -> str:
        close_old_connections()
        try:
            post_return(
                scope,
                uuid.UUID(document["id"]),
                {
                    "fiscal_period_id": accounting_context["period"],
                    "journal_id": accounting_context["journal"],
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
        results = list(executor.map(post, documents))
    assert sorted(results) == ["posted", "rejected"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(quantity_delta),sum(value_delta_company) FROM erp.stock_movements "
            "WHERE company_id=%s AND source_type='sales_return'",
            [accounting_context["company"]],
        )
        assert cursor.fetchone() == (D(2), D(200))
