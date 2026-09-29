import concurrent.futures
import datetime as dt
import threading
import uuid
from decimal import Decimal as D
from unittest.mock import patch

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.inventory.cost_checkpoints import create_cost_checkpoint
from apps.inventory.cost_reconciliation import cost_reconciliation
from apps.inventory.valuation import valuation_reconciliation
from apps.manufacturing.execution import (
    cancel_execution,
    complete_order,
    issue_materials,
    receive_output,
    release_order,
)
from common.api.errors import APIError
from tests.api.test_manufacturing_drafts_api import _active, _counts, _order, _scope, _setup
from tests.api.test_purchase_bill_drafts_api import _bind_license, _change_module
from tests.api.test_sales_returns_api import _command

pytestmark = [pytest.mark.api, pytest.mark.p0, pytest.mark.django_db(transaction=True)]


def _prepare(
    context, *, typed_stock=False, material_lots=False, output_serial=False, zero_cost=False
):
    setup = _setup(context, seed_stock=not typed_stock and not material_lots)
    accounts = {name: uuid.uuid4() for name in ("raw", "finished", "wip")}
    with connection.cursor() as cursor:
        for name, account in accounts.items():
            cursor.execute(
                "INSERT INTO erp.accounts(id,company_id,code,name,account_type,normal_balance) "
                "VALUES (%s,%s,%s,%s,'asset','debit')",
                [account, context["company"], name, name],
            )
        for item, account in [("material", "raw"), ("output", "finished")]:
            cursor.execute(
                "INSERT INTO erp.item_accounting_profiles(company_id,item_id,inventory_account_id) "
                "VALUES (%s,%s,%s)",
                [context["company"], setup["ids"][item], accounts[account]],
            )
        cursor.execute(
            "INSERT INTO erp.document_sequences(company_id,fiscal_period_id,sequence_code,prefix) "
            "VALUES (%s,%s,'SALES_JOURNAL','MFG-')",
            [context["company"], context["period"]],
        )
    setup["accounts"] = accounts
    if output_serial:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.items SET track_serials=true WHERE id=%s", [setup["ids"]["output"]]
            )
    if material_lots:
        setup["lots"] = [uuid.uuid4(), uuid.uuid4()]
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(context["tenant"])])
            cursor.execute(
                "UPDATE erp.items SET track_lots=true WHERE id=%s", [setup["ids"]["material"]]
            )
            for lot, quantity in zip(setup["lots"], (60, 40), strict=True):
                cursor.execute(
                    "INSERT INTO erp.inventory_lots(id,company_id,item_id,lot_code) VALUES "
                    "(%s,%s,%s,%s)",
                    [lot, context["company"], setup["ids"]["material"], str(lot)],
                )
                cursor.execute(
                    "INSERT INTO "
                    "erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,lot_id,"
                    "movement_kind,quantity_delta,unit_cost_company,value_delta_company) "
                    "VALUES (%s,%s,clock_timestamp(),%s,%s,%s,'receipt',%s,1,%s)",
                    [
                        context["company"],
                        str(lot),
                        setup["ids"]["warehouse"],
                        setup["ids"]["material"],
                        lot,
                        quantity,
                        quantity,
                    ],
                )
    setup["posting"] = {
        "posting_date": "2026-09-28",
        "fiscal_period_id": str(context["period"]),
        "journal_id": str(context["journal"]),
    }
    if typed_stock:
        root = setup["root"].removesuffix("/manufacturing")
        response = setup["client"].post(
            f"{root}/inventory/documents",
            {
                "document_no": str(uuid.uuid4()),
                "document_kind": "receipt",
                "document_date": "2026-09-27",
                "lines": [
                    {
                        "item_id": str(setup["ids"]["material"]),
                        "quantity": "100",
                        "unit_cost_company": "0" if zero_cost else "1",
                        "to_warehouse_id": str(setup["ids"]["warehouse"]),
                    }
                ],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        assert response.status_code == 201, response.json()
        document = response.json()
        response = _command(
            setup["client"],
            f"{root}/inventory/documents/{document['id']}/post",
            {
                "fiscal_period_id": str(context["period"]),
                "journal_id": str(context["journal"]),
                "offset_account_id": str(context["equity"]),
            },
            document["row_version"],
        )
        assert response.status_code == 200, response.json()
    order = _order(setup, _active(setup))
    return setup, order


def _call(setup, order, action, data=None, key=None):
    return _command(
        setup["client"],
        f"{setup['root']}/orders/{order['id']}/{action}",
        data or {},
        order["row_version"],
        key,
    )


def _release(setup, order):
    response = _call(setup, order, "release", {"wip_account_id": str(setup["accounts"]["wip"])})
    assert response.status_code == 200, response.json()
    return response.json()


def _issue(setup, order):
    response = _call(
        setup,
        order,
        "material-issues",
        setup["posting"]
        | {"reservation_ids": [r["id"] for r in order["reservations"] if r["status"] == "active"]},
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _output(setup, order, quantity="10", key=None, lot=None):
    return _call(
        setup,
        order,
        "outputs",
        setup["posting"] | {"quantity": quantity, "lot_id": str(lot) if lot else None},
        key,
    )


def _balance(context, account):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(sum(l.debit_amount-l.credit_amount),0) FROM erp.journal_lines l "
            "JOIN erp.journal_entries j ON j.company_id=l.company_id AND j.id=l.journal_entry_id "
            "WHERE l.company_id=%s AND l.account_id=%s AND j.status='posted'",
            [context["company"], account],
        )
        return cursor.fetchone()[0]


def test_mfg003_mfg004_mfg008_issue_partial_output_completion_and_replay(accounting_context):
    setup, order = _prepare(accounting_context)
    released = _release(setup, order)
    assert D(released["reservations"][0]["quantity"]) == D("16.5")
    issued = _issue(setup, released)
    assert _balance(accounting_context, setup["accounts"]["wip"]) == D("16.5")
    before = _counts(accounting_context)
    response = _output(setup, issued, "3", "partial-output")
    assert response.status_code == 200, response.json()
    partial = response.json()
    assert D(partial["outputs"][0]["cost_company"]) == D("4.95")
    assert _balance(accounting_context, setup["accounts"]["wip"]) == D("11.55")
    after = _counts(accounting_context)
    replay = _output(setup, issued, "3", "partial-output")
    assert replay.status_code == 200 and replay.json() == partial
    assert _counts(accounting_context) == after
    assert after["journal_entries"] == before["journal_entries"] + 1
    response = _output(setup, partial, "7")
    assert response.status_code == 200, response.json()
    full = response.json()
    completed = _call(setup, full, "complete", key="complete")
    assert completed.status_code == 200, completed.json()
    assert completed.json()["status"] == "completed"
    assert _balance(accounting_context, setup["accounts"]["wip"]) == 0
    assert _balance(accounting_context, setup["accounts"]["finished"]) == D("16.5")
    assert _balance(accounting_context, setup["accounts"]["raw"]) == -D("16.5")
    assert _call(setup, full, "complete", key="complete").json() == completed.json()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT on_hand_quantity,value_company FROM erp.inventory_positions WHERE "
            "company_id=%s AND item_id=%s",
            [accounting_context["company"], setup["ids"]["output"]],
        )
        assert cursor.fetchone() == (D(10), D("16.5"))


def test_mfg006_cancellation_restores_original_material_cost(accounting_context):
    setup, order = _prepare(accounting_context)
    issued = _issue(setup, _release(setup, order))
    response = _call(
        setup, issued, "cancel", setup["posting"] | {"reason": "Abandon before output"}, "cancel"
    )
    assert response.status_code == 200, response.json()
    cancelled = response.json()
    assert len(cancelled["material_returns"]) == len(cancelled["material_issues"]) == 1
    assert cancelled["status"] == "cancelled"
    assert _balance(accounting_context, setup["accounts"]["wip"]) == 0
    assert _balance(accounting_context, setup["accounts"]["raw"]) == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT on_hand_quantity,value_company,reserved_quantity FROM "
            "erp.inventory_positions WHERE company_id=%s AND item_id=%s",
            [accounting_context["company"], setup["ids"]["material"]],
        )
        assert cursor.fetchone() == (D(100), D(100), D(0))
    assert (
        _call(
            setup,
            issued,
            "cancel",
            setup["posting"] | {"reason": "Abandon before output"},
            "cancel",
        ).json()
        == cancelled
    )


@pytest.mark.parametrize(
    "failure",
    [
        "issue_journal",
        "issue_movement",
        "issue_outbox",
        "output_journal",
        "output_movement",
        "output_outbox",
        "cancel_movement",
    ],
)
def test_execution_failures_rollback_every_effect(accounting_context, failure):
    setup, order = _prepare(accounting_context)
    order = _release(setup, order)
    if not failure.startswith("issue"):
        order = _issue(setup, order)
    before = _counts(accounting_context)
    target = {"journal": "_journal", "movement": "_movement", "outbox": "_event"}[
        failure.split("_")[1]
    ]
    with patch(
        f"apps.manufacturing.execution.{target}", side_effect=DatabaseError("Injected failure")
    ):
        if failure.startswith("issue"):
            response = _call(
                setup,
                order,
                "material-issues",
                setup["posting"] | {"reservation_ids": [r["id"] for r in order["reservations"]]},
            )
        elif failure.startswith("output"):
            response = _output(setup, order)
        else:
            response = _call(setup, order, "cancel", setup["posting"] | {"reason": "Cancel"})
    assert response.status_code == 409, response.json()
    assert _counts(accounting_context) == before
    current = setup["client"].get(f"{setup['root']}/orders/{order['id']}").json()
    assert current == order


@pytest.mark.parametrize(
    "invalid",
    [
        "before_issue",
        "over_output",
        "early_complete",
        "cancel_after_output",
        "stale",
        "closed_period",
        "foreign_reservation",
        "duplicate_reservation",
        "retired_bom",
        "same_account",
    ],
)
def test_execution_validation_has_no_effects(accounting_context, invalid):
    setup, order = _prepare(accounting_context)
    if invalid == "retired_bom":
        bom = setup["client"].get(f"{setup['root']}/boms/{order['bom_id']}").json()
        response = _command(
            setup["client"],
            f"{setup['root']}/boms/{bom['id']}/retire",
            {"reason": "Retire"},
            bom["row_version"],
        )
        assert response.status_code == 200
    if invalid not in {"retired_bom", "same_account"}:
        order = _release(setup, order)
    if invalid in {"over_output", "cancel_after_output", "stale", "closed_period"}:
        order = _issue(setup, order)
    if invalid == "cancel_after_output":
        order = _output(setup, order, "1").json()
    if invalid == "closed_period":
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE erp.fiscal_periods SET state='closed' WHERE id=%s",
                [accounting_context["period"]],
            )
    before = _counts(accounting_context)
    if invalid in {"retired_bom", "same_account"}:
        response = _call(
            setup,
            order,
            "release",
            {
                "wip_account_id": str(
                    setup["accounts"]["finished"]
                    if invalid == "same_account"
                    else setup["accounts"]["wip"]
                )
            },
        )
    elif invalid in {"before_issue", "over_output", "closed_period"}:
        response = _output(setup, order, "11" if invalid == "over_output" else "1")
    elif invalid == "early_complete":
        response = _call(setup, order, "complete")
    elif invalid == "cancel_after_output":
        response = _call(setup, order, "cancel", setup["posting"] | {"reason": "Cancel"})
    elif invalid == "stale":
        response = _output(setup, order | {"row_version": 1}, "1")
    else:
        ids = (
            [str(uuid.uuid4())]
            if invalid == "foreign_reservation"
            else [order["reservations"][0]["id"]] * 2
        )
        response = _call(
            setup, order, "material-issues", setup["posting"] | {"reservation_ids": ids}
        )
    assert response.status_code in {409, 412}, response.json()
    assert _counts(accounting_context) == before


def test_mfg005_concurrent_completion_and_output_do_not_duplicate(accounting_context):
    setup, order = _prepare(accounting_context)
    order = _issue(setup, _release(setup, order))
    barrier = threading.Barrier(2)
    scope = _scope(accounting_context)
    data = {
        "posting_date": dt.date(2026, 9, 28),
        "fiscal_period_id": accounting_context["period"],
        "journal_id": accounting_context["journal"],
        "quantity": D(10),
    }

    def worker(action, current):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if action == "output":
                return receive_output(
                    scope,
                    uuid.UUID(current["id"]),
                    data,
                    revision=current["row_version"],
                    key=str(uuid.uuid4()),
                )
            return complete_order(
                scope,
                uuid.UUID(current["id"]),
                revision=current["row_version"],
                key=str(uuid.uuid4()),
            )
        except APIError as exc:
            return exc.default_code
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker("output", order), range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1
    full = next(r for r in results if isinstance(r, dict))
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker("complete", full), range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM erp.production_outputs WHERE company_id=%s AND "
            "production_order_id=%s",
            [accounting_context["company"], order["id"]],
        )
        assert cursor.fetchone()[0] == 1


def test_execution_records_are_database_immutable(accounting_context):
    setup, order = _prepare(accounting_context)
    order = _issue(setup, _release(setup, order))
    order = _output(setup, order).json()
    for table in (
        "production_material_issues",
        "production_outputs",
        "production_execution_batches",
    ):
        for command in ("UPDATE", "DELETE"):
            with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id',%s,true)",
                    [str(accounting_context["tenant"])],
                )
                statement = (
                    f"UPDATE erp.{table} SET id=id"
                    if command == "UPDATE"
                    else f"DELETE FROM erp.{table}"
                )
                cursor.execute(
                    statement + " WHERE company_id=%s AND production_order_id=%s",
                    [accounting_context["company"], order["id"]],
                )


def test_production_cost_layers_and_valuation_to_gl_survive_output_and_cancellation(
    accounting_context,
):
    setup, order = _prepare(accounting_context, typed_stock=True)
    scope = _scope(accounting_context)
    for name in ("material",):
        create_cost_checkpoint(
            scope,
            {
                "warehouse_id": setup["ids"]["warehouse"],
                "item_id": setup["ids"][name],
                "reason": "Reviewed production adoption",
            },
            key=str(uuid.uuid4()),
        )
    order = _issue(setup, _release(setup, order))
    response = _output(setup, order)
    assert response.status_code == 200, response.json()
    create_cost_checkpoint(
        scope,
        {
            "warehouse_id": setup["ids"]["warehouse"],
            "item_id": setup["ids"]["output"],
            "reason": "Reviewed output adoption",
        },
        key=str(uuid.uuid4()),
    )
    assert cost_reconciliation(accounting_context["company"])["matches"] is True
    report = valuation_reconciliation(
        accounting_context["company"], as_of=dt.date(2026, 9, 28), limit=100
    )
    assert report["matches"] is True, report
    assert D(report["ledger_value_company"]) == D(report["gl_value_company"]) == D(100)


def test_partial_material_issue_cancellation_releases_remaining_reservations(accounting_context):
    setup, order = _prepare(accounting_context, material_lots=True)
    requirement = order["requirements"][0]["id"]
    response = _call(
        setup,
        order,
        "release",
        {
            "wip_account_id": str(setup["accounts"]["wip"]),
            "materials": [
                {"requirement_id": requirement, "lot_id": str(lot), "quantity": quantity}
                for lot, quantity in zip(setup["lots"], ("8", "8.5"), strict=True)
            ],
        },
    )
    assert response.status_code == 200, response.json()
    released = response.json()
    response = _call(
        setup,
        released,
        "material-issues",
        setup["posting"] | {"reservation_ids": [released["reservations"][0]["id"]]},
    )
    assert response.status_code == 200, response.json()
    first = response.json()
    assert _output(setup, first, "1").status_code == 409
    response = _call(
        setup, first, "cancel", setup["posting"] | {"reason": "Cancel only this order"}
    )
    assert response.status_code == 200, response.json()
    current = response.json()
    assert {r["status"] for r in current["reservations"]} == {"released", "consumed"}
    assert len(current["material_issues"]) == len(current["material_returns"]) == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(on_hand_quantity),sum(reserved_quantity),sum(value_company) FROM "
            "erp.inventory_positions WHERE company_id=%s AND item_id=%s",
            [accounting_context["company"], setup["ids"]["material"]],
        )
        assert cursor.fetchone() == (D(100), D(0), D(100))


@pytest.mark.parametrize("action", ["release", "issue", "output", "complete", "cancel"])
@pytest.mark.parametrize("gate", ["read_only", "license", "permission"])
def test_execution_access_gates_have_no_effects(accounting_context, action, gate):
    setup, order = _prepare(accounting_context)
    if action != "release":
        order = _release(setup, order)
    if action in {"output", "complete"}:
        order = _issue(setup, order)
    if action == "complete":
        order = _output(setup, order).json()
    if gate == "read_only":
        _change_module(setup["client"], accounting_context, "manufacturing", "read_only")
    elif gate == "license":
        _bind_license(accounting_context, ("module.inventory",))
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE identity.tenant_memberships SET tenant_role='member' WHERE "
                "tenant_id=%s AND user_id=%s",
                [accounting_context["tenant"], accounting_context["user"]],
            )
    before = _counts(accounting_context)
    if action == "release":
        response = _call(setup, order, "release", {"wip_account_id": str(setup["accounts"]["wip"])})
    elif action == "issue":
        response = _call(
            setup,
            order,
            "material-issues",
            setup["posting"] | {"reservation_ids": [r["id"] for r in order["reservations"]]},
        )
    elif action == "output":
        response = _output(setup, order)
    elif action == "complete":
        response = _call(setup, order, "complete")
    else:
        response = _call(setup, order, "cancel", {"reason": "Cancel"})
    assert response.status_code == 403, response.json()
    assert _counts(accounting_context) == before


def test_release_and_issue_replay_changed_payload_and_duplicate_issue_are_safe(accounting_context):
    setup, order = _prepare(accounting_context)
    payload = {"wip_account_id": str(setup["accounts"]["wip"])}
    response = _call(setup, order, "release", payload, "release")
    assert response.status_code == 200, response.json()
    released = response.json()
    before = _counts(accounting_context)
    assert _call(setup, order, "release", payload, "release").json() == released
    assert (
        _call(
            setup, order, "release", {"wip_account_id": str(setup["accounts"]["raw"])}, "release"
        ).status_code
        == 409
    )
    assert _counts(accounting_context) == before
    data = setup["posting"] | {"reservation_ids": [r["id"] for r in released["reservations"]]}
    response = _call(setup, released, "material-issues", data, "issue")
    assert response.status_code == 200, response.json()
    issued = response.json()
    before = _counts(accounting_context)
    assert _call(setup, released, "material-issues", data, "issue").json() == issued
    assert _call(setup, issued, "material-issues", data, "different-key").status_code == 409
    assert _counts(accounting_context) == before


def test_release_revalidates_availability_and_rolls_back_if_other_reservations_exhaust_stock(
    accounting_context,
):
    setup, order = _prepare(accounting_context)
    bom = setup["client"].get(f"{setup['root']}/boms/{order['bom_id']}").json()
    orders = [_order(setup, bom) for _ in range(6)]
    for current in orders[:5]:
        _release(setup, current)
    before = _counts(accounting_context)
    response = _call(setup, orders[5], "release", {"wip_account_id": str(setup["accounts"]["wip"])})
    assert response.status_code == 200, response.json()  # 99 of 100 units reserved.
    before = _counts(accounting_context)
    response = _call(setup, order, "release", {"wip_account_id": str(setup["accounts"]["wip"])})
    assert response.status_code == 409, response.json()
    assert _counts(accounting_context) == before


@pytest.mark.security
def test_execution_rls_and_immutable_planning_and_reservation_database_guards(accounting_context):
    setup, order = _prepare(accounting_context)
    released = _release(setup, order)
    issued = _issue(setup, released)
    response = _call(setup, issued, "cancel", setup["posting"] | {"reason": "Cancel"})
    assert response.status_code == 200, response.json()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("GRANT USAGE ON SCHEMA erp TO quickaccounts_runtime")
        cursor.execute("GRANT SELECT ON ALL TABLES IN SCHEMA erp TO quickaccounts_runtime")
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute("SET LOCAL ROLE quickaccounts_runtime")
        for table in (
            "production_execution_batches",
            "production_material_returns",
            "production_material_issues",
        ):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] > 0
        cursor.execute("SELECT set_config('app.tenant_id',%s,true)", [str(uuid.uuid4())])
        for table in (
            "production_execution_batches",
            "production_material_returns",
            "production_material_issues",
        ):
            cursor.execute(
                f"SELECT count(*) FROM erp.{table} WHERE company_id=%s",
                [accounting_context["company"]],
            )
            assert cursor.fetchone()[0] == 0
        transaction.set_rollback(True)
    order2 = _order(setup, setup["client"].get(f"{setup['root']}/boms/{order['bom_id']}").json())
    released2 = _release(setup, order2)
    for sql, params in [
        ("UPDATE erp.production_orders SET planned_quantity=1 WHERE id=%s", [released2["id"]]),
        (
            "UPDATE erp.inventory_reservations SET status='released' WHERE id=%s",
            [released2["reservations"][0]["id"]],
        ),
    ]:
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
            )
            cursor.execute(sql, params)


def test_serial_outputs_require_single_units_and_cannot_duplicate_serial(accounting_context):
    setup, order = _prepare(accounting_context, output_serial=True)
    lot = uuid.uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO erp.inventory_lots(id,company_id,item_id,lot_code,serial_code) "
            "VALUES (%s,%s,%s,%s,%s)",
            [lot, accounting_context["company"], setup["ids"]["output"], str(lot), "SERIAL-1"],
        )
    order = _issue(setup, _release(setup, order))
    before = _counts(accounting_context)
    assert _output(setup, order, "2", lot=lot).status_code == 409
    assert _counts(accounting_context) == before
    response = _output(setup, order, "1", lot=lot)
    assert response.status_code == 200, response.json()
    order = response.json()
    before = _counts(accounting_context)
    assert _output(setup, order, "1", lot=lot).status_code == 409
    assert _counts(accounting_context) == before


def test_missing_output_fact_rolls_back_posted_journal_and_movement_at_commit(accounting_context):
    from apps.manufacturing import execution

    setup, order = _prepare(accounting_context)
    order = _issue(setup, _release(setup, order))
    before = _counts(accounting_context)
    original = execution._run

    def incomplete(sql, params):
        if sql.startswith("INSERT INTO erp.production_outputs"):
            return None
        return original(sql, params)

    with patch("apps.manufacturing.execution._run", side_effect=incomplete):
        response = _output(setup, order)
    assert response.status_code == 409, response.json()
    assert _counts(accounting_context) == before
    assert setup["client"].get(f"{setup['root']}/orders/{order['id']}").json() == order


def test_historical_cancellation_is_not_revalued_at_new_warehouse_average(accounting_context):
    setup, order = _prepare(accounting_context)
    order = _issue(setup, _release(setup, order))
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id',%s,true)", [str(accounting_context["tenant"])]
        )
        cursor.execute(
            "INSERT INTO "
            "erp.stock_movements(company_id,event_key,occurred_at,warehouse_id,item_id,movement_kind,"
            "quantity_delta,unit_cost_company,value_delta_company) VALUES "
            "(%s,%s,clock_timestamp(),%s,%s,'receipt',10,10,100)",
            [
                accounting_context["company"],
                str(uuid.uuid4()),
                setup["ids"]["warehouse"],
                setup["ids"]["material"],
            ],
        )
    response = _call(setup, order, "cancel", setup["posting"] | {"reason": "Restore original cost"})
    assert response.status_code == 200, response.json()
    assert _balance(accounting_context, setup["accounts"]["raw"]) == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sum(on_hand_quantity),sum(value_company) FROM erp.inventory_positions "
            "WHERE company_id=%s AND item_id=%s",
            [accounting_context["company"], setup["ids"]["material"]],
        )
        assert cursor.fetchone() == (D(110), D(200))


@pytest.mark.parametrize("race", ["release_shortage", "duplicate_issue", "issue_cancel"])
@pytest.mark.concurrency
def test_production_command_races_preserve_stock_and_execution_history(accounting_context, race):
    setup, order = _prepare(accounting_context)
    scope = _scope(accounting_context)
    if race == "release_shortage":
        bom = setup["client"].get(f"{setup['root']}/boms/{order['bom_id']}").json()
        orders = [_order(setup, bom) for _ in range(6)]
        for current in orders[:5]:
            _release(setup, current)
        commands = [("release", order), ("release", orders[5])]
    else:
        order = _release(setup, order)
        commands = [("issue", order), ("cancel" if race == "issue_cancel" else "issue", order)]
    barrier = threading.Barrier(2)

    def worker(command):
        action, current = command
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            kwargs = {"revision": current["row_version"], "key": str(uuid.uuid4())}
            if action == "release":
                return release_order(
                    scope,
                    uuid.UUID(current["id"]),
                    {"wip_account_id": setup["accounts"]["wip"]},
                    **kwargs,
                )
            posting = {
                "posting_date": dt.date(2026, 9, 28),
                "fiscal_period_id": accounting_context["period"],
                "journal_id": accounting_context["journal"],
            }
            if action == "cancel":
                return cancel_execution(
                    scope, uuid.UUID(current["id"]), posting | {"reason": "Cancel race"}, **kwargs
                )
            return issue_materials(
                scope,
                uuid.UUID(current["id"]),
                posting
                | {"reservation_ids": [uuid.UUID(r["id"]) for r in current["reservations"]]},
                **kwargs,
            )
        except (APIError, DatabaseError) as exc:
            return str(exc)
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(worker, commands))
    assert sum(isinstance(result, dict) for result in results) == 1, results
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT on_hand_quantity,reserved_quantity FROM erp.inventory_positions "
            "WHERE company_id=%s AND item_id=%s",
            [accounting_context["company"], setup["ids"]["material"]],
        )
        quantity, reserved = cursor.fetchone()
        assert 0 <= reserved <= quantity
        cursor.execute(
            "SELECT count(*) FROM erp.production_material_issues "
            "WHERE company_id=%s AND production_order_id=%s",
            [accounting_context["company"], order["id"]],
        )
        assert cursor.fetchone()[0] <= 1


def test_retained_execution_refuses_migration_rollback(accounting_context):
    from importlib import import_module

    setup, order = _prepare(accounting_context)
    _release(setup, order)
    with (
        pytest.raises(DatabaseError, match="Retained production execution"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            import_module("apps.database.migrations.0032_manufacturing_execution").REVERSE
        )


def test_zero_cost_production_retains_stock_facts_without_fictitious_journal(accounting_context):
    setup, order = _prepare(accounting_context, typed_stock=True, zero_cost=True)
    order = _issue(setup, _release(setup, order))
    response = _output(setup, order)
    assert response.status_code == 200, response.json()
    order = response.json()
    assert all(batch["journal_entry_id"] is None for batch in order["execution_batches"])
    assert D(order["outputs"][0]["cost_company"]) == 0
    assert _call(setup, order, "complete").status_code == 200
    assert _counts(accounting_context)["journal_entries"] == 0
