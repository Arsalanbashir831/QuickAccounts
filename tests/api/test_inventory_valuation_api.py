import concurrent.futures
import datetime as dt
import threading
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection, transaction

from apps.inventory.cost_basis import record_cost_basis
from apps.purchasing.services import post_purchase_bill
from common.access.scopes import CompanyScope
from tests.api.test_accounting_api import _balanced_entry_payload, _entry_url
from tests.api.test_purchase_bill_drafts_api import _change_module, _root
from tests.api.test_sales_returns_api import _command, _stock_setup
from tests.api.test_stock_commands_api import _document, _post
from tests.api.test_supplier_return_costing_api import _setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _report(context: dict, setup: dict, *, date: str | None = None, limit: int = 200):
    return setup["client"].get(
        f"{_root(context)}/reports/inventory-valuation-reconciliation",
        {"as_of": date or dt.datetime.now(dt.UTC).date().isoformat(), "limit": limit},
    )


def test_valuation_matches_physical_supplier_credit_and_is_read_only(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    before = _report(accounting_context, setup)
    assert before.status_code == 200, before.json()
    assert before.json()["matches"] is True
    assert D(before.json()["ledger_value_company"]) == 100
    assert D(before.json()["gl_value_company"]) == 100
    posted = _command(setup["client"], setup["url"], setup["payload"], 1)
    assert posted.status_code == 200, posted.json()
    after = _report(accounting_context, setup)
    assert after.status_code == 200, after.json()
    assert after.json()["matches"] is True
    assert D(after.json()["ledger_value_company"]) == 0
    assert D(after.json()["gl_value_company"]) == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_layers WHERE company_id=%s",
            [accounting_context["company"]],
        )
        count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        events = cursor.fetchone()[0]
    assert _report(accounting_context, setup).json() == after.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.inventory_cost_layers WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == count
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == events


def test_valuation_transfer_is_value_neutral_and_truncation_does_not_hide_totals(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    warehouse = (
        setup["client"]
        .post(
            f"{_root(accounting_context)}/inventory/warehouses",
            {"code": "OTHER", "name": "Other"},
            format="json",
        )
        .json()
    )
    document = _document(accounting_context, setup, "transfer", to_warehouse_id=warehouse["id"])
    posted = _post(accounting_context, setup, document)
    assert posted.status_code == 200, posted.json()
    report = _report(accounting_context, setup, limit=1)
    assert report.status_code == 200, report.json()
    result = report.json()
    assert result["matches"] is True
    assert result["truncated"] is True
    assert len(result["valuation"]) == 1
    assert result["scope_count"] == 2
    assert D(result["ledger_value_company"]) == D(result["gl_value_company"]) == 100


def test_legacy_stock_is_unattributed_not_falsely_reconciled(accounting_context: dict) -> None:
    setup = _stock_setup(accounting_context)
    response = _report(accounting_context, setup)
    assert response.status_code == 200, response.json()
    result = response.json()
    assert result["matches"] is False
    assert result["unattributed_movement_count"] == 1
    assert result["costing_coverage_complete"] is False
    assert any(row["code"] == "UNATTRIBUTED_MOVEMENT" for row in result["diagnostics"])


def test_valuation_snapshots_survive_profile_change_and_historical_cutoff(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.item_accounting_profiles SET inventory_account_id=%s "
            "WHERE company_id=%s AND item_id=%s",
            [setup["posting"]["tax"], accounting_context["company"], setup["ids"]["item"]],
        )
    result = _report(accounting_context, setup).json()
    # Retained stock stays attributed to the original inventory account. The new
    # mapping also exposes an existing balance on that account, rather than hiding it.
    inventory = next(
        row for row in result["accounts"] if row["account_id"] == str(setup["posting"]["inventory"])
    )
    assert D(inventory["stock_value_company"]) == 100
    assert D(inventory["difference_company"]) == 0
    prior = _report(accounting_context, setup, date="2019-12-31")
    assert prior.status_code == 200, prior.json()
    assert D(prior.json()["ledger_value_company"]) == 0
    assert D(prior.json()["gl_value_company"]) == 0
    assert prior.json()["matches"] is True


@pytest.mark.parametrize(
    "query", [{}, {"as_of": "bad"}, {"as_of": "9999-12-31"}, {"as_of": "2026-09-27", "limit": 201}]
)
def test_valuation_validates_required_cutoff_and_bounds(
    accounting_context: dict, query: dict
) -> None:
    setup = _setup(accounting_context)
    assert (
        setup["client"]
        .get(f"{_root(accounting_context)}/reports/inventory-valuation-reconciliation", query)
        .status_code
        == 400
    )


def test_valuation_company_and_module_gates(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    hidden = setup["client"].get(
        f"/api/v1/tenants/{accounting_context['tenant']}/companies/{uuid.uuid4()}/reports/inventory-valuation-reconciliation",
        {"as_of": "2026-09-27"},
    )
    assert hidden.status_code == 404
    _change_module(setup["client"], accounting_context, "inventory", "read_only")
    assert _report(accounting_context, setup).status_code == 200
    _change_module(setup["client"], accounting_context, "inventory", "disabled")
    assert _report(accounting_context, setup).status_code == 403


def test_valuation_reads_use_tenant_rls(accounting_context: dict) -> None:
    from apps.inventory.valuation import valuation_reconciliation

    setup = _setup(accounting_context)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp TO quickaccounts_runtime")
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        result = valuation_reconciliation(
            accounting_context["company"], as_of=dt.datetime.now(dt.UTC).date(), limit=200
        )
        assert result["scope_count"] == 0 and result["account_count"] == 0
        assert D(result["ledger_value_company"]) == D(result["gl_value_company"]) == 0
        transaction.set_rollback(True)
    assert _report(accounting_context, setup).json()["matches"] is True


def test_offsetting_manual_journals_do_not_create_false_match(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    entries = []
    for reverse in (False, True):
        payload = _balanced_entry_payload(accounting_context)
        accounts = [setup["posting"]["inventory"], setup["posting"]["purchase"]]
        if reverse:
            accounts.reverse()
        for line, account in zip(payload["lines"], accounts, strict=True):
            line["account_id"] = str(account)
        created = setup["client"].post(_entry_url(accounting_context), payload, format="json")
        assert created.status_code == 201, created.json()
        entries.append(created.json())
    assert _report(accounting_context, setup).json()["matches"] is True  # Draft GL is excluded.
    for entry in entries:
        posted = _command(
            setup["client"], f"{_entry_url(accounting_context)}/{entry['id']}/post", {}, 1
        )
        assert posted.status_code == 200, posted.json()
    report = _report(accounting_context, setup).json()
    assert D(report["ledger_value_company"]) == D(report["gl_value_company"]) == 100
    assert report["account_difference_count"] == 0
    assert report["journal_difference_count"] == 2
    assert report["financial_matches"] is False
    assert report["matches"] is False


def test_valuation_flags_stock_and_journal_effective_date_difference(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT entry_date FROM erp.journal_entries WHERE company_id=%s "
            "AND source_type='purchase_bill' AND status='posted'",
            [accounting_context["company"]],
        )
        journal_date = cursor.fetchone()[0]
    if journal_date >= dt.datetime.now(dt.UTC).date():
        pytest.skip("Fixture must be backdated relative to stock posting.")
    report = _report(accounting_context, setup, date=journal_date.isoformat()).json()
    assert D(report["ledger_value_company"]) == 0
    assert D(report["gl_value_company"]) == 100
    assert report["matches"] is False
    assert report["journal_difference_count"] == 1


def test_large_inventory_history_never_returns_partial_reconciliation(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,"
            "movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
            "SELECT %s,'bounded-history-'||n,clock_timestamp(),%s,%s,'receipt',1,0,0 "
            "FROM generate_series(1,10000) n",
            [accounting_context["company"], setup["warehouse"], setup["ids"]["item"]],
        )
    report = _report(accounting_context, setup)
    assert report.status_code == 409, report.json()
    assert report.json()["error"]["code"] == "INVENTORY_REPORT_JOB_REQUIRED"
    assert "matches" not in report.json()


@pytest.mark.concurrency
def test_report_never_observes_half_posted_stock_and_gl(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    started, release = threading.Event(), threading.Event()
    scope = CompanyScope(
        tenant_id=accounting_context["tenant"],
        company_id=accounting_context["company"],
        user_id=accounting_context["user"],
    )

    def pause(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10)
        return record_cost_basis(*args, **kwargs)

    def post():
        close_old_connections()
        try:
            return post_purchase_bill(
                scope,
                uuid.UUID(setup["credit"]["id"]),
                {
                    "journal_id": setup["posting"]["journal"],
                    "fiscal_period_id": accounting_context["period"],
                    "warehouse_id": setup["warehouse"],
                },
                expected_revision=1,
                idempotency_key=str(uuid.uuid4()),
                request_id=None,
            )
        finally:
            close_old_connections()

    with patch("apps.inventory.supplier_returns.record_cost_basis", side_effect=pause):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(post)
            try:
                assert started.wait(timeout=10)
                report = _report(accounting_context, setup)
                assert report.status_code == 200, report.json()
                assert report.json()["matches"] is True
                assert (
                    D(report.json()["ledger_value_company"])
                    == D(report.json()["gl_value_company"])
                    == 100
                )
            finally:
                release.set()
            assert future.result(timeout=10)[1] == 200
    after = _report(accounting_context, setup).json()
    assert after["matches"] is True
    assert D(after["ledger_value_company"]) == D(after["gl_value_company"]) == 0
