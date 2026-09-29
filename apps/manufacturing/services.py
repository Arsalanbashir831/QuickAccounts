"""Revisioned BOM and production-plan commands. Execution is deliberately separate."""

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.manufacturing.planning import MAX_QUANTITY, PlanningError, required_quantity
from apps.manufacturing.selectors import bom_detail, order_detail
from apps.payments.services import _claim, _context, _rows, _run
from apps.sales.return_services import _finish
from common.access.scopes import CompanyScope, assert_company_write, bind_and_verify_company
from common.api.errors import APIError, Conflict, PreconditionFailed

BOM_FIELDS = ("output_item_id", "revision", "output_quantity", "effective_from", "effective_to")
ORDER_FIELDS = (
    "production_no",
    "bom_id",
    "warehouse_id",
    "planned_quantity",
    "planned_start",
    "planned_end",
)


def _graph_lock(company: uuid.UUID) -> None:
    _run(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        [f"manufacturing-planning:{company}"],
    )


def _revision(current: dict[str, Any], revision: int | None) -> None:
    if current["row_version"] != revision:
        raise PreconditionFailed(int(current["row_version"]))


def _input(data: dict[str, Any], allowed: tuple[str, ...]) -> None:
    if not data or set(data) - set(allowed):
        raise APIError(
            code="INVALID_MANUFACTURING_INPUT", message="Unsupported or empty draft fields."
        )


def _dates(data: dict[str, Any], start: str, end: str) -> None:
    for field in (start, end):
        if isinstance(data.get(field), str):
            data[field] = dt.date.fromisoformat(data[field])
    if data.get(start) and data.get(end) and data[end] < data[start]:
        raise APIError(
            code="MFG_DATES_INVALID", message="The end date cannot precede the start date."
        )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise APIError(code="MFG_QUANTITY_INVALID", message="Invalid planning quantity.") from exc


def _event(scope: CompanyScope, result: dict[str, Any], event: str, aggregate: str) -> None:
    _run(
        "INSERT INTO erp.outbox_events(company_id,event_key,aggregate_type,aggregate_id,"
        "event_type,payload) VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
        [
            scope.company_id,
            f"{event}:{result['id']}:{result['row_version']}",
            aggregate,
            result["id"],
            event,
            json.dumps({"id": result["id"], "row_version": result["row_version"]}),
        ],
    )


def save_bom(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    key: str,
    bom_id: uuid.UUID | None = None,
    revision: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    _input(data, (*BOM_FIELDS, "lines"))
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "manufacturing.bom.update" if bom_id else "manufacturing.bom.create",
            str(bom_id) if bom_id else "new",
            key,
            {"revision": revision, "data": data},
        )
        assert_company_write(scope.company_id, "manufacturing", "manufacturing.bom.manage")
        if replay is not None:
            return replay
        _context(request_id)
        _graph_lock(scope.company_id)
        current = bom_detail(scope.company_id, bom_id, for_update=True) if bom_id else {}
        if bom_id:
            _revision(current, revision)
            if current["status"] != "draft":
                raise Conflict("MFG_BOM_FROZEN", "Only draft BOM revisions can be edited.")
        merged = current | data
        _dates(merged, "effective_from", "effective_to")
        lines = merged.get("lines", [])
        if not 1 <= len(lines) <= 100:
            raise APIError(code="MFG_BOM_LINES_INVALID", message="A BOM requires 1 to 100 lines.")
        components = [str(line["component_item_id"]) for line in lines]
        if len(set(components)) != len(components) or str(merged["output_item_id"]) in components:
            raise APIError(
                code="MFG_BOM_ITEMS_INVALID",
                message="Components must be distinct and not the output.",
            )
        quantity = _decimal(merged["output_quantity"])
        # Validate the basis and line inputs without requiring a rounded derived allowance.
        try:
            required_quantity(quantity, quantity, quantity)
            for line in lines:
                component_quantity = _decimal(line["quantity_per_output"])
                required_quantity(component_quantity, component_quantity, component_quantity)
                scrap = _decimal(line.get("scrap_percent", 0))
                if not scrap.is_finite() or not 0 <= scrap <= 100:
                    raise PlanningError("MFG_QUANTITY_INVALID", "Scrap allowance must be 0 to 100.")
        except PlanningError as exc:
            raise Conflict(exc.code, str(exc)) from exc
        if bom_id:
            _run(
                "UPDATE erp.boms SET output_item_id=%s,revision=%s,output_quantity=%s,"
                "effective_from=%s,effective_to=%s WHERE company_id=%s AND id=%s",
                [*(merged.get(field) for field in BOM_FIELDS), scope.company_id, bom_id],
            )
        else:
            bom_id = _rows(
                "INSERT INTO erp.boms(company_id,output_item_id,revision,output_quantity,"
                "effective_from,effective_to) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                [scope.company_id, *(merged.get(field) for field in BOM_FIELDS)],
            )[0]["id"]
        if "lines" in data:
            _run(
                "DELETE FROM erp.bom_lines WHERE company_id=%s AND bom_id=%s",
                [scope.company_id, bom_id],
            )
            for number, line in enumerate(lines, 1):
                _run(
                    "INSERT INTO erp.bom_lines(company_id,bom_id,line_no,component_item_id,"
                    "quantity_per_output,scrap_percent) VALUES (%s,%s,%s,%s,%s,%s)",
                    [
                        scope.company_id,
                        bom_id,
                        number,
                        line["component_item_id"],
                        line["quantity_per_output"],
                        line.get("scrap_percent", 0),
                    ],
                )
        result = bom_detail(scope.company_id, bom_id)
        _event(scope, result, "manufacturing.bom.saved", "bom")
        _finish(receipt, result, 200 if current else 201, result_type="bom")
        return result


def transition_bom(
    scope: CompanyScope,
    bom_id: uuid.UUID,
    action: str,
    *,
    revision: int,
    key: str,
    reason: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    if action not in {"activate", "retire"}:
        raise ValueError("Unsupported BOM transition.")
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            f"manufacturing.bom.{action}",
            str(bom_id),
            key,
            {"revision": revision, "reason": reason},
        )
        permission = (
            "manufacturing.bom.activate" if action == "activate" else "manufacturing.bom.manage"
        )
        assert_company_write(scope.company_id, "manufacturing", permission)
        if replay is not None:
            return replay
        _context(request_id)
        _graph_lock(scope.company_id)
        current = bom_detail(scope.company_id, bom_id, for_update=True)
        _revision(current, revision)
        if current["status"] == "retired" or (
            action == "activate" and current["status"] != "draft"
        ):
            raise Conflict(
                "MFG_BOM_STATE_INVALID", "BOM revision is not eligible for this transition."
            )
        if action == "activate":
            _run(
                "UPDATE erp.boms SET status='active' WHERE company_id=%s AND id=%s",
                [scope.company_id, bom_id],
            )
        else:
            if not reason or not reason.strip() or len(reason.strip()) > 2000:
                raise APIError(code="MFG_REASON_REQUIRED", message="Retirement requires a reason.")
            _run(
                "UPDATE erp.boms SET status='retired',retirement_reason=%s "
                "WHERE company_id=%s AND id=%s",
                [reason.strip(), scope.company_id, bom_id],
            )
        result = bom_detail(scope.company_id, bom_id)
        _event(scope, result, f"manufacturing.bom.{action}d", "bom")
        _finish(receipt, result, result_type="bom")
        return result


def _prepare_requirements(company: uuid.UUID, data: dict[str, Any]) -> list[dict[str, Any]]:
    bom = bom_detail(company, uuid.UUID(str(data["bom_id"])))
    if bom["status"] != "active":
        raise Conflict("MFG_BOM_NOT_SELECTABLE", "Production plans require an active approved BOM.")
    planned_start = data.get("planned_start") or dt.datetime.now(dt.UTC).date()
    for field, is_start in (("effective_from", True), ("effective_to", False)):
        boundary = dt.date.fromisoformat(bom[field]) if bom[field] else None
        if boundary and (
            (is_start and planned_start < boundary) or (not is_start and planned_start > boundary)
        ):
            raise Conflict(
                "MFG_BOM_NOT_SELECTABLE", "BOM is not effective on the planning start date."
            )
    items = {
        str(row["id"]): row
        for row in _rows(
            "SELECT id,track_serials,base_uom_id FROM erp.items WHERE company_id=%s "
            "AND id=ANY(%s::uuid[]) ORDER BY id FOR SHARE",
            [
                company,
                [bom["output_item_id"], *(line["component_item_id"] for line in bom["lines"])],
            ],
        )
    }
    planned = _decimal(data["planned_quantity"])
    if not planned.is_finite() or not 0 < planned <= MAX_QUANTITY:
        raise Conflict("MFG_QUANTITY_INVALID", "Planned quantity is invalid.")
    if items[bom["output_item_id"]]["track_serials"] and planned != planned.to_integral_value():
        raise Conflict("MFG_SERIAL_QUANTITY_INVALID", "Serial output plans require whole units.")
    result = []
    try:
        for line in bom["lines"]:
            item = items[line["component_item_id"]]
            quantity = required_quantity(
                planned,
                _decimal(bom["output_quantity"]),
                _decimal(line["quantity_per_output"]),
                _decimal(line["scrap_percent"]),
                serial_tracked=item["track_serials"],
            )
            result.append(line | {"required_quantity": quantity, "uom_id": item["base_uom_id"]})
    except PlanningError as exc:
        raise Conflict(exc.code, str(exc)) from exc
    return result


def _retain_requirements(
    company: uuid.UUID, order: uuid.UUID, requirements: list[dict[str, Any]]
) -> None:
    for line in requirements:
        _run(
            "INSERT INTO erp.production_order_requirements(company_id,production_order_id,"
            "bom_line_id,line_no,component_item_id,component_uom_id_snapshot,"
            "quantity_per_output_snapshot,scrap_percent_snapshot,required_quantity) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                company,
                order,
                line["id"],
                line["line_no"],
                line["component_item_id"],
                line["uom_id"],
                line["quantity_per_output"],
                line["scrap_percent"],
                line["required_quantity"],
            ],
        )


def save_order(
    scope: CompanyScope,
    data: dict[str, Any],
    *,
    key: str,
    order_id: uuid.UUID | None = None,
    revision: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    _input(data, ORDER_FIELDS)
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "manufacturing.order.update" if order_id else "manufacturing.order.create",
            str(order_id) if order_id else "new",
            key,
            {"revision": revision, "data": data},
        )
        assert_company_write(scope.company_id, "manufacturing", "manufacturing.order.manage")
        if replay is not None:
            return replay
        _context(request_id)
        _graph_lock(scope.company_id)
        current = order_detail(scope.company_id, order_id, for_update=True) if order_id else {}
        if order_id:
            _revision(current, revision)
            if current["status"] != "planned":
                raise Conflict("MFG_ORDER_FROZEN", "Only planned production orders can be edited.")
        merged = current | data
        _dates(merged, "planned_start", "planned_end")
        requirements = _prepare_requirements(scope.company_id, merged)
        if order_id:
            _run(
                "UPDATE erp.production_orders SET production_no=%s,bom_id=%s,warehouse_id=%s,"
                "planned_quantity=%s,planned_start=%s,planned_end=%s WHERE company_id=%s AND id=%s",
                [*(merged.get(field) for field in ORDER_FIELDS), scope.company_id, order_id],
            )
            _run(
                "DELETE FROM erp.production_order_requirements "
                "WHERE company_id=%s AND production_order_id=%s",
                [scope.company_id, order_id],
            )
        else:
            order_id = _rows(
                "INSERT INTO erp.production_orders(company_id,production_no,bom_id,warehouse_id,"
                "planned_quantity,planned_start,planned_end) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                [scope.company_id, *(merged.get(field) for field in ORDER_FIELDS)],
            )[0]["id"]
        _retain_requirements(scope.company_id, order_id, requirements)
        result = order_detail(scope.company_id, order_id)
        _event(scope, result, "manufacturing.order.saved", "production_order")
        _finish(receipt, result, 200 if current else 201, result_type="production_order")
        return result


def cancel_order(
    scope: CompanyScope,
    order_id: uuid.UUID,
    *,
    revision: int,
    key: str,
    reason: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not reason.strip() or len(reason.strip()) > 2000:
        raise APIError(code="MFG_REASON_REQUIRED", message="Cancellation requires a reason.")
    with transaction.atomic(durable=True):
        bind_and_verify_company(scope)
        receipt, replay = _claim(
            scope,
            "manufacturing.order.cancel",
            str(order_id),
            key,
            {"revision": revision, "reason": reason.strip()},
        )
        assert_company_write(scope.company_id, "manufacturing", "manufacturing.order.manage")
        if replay is not None:
            return replay
        _context(request_id)
        _graph_lock(scope.company_id)
        current = order_detail(scope.company_id, order_id, for_update=True)
        _revision(current, revision)
        if current["status"] != "planned":
            raise Conflict("MFG_ORDER_FROZEN", "Only an unexecuted planned order can be cancelled.")
        _run(
            "UPDATE erp.production_orders SET status='cancelled',cancellation_reason=%s "
            "WHERE company_id=%s AND id=%s",
            [reason.strip(), scope.company_id, order_id],
        )
        result = order_detail(scope.company_id, order_id)
        _event(scope, result, "manufacturing.order.cancelled", "production_order")
        _finish(receipt, result, result_type="production_order")
        return result
