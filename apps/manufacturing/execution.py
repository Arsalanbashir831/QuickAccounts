"""Atomic material-only production commands; no external effects inside transactions."""

import datetime as dt
import uuid
from decimal import Decimal as D
from typing import Any

from django.db import transaction

from apps.inventory.cost_basis import locked_cost_policy, record_cost_basis
from apps.inventory.cost_layers import locked_layers, record_layer_uses
from apps.inventory.costing import CostingError, scope_average_issue
from apps.inventory.layer_costing import allocate_average_issue
from apps.inventory.stock_commands import _item, _position, _serial_reservation_event
from apps.manufacturing.execution_costing import output_cost
from apps.manufacturing.planning import PlanningError
from apps.manufacturing.retries import retry_lock_failure
from apps.manufacturing.selectors import order_detail
from apps.manufacturing.services import _event, _graph_lock, _revision
from apps.payments.services import (
    _account,
    _claim,
    _context,
    _line,
    _money,
    _posting_period,
    _rows,
    _run,
)
from apps.sales.return_services import _finish, _journal, _one
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import Conflict


def _begin(
    scope: CompanyScope,
    order_id: uuid.UUID,
    data: dict[str, Any],
    revision: int,
    key: str,
    action: str,
    request_id: str | None,
) -> tuple[uuid.UUID, dict[str, Any] | None, dict[str, Any]]:
    bind_and_verify_company(scope)
    receipt, replay = _claim(
        scope,
        f"manufacturing.order.{action}",
        str(order_id),
        key,
        {"revision": revision, "data": data},
    )
    permission = (
        "manufacturing.order.release"
        if action == "release"
        else ("manufacturing.order.cancel" if action == "cancel" else "manufacturing.order.post")
    )
    assert_company_write(scope.company_id, "manufacturing", permission)
    assert_company_write(scope.company_id, "inventory", "inventory.post")
    if replay is not None:
        return receipt, replay, {}
    _context(request_id)
    _graph_lock(scope.company_id)
    order = _one(
        "SELECT * FROM erp.production_orders WHERE company_id=%s AND id=%s FOR UPDATE",
        [scope.company_id, order_id],
    )
    _revision(order, revision)
    if order["status"] in {"released", "in_progress"}:
        _run(
            "UPDATE erp.production_orders SET execution_edit_xid=pg_current_xact_id() "
            "WHERE company_id=%s AND id=%s",
            [scope.company_id, order_id],
        )
    _run(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
        [f"inventory-rebuild:{scope.company_id}"],
    )
    return receipt, None, order


def _done(
    scope: CompanyScope, order: dict[str, Any], receipt: uuid.UUID, action: str
) -> dict[str, Any]:
    result = order_detail(scope.company_id, order["id"])
    _event(scope, result, f"manufacturing.order.{action}", "production_order")
    _finish(receipt, result, result_type="production_order")
    return result


def _warehouse(scope: CompanyScope, order: dict[str, Any]) -> None:
    warehouse = _one(
        "SELECT * FROM erp.warehouses WHERE company_id=%s AND id=%s FOR SHARE",
        [scope.company_id, order["warehouse_id"]],
    )
    if not warehouse["is_active"] or warehouse["stock_category"] != "sellable":
        raise Conflict("MFG_WAREHOUSE_INVALID", "Production requires an active sellable warehouse.")


def _lock_items(scope: CompanyScope, ids: set[uuid.UUID]) -> None:
    for item in sorted(ids, key=str):
        _one(
            "SELECT id FROM erp.items WHERE company_id=%s AND id=%s AND is_active FOR UPDATE",
            [scope.company_id, item],
        )


def _lock_scopes(
    scope: CompanyScope, scopes: set[tuple[Any, ...]], *, inbound: bool = False
) -> None:
    for warehouse, item, lot in sorted(scopes, key=lambda s: tuple(str(v or "") for v in s)):
        exists = _rows(
            "SELECT id FROM erp.inventory_positions WHERE company_id=%s AND warehouse_id=%s "
            "AND item_id=%s AND lot_id IS NOT DISTINCT FROM %s::uuid",
            [scope.company_id, warehouse, item, lot],
        )
        if exists:
            _position(scope.company_id, warehouse, item, lot)
        elif not inbound:
            raise Conflict("MFG_MATERIAL_UNAVAILABLE", "Selected material scope has no stock.")


@retry_lock_failure
def release_order(
    scope: CompanyScope,
    order_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        receipt, replay, order = _begin(scope, order_id, data, revision, key, "release", request_id)
        if replay is not None:
            return replay
        if order["status"] != "planned":
            raise Conflict("MFG_RELEASE_INVALID", "Only a planned order can be released.")
        _warehouse(scope, order)
        requirements = _rows(
            "SELECT * FROM erp.production_order_requirements WHERE company_id=%s "
            "AND production_order_id=%s ORDER BY id FOR UPDATE",
            [scope.company_id, order_id],
        )
        _lock_items(
            scope, {order["output_item_id"], *(q["component_item_id"] for q in requirements)}
        )
        output_profile = _one(
            "SELECT * FROM erp.item_accounting_profiles WHERE company_id=%s "
            "AND item_id=%s FOR SHARE",
            [scope.company_id, order["output_item_id"]],
        )
        wip = data["wip_account_id"]
        output_account = output_profile["inventory_account_id"]
        _account(scope.company_id, wip, {"asset"})
        _account(scope.company_id, output_account, {"asset"})
        if wip == output_account:
            raise Conflict("MFG_ACCOUNT_INVALID", "WIP and inventory accounts must differ.")
        selected = data.get("materials")
        if selected is None:
            selected = [
                {"requirement_id": q["id"], "lot_id": None, "quantity": q["required_quantity"]}
                for q in requirements
            ]
        by_id = {q["id"]: q for q in requirements}
        totals: dict[uuid.UUID, D] = {}
        if not 1 <= len(selected) <= 100 or len(
            {(s["requirement_id"], s.get("lot_id")) for s in selected}
        ) != len(selected):
            raise Conflict("MFG_RESERVATION_INVALID", "Select one to 100 distinct material scopes.")
        for row in selected:
            if row["requirement_id"] not in by_id:
                raise Conflict(
                    "MFG_RESERVATION_INVALID", "Material requirement does not belong to this order."
                )
            q = by_id[row["requirement_id"]]
            _item(scope.company_id, q["component_item_id"], row.get("lot_id"), row["quantity"])
            profile = _one(
                "SELECT inventory_account_id FROM erp.item_accounting_profiles "
                "WHERE company_id=%s AND item_id=%s FOR SHARE",
                [scope.company_id, q["component_item_id"]],
            )
            _account(scope.company_id, profile["inventory_account_id"], {"asset"})
            if profile["inventory_account_id"] == wip:
                raise Conflict("MFG_ACCOUNT_INVALID", "Material inventory must differ from WIP.")
            totals[q["id"]] = totals.get(q["id"], D(0)) + row["quantity"]
        if any(totals.get(q["id"]) != q["required_quantity"] for q in requirements):
            raise Conflict("MFG_RESERVATION_INVALID", "Reserve each retained requirement exactly.")
        _lock_scopes(
            scope,
            {
                (
                    order["warehouse_id"],
                    by_id[s["requirement_id"]]["component_item_id"],
                    s.get("lot_id"),
                )
                for s in selected
            },
        )
        _run(
            "UPDATE erp.production_orders SET "
            "status='released',execution_edit_xid=pg_current_xact_id(),"
            "wip_account_id_snapshot=%s,output_inventory_account_id_snapshot=%s WHERE "
            "company_id=%s AND id=%s",
            [wip, output_account, scope.company_id, order_id],
        )
        for row in selected:
            reservation = _one(
                "INSERT INTO erp.inventory_reservations(company_id,warehouse_id,item_id,lot_id,"
                "reservation_key,source_type,source_id,quantity) VALUES "
                "(%s,%s,%s,%s,%s,'production_requirement',%s,%s) RETURNING *",
                [
                    scope.company_id,
                    order["warehouse_id"],
                    by_id[row["requirement_id"]]["component_item_id"],
                    row.get("lot_id"),
                    f"production:{order_id}:{row['requirement_id']}:{row.get('lot_id')}",
                    row["requirement_id"],
                    row["quantity"],
                ],
            )
            _serial_reservation_event(scope.company_id, reservation, "production_reserved")
        return _done(scope, order, receipt, "released")


def _facts(scope: CompanyScope) -> dict[str, Any]:
    return _one(
        "SELECT c.functional_currency currency_code,f.minor_units AS precision FROM "
        "erp.companies c "
        "JOIN erp.currencies f ON f.code=c.functional_currency WHERE c.id=%s",
        [scope.company_id],
    ) | {"exchange_rate": D(1), "partner_id": None}


def _batch(
    scope: CompanyScope,
    order: dict[str, Any],
    action: str,
    data: dict[str, Any],
    key: str,
    facts: dict[str, Any],
    amounts: list[tuple[uuid.UUID, D]],
    *,
    output_basis: tuple[D, D, D] | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    batch = uuid.uuid4()
    entry = None
    if any(amount for _, amount in amounts):
        entry = _journal(scope, batch, "production_execution", data["posting_date"], data, key)
        for index, (account, amount) in enumerate(amounts, 1):
            _account(scope.company_id, account, {"asset"})
            _line(
                scope.company_id,
                entry,
                index,
                account,
                abs(amount),
                amount > 0,
                facts,
                facts["precision"],
            )
        _run(
            "UPDATE erp.journal_lines SET description='Production material accounting' "
            "WHERE company_id=%s AND journal_entry_id=%s",
            [scope.company_id, entry],
        )
        _run("SELECT erp.post_journal_entry(%s,%s)", [scope.company_id, entry])
    _run(
        "INSERT INTO "
        "erp.production_execution_batches(id,company_id,production_order_id,kind,posting_date,"
        "journal_entry_id,currency_precision,basis_completed_quantity,basis_allocated_cost,"
        "basis_material_cost) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            batch,
            scope.company_id,
            order["id"],
            action,
            data["posting_date"],
            entry,
            facts["precision"],
            *(output_basis if output_basis is not None else (None, None, None)),
        ],
    )
    return batch, entry


def _period(scope: CompanyScope, order: dict[str, Any], data: dict[str, Any]) -> None:
    _posting_period(
        scope.company_id, data["fiscal_period_id"], data["journal_id"], data["posting_date"]
    )
    latest = _one(
        "SELECT max(posting_date) date FROM erp.production_execution_batches "
        "WHERE company_id=%s AND production_order_id=%s",
        [scope.company_id, order["id"]],
    )["date"]
    if latest and data["posting_date"] < latest:
        raise Conflict(
            "MFG_POSTING_DATE_INVALID", "Production commands cannot precede prior execution."
        )
    _warehouse(scope, order)


def _movement(
    scope: CompanyScope,
    order: dict[str, Any],
    batch: uuid.UUID,
    entry: uuid.UUID | None,
    line: uuid.UUID,
    item: uuid.UUID,
    lot: uuid.UUID | None,
    quantity: D,
    value: D,
    unit_cost: D,
    date: dt.date,
) -> uuid.UUID:
    movement = uuid.uuid4()
    _run(
        "INSERT INTO "
        "erp.stock_movements(id,company_id,event_key,occurred_at,warehouse_id,item_id,lot_id,"
        "movement_kind,quantity_delta,unit_cost_company,value_delta_company,source_type,source_id,source_line_id,"
        "journal_entry_id) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'production_execution',%s,%s,%s)",
        [
            movement,
            scope.company_id,
            f"production:{batch}:{line}",
            dt.datetime.combine(date, dt.time(), dt.UTC),
            order["warehouse_id"],
            item,
            lot,
            "issue" if quantity < 0 else "receipt",
            quantity,
            unit_cost,
            value,
            batch,
            line,
            entry,
        ],
    )
    return movement


@retry_lock_failure
def issue_materials(
    scope: CompanyScope,
    order_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        receipt, replay, order = _begin(scope, order_id, data, revision, key, "issue", request_id)
        if replay is not None:
            return replay
        if order["status"] not in {"released", "in_progress"} or order["completed_quantity"]:
            raise Conflict(
                "MFG_ORDER_STATE_INVALID", "Issue materials before any output on a released order."
            )
        _period(scope, order, data)
        reservations = _rows(
            "SELECT r.* FROM erp.inventory_reservations r JOIN erp.production_order_requirements q "
            "ON q.company_id=r.company_id AND q.id=r.source_id WHERE r.company_id=%s AND "
            "q.production_order_id=%s "
            "AND r.source_type='production_requirement' AND r.id=ANY(%s::uuid[]) ORDER BY r.id",
            [scope.company_id, order_id, data["reservation_ids"]],
        )
        if (
            len(reservations) != len(set(data["reservation_ids"]))
            or len(reservations) != len(data["reservation_ids"])
            or not reservations
        ):
            raise Conflict(
                "MFG_RESERVATION_INVALID", "Select distinct reservations belonging to this order."
            )
        _lock_items(scope, {r["item_id"] for r in reservations})
        _lock_scopes(scope, {(r["warehouse_id"], r["item_id"], r["lot_id"]) for r in reservations})
        policy = locked_cost_policy(scope)
        facts = _facts(scope)
        costs = []
        amounts = []
        for reservation in reservations:
            r = _one(
                "SELECT * FROM erp.inventory_reservations WHERE company_id=%s AND id=%s FOR UPDATE",
                [scope.company_id, reservation["id"]],
            )
            if r["status"] != "active":
                raise Conflict(
                    "MFG_RESERVATION_INVALID", "Only active reservations can be issued once."
                )
            _item(scope.company_id, r["item_id"], r["lot_id"], r["quantity"])
            profile = _one(
                "SELECT inventory_account_id FROM erp.item_accounting_profiles "
                "WHERE company_id=%s AND item_id=%s FOR SHARE",
                [scope.company_id, r["item_id"]],
            )
            account = profile["inventory_account_id"]
            _account(scope.company_id, account, {"asset"})
            if account == order["wip_account_id_snapshot"]:
                raise Conflict("MFG_ACCOUNT_INVALID", "Material inventory and WIP must differ.")
            _run(
                "UPDATE erp.inventory_reservations SET status='consumed' WHERE "
                "company_id=%s AND id=%s",
                [scope.company_id, r["id"]],
            )
            _serial_reservation_event(scope.company_id, r, "production_reservation_consumed")
            position = _position(scope.company_id, r["warehouse_id"], r["item_id"], r["lot_id"])
            try:
                calculated = scope_average_issue(
                    on_hand=position["on_hand_quantity"],
                    stock_value=position["value_company"],
                    reserved=position["reserved_quantity"],
                    quantity=r["quantity"],
                    currency_precision=facts["precision"],
                )
                layers = locked_layers(
                    scope.company_id, (r["warehouse_id"], r["item_id"], r["lot_id"])
                )
                uses = allocate_average_issue(layers, calculated) if layers is not None else None
            except CostingError as exc:
                raise Conflict(exc.code, str(exc)) from exc
            costs.append((r, calculated, account, uses, position))
            amounts.extend(
                [
                    (order["wip_account_id_snapshot"], calculated.value_company),
                    (account, -calculated.value_company),
                ]
            )
        batch, entry = _batch(scope, order, "issue", data, key, facts, amounts)
        for r, cost, account, uses, position in costs:
            line = uuid.uuid4()
            movement = _movement(
                scope,
                order,
                batch,
                entry,
                line,
                r["item_id"],
                r["lot_id"],
                -r["quantity"],
                -cost.value_company,
                cost.unit_cost_company,
                data["posting_date"],
            )
            record_cost_basis(
                scope,
                movement,
                policy["id"],
                {
                    "basis_quantity": position["on_hand_quantity"],
                    "basis_value_company": position["value_company"],
                    "reserved_quantity": position["reserved_quantity"],
                    "issue_quantity": cost.quantity,
                    "issue_value_company": cost.value_company,
                    "unit_cost_company": cost.unit_cost_company,
                },
                currency_code=facts["currency_code"],
                currency_precision=facts["precision"],
            )
            if uses is not None:
                record_layer_uses(scope.company_id, movement, uses)
            _run(
                "INSERT INTO "
                "erp.production_material_issues(id,company_id,production_order_id,stock_movement_id,"
                "component_item_id,quantity_issued,cost_company,batch_id,reservation_id,inventory_account_id_snapshot)"
                " "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    line,
                    scope.company_id,
                    order_id,
                    movement,
                    r["item_id"],
                    r["quantity"],
                    cost.value_company,
                    batch,
                    r["id"],
                    account,
                ],
            )
        _run(
            "UPDATE erp.production_orders SET status='in_progress' WHERE company_id=%s AND id=%s",
            [scope.company_id, order_id],
        )
        return _done(scope, order, receipt, "materials_issued")


@retry_lock_failure
def receive_output(
    scope: CompanyScope,
    order_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        receipt, replay, order = _begin(scope, order_id, data, revision, key, "output", request_id)
        if replay is not None:
            return replay
        if order["status"] != "in_progress":
            raise Conflict("MFG_ORDER_STATE_INVALID", "Receive output from an in-progress order.")
        _period(scope, order, data)
        if _rows(
            "SELECT q.id FROM erp.production_order_requirements q WHERE q.company_id=%s AND"
            " q.production_order_id=%s "
            "AND q.required_quantity<>coalesce((SELECT sum(i.quantity_issued) FROM "
            "erp.production_material_issues i "
            "JOIN erp.inventory_reservations r ON r.company_id=i.company_id AND "
            "r.id=i.reservation_id "
            "WHERE i.company_id=q.company_id AND r.source_id=q.id),0)",
            [scope.company_id, order_id],
        ):
            raise Conflict(
                "MFG_MATERIAL_INCOMPLETE", "Issue all retained material requirements before output."
            )
        facts = _facts(scope)
        basis = _one(
            "SELECT coalesce(sum(cost_company),0) cost FROM erp.production_material_issues "
            "WHERE company_id=%s AND production_order_id=%s",
            [scope.company_id, order_id],
        )
        prior = _one(
            "SELECT coalesce(sum(cost_company),0) cost,coalesce(sum(quantity_completed),0) "
            "quantity "
            "FROM erp.production_outputs WHERE company_id=%s AND production_order_id=%s",
            [scope.company_id, order_id],
        )
        if prior["quantity"] != order["completed_quantity"]:
            raise Conflict("MFG_WIP_DRIFT", "Output quantity projection differs from history.")
        try:
            value = output_cost(
                order["planned_quantity"],
                prior["quantity"],
                data["quantity"],
                basis["cost"],
                prior["cost"],
                facts["precision"],
            )
        except PlanningError as exc:
            raise Conflict(exc.code, str(exc)) from exc
        _lock_items(scope, {order["output_item_id"]})
        _item(scope.company_id, order["output_item_id"], data.get("lot_id"), data["quantity"])
        _lock_scopes(
            scope,
            {(order["warehouse_id"], order["output_item_id"], data.get("lot_id"))},
            inbound=True,
        )
        batch, entry = _batch(
            scope,
            order,
            "output",
            data,
            key,
            facts,
            [
                (order["output_inventory_account_id_snapshot"], value),
                (order["wip_account_id_snapshot"], -value),
            ],
            output_basis=(prior["quantity"], prior["cost"], basis["cost"]),
        )
        line = uuid.uuid4()
        movement = _movement(
            scope,
            order,
            batch,
            entry,
            line,
            order["output_item_id"],
            data.get("lot_id"),
            data["quantity"],
            value,
            _money(value / data["quantity"], 6),
            data["posting_date"],
        )
        _run(
            "INSERT INTO "
            "erp.production_outputs(id,company_id,production_order_id,stock_movement_id,"
            "output_item_id,quantity_completed,cost_company,batch_id) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                line,
                scope.company_id,
                order_id,
                movement,
                order["output_item_id"],
                data["quantity"],
                value,
                batch,
            ],
        )
        _run(
            "UPDATE erp.production_orders SET completed_quantity=completed_quantity+%s "
            "WHERE company_id=%s AND id=%s",
            [data["quantity"], scope.company_id, order_id],
        )
        return _done(scope, order, receipt, "output_received")


@retry_lock_failure
def complete_order(
    scope: CompanyScope,
    order_id: uuid.UUID,
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        receipt, replay, order = _begin(scope, order_id, {}, revision, key, "complete", request_id)
        if replay is not None:
            return replay
        if (
            order["status"] != "in_progress"
            or order["completed_quantity"] != order["planned_quantity"]
        ):
            raise Conflict(
                "MFG_COMPLETION_INVALID", "Complete only fully produced in-progress orders."
            )
        _run(
            "UPDATE erp.production_orders SET "
            "status='completed',execution_edit_xid=pg_current_xact_id() "
            "WHERE company_id=%s AND id=%s",
            [scope.company_id, order_id],
        )
        return _done(scope, order, receipt, "completed")


@retry_lock_failure
def cancel_execution(
    scope: CompanyScope,
    order_id: uuid.UUID,
    data: dict[str, Any],
    *,
    revision: int,
    key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    with transaction.atomic(durable=True):
        receipt, replay, order = _begin(scope, order_id, data, revision, key, "cancel", request_id)
        if replay is not None:
            return replay
        if order["status"] == "planned":
            _run(
                "UPDATE erp.production_orders SET status='cancelled',cancellation_reason=%s"
                " WHERE company_id=%s AND id=%s",
                [data["reason"], scope.company_id, order_id],
            )
            return _done(scope, order, receipt, "cancelled")
        if order["status"] not in {"released", "in_progress"} or order["completed_quantity"]:
            raise Conflict(
                "MFG_CANCELLATION_INVALID", "Execution can be cancelled only before output."
            )
        issues = _rows(
            "SELECT i.*,m.lot_id,m.unit_cost_company FROM erp.production_material_issues i "
            "JOIN erp.stock_movements m ON m.company_id=i.company_id AND m.id=i.stock_movement_id "
            "WHERE i.company_id=%s AND i.production_order_id=%s ORDER BY i.id",
            [scope.company_id, order_id],
        )
        active = _rows(
            "SELECT r.* FROM erp.inventory_reservations r JOIN erp.production_order_requirements q "
            "ON q.company_id=r.company_id AND q.id=r.source_id WHERE q.company_id=%s AND "
            "q.production_order_id=%s AND r.source_type='production_requirement' "
            "AND r.status='active' ORDER BY r.warehouse_id,r.item_id,r.lot_id,r.id",
            [scope.company_id, order_id],
        )
        if issues:
            if not {"posting_date", "fiscal_period_id", "journal_id"} <= set(data):
                raise Conflict(
                    "MFG_POSTING_REQUIRED",
                    "Issued materials require cancellation posting date, period and journal.",
                )
            _period(scope, order, data)
        _lock_items(
            scope, {i["component_item_id"] for i in issues} | {r["item_id"] for r in active}
        )
        _lock_scopes(
            scope,
            {(order["warehouse_id"], i["component_item_id"], i["lot_id"]) for i in issues}
            | {(r["warehouse_id"], r["item_id"], r["lot_id"]) for r in active},
            inbound=True,
        )
        if issues:
            facts = _facts(scope)
            amounts = [
                (account, value)
                for i in issues
                for account, value in (
                    (i["inventory_account_id_snapshot"], i["cost_company"]),
                    (order["wip_account_id_snapshot"], -i["cost_company"]),
                )
            ]
            batch, entry = _batch(scope, order, "cancel", data, key, facts, amounts)
            for issue in issues:
                line = uuid.uuid4()
                movement = _movement(
                    scope,
                    order,
                    batch,
                    entry,
                    line,
                    issue["component_item_id"],
                    issue["lot_id"],
                    issue["quantity_issued"],
                    issue["cost_company"],
                    issue["unit_cost_company"],
                    data["posting_date"],
                )
                _run(
                    "INSERT INTO "
                    "erp.production_material_returns(id,company_id,production_order_id,batch_id,"
                    "material_issue_id,stock_movement_id) VALUES (%s,%s,%s,%s,%s,%s)",
                    [line, scope.company_id, order_id, batch, issue["id"], movement],
                )
        for r in active:
            _run(
                "UPDATE erp.inventory_reservations SET status='released' WHERE "
                "company_id=%s AND id=%s",
                [scope.company_id, r["id"]],
            )
            _serial_reservation_event(scope.company_id, r, "production_reservation_released")
        _run(
            "UPDATE erp.production_orders SET "
            "status='cancelled',execution_edit_xid=pg_current_xact_id(),cancellation_reason=%s"
            " "
            "WHERE company_id=%s AND id=%s",
            [data["reason"], scope.company_id, order_id],
        )
        return _done(scope, order, receipt, "cancelled")
