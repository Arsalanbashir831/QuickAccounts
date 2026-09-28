import concurrent.futures
import threading
import time
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.cost_basis import record_cost_basis
from apps.inventory.cost_reconciliation import cost_reconciliation
from apps.inventory.stock_commands import post_document, rebuild_positions
from apps.purchasing.services import post_purchase_bill
from common.access.scopes import CompanyScope
from common.api.errors import Conflict
from tests.api.test_inventory_cost_layers_api import _adopt
from tests.api.test_inventory_return_costing_api import _dispose, _returned
from tests.api.test_inventory_valuation_api import _report as _valuation
from tests.api.test_purchase_bill_drafts_api import _change_module, _root
from tests.api.test_sales_returns_api import _command, _stock_setup
from tests.api.test_stock_commands_api import _document, _post
from tests.api.test_supplier_return_costing_api import _setup

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _scope(context: dict) -> CompanyScope:
    return CompanyScope(context["tenant"], context["company"], context["user"])


def _accounted_setup(context: dict) -> dict:
    setup = _setup(context)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.document_sequences(company_id,fiscal_period_id,sequence_code,"
            "prefix,next_value,padding_length) VALUES (%s,%s,'SALES_JOURNAL','SJ-',1,6)",
            [context["company"], context["period"]],
        )
    return setup


def _report(context: dict, setup: dict, limit: int = 200):
    return setup["client"].get(f"{_root(context)}/inventory/cost-reconciliation", {"limit": limit})


def _history(context: dict) -> dict:
    result = {}
    with connection.cursor() as cursor:
        for table in (
            "stock_movements",
            "inventory_cost_policies",
            "inventory_cost_checkpoints",
            "inventory_cost_checkpoint_movements",
            "inventory_cost_layers",
            "inventory_cost_allocations",
            "inventory_cost_basis_snapshots",
            "inventory_return_cost_basis",
            "journal_entries",
            "journal_lines",
        ):
            cursor.execute(
                f"SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text),'[]'::jsonb) "
                f"FROM erp.{table} x WHERE company_id=%s",
                [context["company"]],
            )
            result[table] = cursor.fetchone()[0]
    return result


def _drift(context: dict) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.inventory_positions SET on_hand_quantity=7,value_company=700 "
            "WHERE company_id=%s",
            [context["company"]],
        )


def _rebuild(context: dict, setup: dict, key: str = "verified-rebuild"):
    return setup["client"].post(
        f"{_root(context)}/inventory/rebuild", {}, format="json", HTTP_IDEMPOTENCY_KEY=key
    )


def test_lazy_origins_reconcile_without_writing_history(accounting_context: dict) -> None:
    setup = _accounted_setup(accounting_context)
    receipt = _document(accounting_context, setup, "receipt", quantity="2")
    assert (
        _post(
            accounting_context,
            setup,
            receipt,
            offset_account_id=str(setup["posting"]["purchase"]),
        ).status_code
        == 200
    )
    before = _history(accounting_context)
    report = _report(accounting_context, setup)
    assert report.status_code == 200, report.json()
    result = report.json()
    assert result["matches"] is True
    assert result["retained_layer_count"] == 0
    assert result["pending_origin_count"] == 2
    assert D(result["scopes"][0]["layer_quantity"]) == 4
    assert D(result["scopes"][0]["layer_value_company"]) == 300
    assert _report(accounting_context, setup).json() == result
    assert _history(accounting_context) == before
    assert _valuation(accounting_context, setup).json()["matches"] is True


def test_inv009_mixed_cost_full_drain_gl_rebuild_and_replay(accounting_context: dict) -> None:
    setup = _accounted_setup(accounting_context)  # Reviewed opening: 2 units / 100.
    receipt = _document(accounting_context, setup, "receipt", quantity="2")
    assert (
        _post(
            accounting_context,
            setup,
            receipt,
            offset_account_id=str(setup["posting"]["purchase"]),
        ).status_code
        == 200
    )  # New actual receipt: 2 units / 200.
    for quantity in ("2", "2"):
        issue = _document(accounting_context, setup, "adjustment_out", quantity=quantity)
        url = f"{_root(accounting_context)}/inventory/documents/{issue['id']}/post"
        payload = setup["payload"] | {
            "offset_account_id": str(setup["posting"]["purchase"]),
            "approve_loss": True,
        }
        key = str(uuid.uuid4())
        posted = _command(setup["client"], url, payload, 1, key)
        assert posted.status_code == 200, posted.json()
        assert _command(setup["client"], url, payload, 1, key).json() == posted.json()
        allocations = posted.json()["cost_allocations"]
        assert len(allocations) == 2
        assert sum(D(a["quantity"]) for a in allocations) == 2
        assert sum(D(a["value_company"]) for a in allocations) == 150
        assert _report(accounting_context, setup).json()["matches"] is True
        assert _valuation(accounting_context, setup).json()["matches"] is True
    before = _history(accounting_context)
    _drift(accounting_context)
    report = _report(accounting_context, setup).json()
    assert report["cost_history_matches"] is True
    assert report["projection_matches"] is False and report["matches"] is False
    assert report["projection_difference_count"] == 1
    rebuilt = _rebuild(accounting_context, setup)
    assert rebuilt.status_code == 200, rebuilt.json()
    assert rebuilt.json()["cost_history_verified"] is True
    assert rebuilt.json()["projection_verified"] is True
    assert _rebuild(accounting_context, setup).json() == rebuilt.json()
    after = _report(accounting_context, setup).json()
    assert after["matches"] is True
    assert D(after["scopes"][0]["layer_quantity"]) == 0
    assert D(after["scopes"][0]["layer_value_company"]) == 0
    assert _history(accounting_context) == before
    assert _valuation(accounting_context, setup).json()["matches"] is True


def test_supplier_return_rebuild_preserves_immutable_cost_and_gl(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    posted = _command(setup["client"], setup["url"], setup["payload"], 1)
    assert posted.status_code == 200, posted.json()
    before = _history(accounting_context)
    _drift(accounting_context)
    assert _report(accounting_context, setup).json()["projection_matches"] is False
    rebuilt = _rebuild(accounting_context, setup)
    assert rebuilt.status_code == 200, rebuilt.json()
    assert _report(accounting_context, setup).json()["matches"] is True
    assert _valuation(accounting_context, setup).json()["matches"] is True
    assert _history(accounting_context) == before


def test_rebuild_restores_active_reservations_and_costing_consumes_own_reservation(
    accounting_context: dict,
) -> None:
    setup = _accounted_setup(accounting_context)
    issue = _document(accounting_context, setup, "adjustment_out", quantity="1")
    reserved = setup["client"].post(
        f"{_root(accounting_context)}/inventory/reservations",
        {
            "line_id": issue["lines"][0]["id"],
            "document_revision": 1,
            "quantity": "1",
            "reservation_key": "cost-rebuild-reservation",
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="cost-rebuild-reservation",
    )
    assert reserved.status_code == 201, reserved.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE erp.inventory_positions SET reserved_quantity=0 WHERE company_id=%s",
            [accounting_context["company"]],
        )
    report = _report(accounting_context, setup).json()
    assert report["projection_matches"] is False
    assert D(report["projection_differences"][0]["ledger_reserved_quantity"]) == 1
    assert _rebuild(accounting_context, setup).status_code == 200
    assert _report(accounting_context, setup).json()["matches"] is True
    posted = _post(
        accounting_context,
        setup,
        issue,
        offset_account_id=str(setup["posting"]["purchase"]),
        approve_loss=True,
    )
    assert posted.status_code == 200, posted.json()
    assert _report(accounting_context, setup).json()["matches"] is True
    assert _valuation(accounting_context, setup).json()["matches"] is True


def test_empty_checkpoint_and_missing_projection_rebuild_are_safe(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    other = setup["client"].post(
        f"{_root(accounting_context)}/inventory/warehouses",
        {"code": "EMPTY", "name": "Empty"},
        format="json",
    )
    assert other.status_code == 201, other.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO erp.inventory_positions(company_id,warehouse_id,item_id) "
            "VALUES (%s,%s,%s)",
            [accounting_context["company"], other.json()["id"], setup["ids"]["item"]],
        )
    _adopt(accounting_context, setup | {"warehouse": uuid.UUID(other.json()["id"])})
    before = _history(accounting_context)
    limited = _report(accounting_context, setup, limit=1).json()
    assert limited["matches"] is True
    assert limited["scope_count"] == limited["adopted_scope_count"] == 2
    assert limited["truncated"] is True and len(limited["scopes"]) == 1
    assert limited["diagnostic_count"] == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM erp.inventory_positions WHERE company_id=%s",
            [accounting_context["company"]],
        )
    missing = _report(accounting_context, setup).json()
    assert missing["projection_difference_count"] == 2 and missing["cost_history_matches"] is True
    rebuilt = _rebuild(accounting_context, setup)
    assert rebuilt.status_code == 200, rebuilt.json()
    assert _report(accounting_context, setup).json()["matches"] is True
    assert _history(accounting_context) == before


def test_missing_immutable_history_is_diagnosed_not_reconstructed(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    assert _command(setup["client"], setup["url"], setup["payload"], 1).status_code == 200
    before = _history(accounting_context)
    # Simulate storage/restore loss only in the test database and rollback everything.
    # No production history trigger is disabled and no damaged fixture is committed.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("TRUNCATE erp.inventory_cost_allocations")
        report = _report(accounting_context, setup).json()
        assert report["matches"] is False and report["cost_history_matches"] is False
        assert report["projection_matches"] is True
        codes = {row["code"] for row in report["diagnostics"]}
        assert {"ISSUE_COSTING_DIFFERENCE", "LAYER_LEDGER_DIFFERENCE"} <= codes
        cursor.execute("SELECT count(*) FROM erp.inventory_cost_allocations")
        assert cursor.fetchone()[0] == 0  # Report never invents replacement allocations.
        transaction.set_rollback(True)
    assert _history(accounting_context) == before
    assert _report(accounting_context, setup).json()["matches"] is True


def test_linked_historical_return_cost_survives_reconciliation_and_rebuild(
    accounting_context: dict,
) -> None:
    setup, returned, warehouse = _returned(accounting_context)
    posted = _dispose(accounting_context, setup, returned, warehouse)
    assert posted.status_code == 200, posted.json()
    report = _report(accounting_context, setup).json()
    assert report["cost_history_matches"] is True
    assert report["projection_matches"] is True
    assert report["adoption_complete"] is False  # Original legacy MAIN is not adopted.
    assert report["matches"] is False
    before = _history(accounting_context)
    _drift(accounting_context)
    rebuilt = _rebuild(accounting_context, setup)
    assert rebuilt.status_code == 200, rebuilt.json()
    after = _report(accounting_context, setup).json()
    assert after["cost_history_matches"] is True and after["projection_matches"] is True
    assert _history(accounting_context) == before
    # Cost integrity does not claim that legacy unlinked opening stock has GL provenance.
    assert _valuation(accounting_context, setup).json()["matches"] is False


def test_unadopted_legacy_is_explicit_and_no_old_allocations_are_fabricated(
    accounting_context: dict,
) -> None:
    setup = _stock_setup(accounting_context)
    report = _report(accounting_context, setup).json()
    assert report["cost_history_matches"] is True and report["projection_matches"] is True
    assert report["unadopted_scope_count"] == 1 and report["matches"] is False
    _adopt(accounting_context, setup)
    adopted = _report(accounting_context, setup).json()
    assert adopted["matches"] is True  # Current checkpoint, not historic issue coverage.
    assert adopted["retained_layer_count"] == 0 and adopted["pending_origin_count"] == 1
    assert _rebuild(accounting_context, setup).status_code == 200
    assert _history(accounting_context)["inventory_cost_allocations"] in ("[]", [])
    assert _valuation(accounting_context, setup).json()["costing_coverage_complete"] is False


@pytest.mark.parametrize("failure", ["history", "projection", "raised"])
def test_rebuild_validation_failure_rolls_back_projection_receipt_and_events(
    accounting_context: dict, failure: str
) -> None:
    setup = _setup(accounting_context)
    _drift(accounting_context)
    before = _history(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        audits = cursor.fetchone()[0]
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        events = cursor.fetchone()[0]
    options = (
        {"side_effect": DatabaseError("injected verification failure")}
        if failure == "raised"
        else {
            "return_value": {
                "cost_history_matches": failure != "history",
                "projection_matches": failure != "projection",
            }
        }
    )
    with patch("apps.inventory.stock_commands.cost_reconciliation", **options):
        response = _rebuild(accounting_context, setup)
    assert response.status_code == 409, response.json()
    if failure != "raised":
        assert response.json()["error"]["code"] == "INVENTORY_REBUILD_INTEGRITY_FAILED"
    assert _history(accounting_context) == before
    assert _report(accounting_context, setup).json()["projection_matches"] is False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.company_audit_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == audits
        cursor.execute(
            "SELECT count(*) FROM erp.outbox_events WHERE company_id=%s",
            [accounting_context["company"]],
        )
        assert cursor.fetchone()[0] == events
    # Same key succeeds after rollback; a failed result was not retained as replay.
    assert _rebuild(accounting_context, setup).status_code == 200


def test_cost_reconciliation_bounds_company_and_module_gates(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    for limit in (0, 201):
        assert _report(accounting_context, setup, limit=limit).status_code == 400
    hidden = setup["client"].get(
        f"/api/v1/tenants/{accounting_context['tenant']}/companies/{uuid.uuid4()}/inventory/cost-reconciliation"
    )
    assert hidden.status_code == 404
    _change_module(setup["client"], accounting_context, "inventory", "read_only")
    assert _report(accounting_context, setup).status_code == 200
    assert _rebuild(accounting_context, setup).status_code == 403
    _change_module(setup["client"], accounting_context, "inventory", "disabled")
    assert _report(accounting_context, setup).status_code == 403


@pytest.mark.security
def test_cost_reconciliation_and_rebuild_require_reconcile_permission(
    accounting_context: dict,
) -> None:
    setup = _setup(accounting_context)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE identity.tenant_memberships SET tenant_role='member' "
            "WHERE tenant_id=%s AND user_id=%s",
            [accounting_context["tenant"], accounting_context["user"]],
        )
    response = _report(accounting_context, setup)
    assert response.status_code == 403, response.json()
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"
    assert _rebuild(accounting_context, setup).status_code == 403


@pytest.mark.security
def test_cost_reconciliation_runtime_rls_hides_other_tenant(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp TO quickaccounts_runtime")
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        report = cost_reconciliation(accounting_context["company"])
        assert report["scope_count"] == report["adopted_scope_count"] == 0
        transaction.set_rollback(True)
    assert _report(accounting_context, setup).json()["scope_count"] == 1


@pytest.mark.concurrency
def test_reconciliation_and_rebuild_during_supplier_post_are_safe(accounting_context: dict) -> None:
    setup = _setup(accounting_context)
    started, release = threading.Event(), threading.Event()

    def pause(*args, **kwargs):
        started.set()
        assert release.wait(timeout=15)
        return record_cost_basis(*args, **kwargs)

    def post():
        close_old_connections()
        try:
            return post_purchase_bill(
                _scope(accounting_context),
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
                before = _report(accounting_context, setup).json()
                assert before["matches"] is True
                assert D(before["scopes"][0]["ledger_quantity"]) == 2
                response = _rebuild(accounting_context, setup)
                assert response.status_code == 409, response.json()
            finally:
                release.set()
            assert future.result(timeout=10)[1] == 200
    assert _report(accounting_context, setup).json()["matches"] is True
    assert _rebuild(accounting_context, setup).status_code == 200


@pytest.mark.concurrency
def test_post_waits_for_rebuild_exclusive_lock_then_costs_restored_projection(
    accounting_context: dict,
) -> None:
    setup = _accounted_setup(accounting_context)
    issue = _document(accounting_context, setup, "adjustment_out", quantity="1")
    _drift(accounting_context)
    started, release, post_started = threading.Event(), threading.Event(), threading.Event()
    writer_pid = []

    def pause(company, **kwargs):
        started.set()
        assert release.wait(timeout=15)
        return cost_reconciliation(company, **kwargs)

    def rebuild():
        close_old_connections()
        try:
            return rebuild_positions(_scope(accounting_context), key="held-rebuild")
        finally:
            close_old_connections()

    def post():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer_pid.append(cursor.fetchone()[0])
            post_started.set()
            return post_document(
                _scope(accounting_context),
                uuid.UUID(issue["id"]),
                {
                    "journal_id": accounting_context["journal"],
                    "fiscal_period_id": accounting_context["period"],
                    "offset_account_id": setup["posting"]["purchase"],
                    "approve_loss": True,
                },
                revision=1,
                key="post-after-rebuild",
            )
        finally:
            close_old_connections()

    with patch("apps.inventory.stock_commands.cost_reconciliation", side_effect=pause):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            rebuilding = executor.submit(rebuild)
            try:
                assert started.wait(timeout=10)
                posting = executor.submit(post)
                assert post_started.wait(timeout=10)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s "
                            "AND locktype='advisory' AND NOT granted)",
                            [writer_pid[0]],
                        )
                        if cursor.fetchone()[0]:
                            break
                    time.sleep(0.01)
                else:
                    pytest.fail("Posting did not wait on the rebuild maintenance lock.")
                assert not posting.done()
            finally:
                release.set()
            assert rebuilding.result(timeout=10)["projection_verified"] is True
            posted = posting.result(timeout=10)
    assert D(posted["cost_basis"][0]["basis_quantity"]) == 2
    assert D(posted["cost_basis"][0]["issue_value_company"]) == 50
    assert _report(accounting_context, setup).json()["matches"] is True
    assert _valuation(accounting_context, setup).json()["matches"] is True


def test_cost_reconciliation_large_input_fails_without_partial_success(
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
            "SELECT %s,'cost-report-bound-'||n,clock_timestamp(),%s,%s,'receipt',1,0,0 "
            "FROM generate_series(1,10000) n",
            [accounting_context["company"], setup["warehouse"], setup["ids"]["item"]],
        )
    response = _report(accounting_context, setup)
    assert response.status_code == 409, response.json()
    assert response.json()["error"]["code"] == "INVENTORY_REPORT_JOB_REQUIRED"
    assert "matches" not in response.json()
    with pytest.raises(Conflict) as failure:
        rebuild_positions(_scope(accounting_context), key="too-large")
    assert failure.value.default_code == "INVENTORY_REPORT_JOB_REQUIRED"
