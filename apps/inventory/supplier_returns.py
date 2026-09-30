"""Physical linked supplier credits: issue stock without inventing a cost variance."""

import uuid
from decimal import Decimal
from typing import Any

from apps.inventory.cost_basis import locked_cost_policy, record_cost_basis
from apps.inventory.cost_layers import locked_layers, record_layer_uses
from apps.inventory.costing import CostingError, scope_average_issue
from apps.inventory.layer_costing import allocate_average_issue
from apps.payments.services import _money, _rows, _run
from common.access.scopes import CompanyScope
from common.api.errors import Conflict


def issue_supplier_return(
    scope: CompanyScope,
    credit_id: uuid.UUID,
    credit_date: object,
    warehouse_id: uuid.UUID,
    journal_id: uuid.UUID,
    exchange_rate: Decimal,
    currency: str,
    precision: int,
    serial_returns: list[dict[str, Any]],
) -> None:
    lines = _rows(
        "SELECT l.*,i.track_lots,i.track_serials,coalesce((SELECT sum(t.tax_amount-"
        "t.recoverable_amount) FROM erp.purchase_bill_tax_components t WHERE "
        "t.company_id=l.company_id AND t.purchase_bill_line_id=l.id),0) nonrecoverable_tax "
        "FROM erp.purchase_bill_lines l JOIN erp.items i ON i.company_id=l.company_id "
        "AND i.id=l.item_id WHERE l.company_id=%s AND l.purchase_bill_id=%s "
        "AND i.item_kind='stock' ORDER BY l.item_id,l.id FOR UPDATE OF l",
        [scope.company_id, credit_id],
    )
    if any(line["track_lots"] for line in lines):
        raise Conflict(
            "TRACKED_SUPPLIER_RETURN_REQUIRED",
            "Lot-tracked supplier returns require a separate physical return workflow.",
        )
    selected = {entry["purchase_bill_line_id"]: entry["serial_ids"]
                for entry in serial_returns}
    tracked_ids = {line["id"] for line in lines if line["track_serials"]}
    if len(selected) != len(serial_returns) or set(selected) != tracked_ids:
        raise Conflict(
            "SUPPLIER_SERIAL_SELECTION_REQUIRED",
            "Select exact serials for every serialized supplier-credit line.",
        )
    _run(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))",
        [f"inventory-rebuild:{scope.company_id}"],
    )
    warehouses = _rows(
        "SELECT id FROM erp.warehouses WHERE company_id=%s AND id=%s AND is_active FOR SHARE",
        [scope.company_id, warehouse_id],
    )
    if not warehouses:
        raise Conflict("INVALID_WAREHOUSE", "Supplier return warehouse is unavailable.")
    for item in sorted({line["item_id"] for line in lines}, key=str):
        if all(line["track_serials"] for line in lines if line["item_id"] == item):
            continue
        if not _rows(
            "SELECT id FROM erp.inventory_positions WHERE company_id=%s "
            "AND warehouse_id=%s AND item_id=%s AND lot_id IS NULL FOR UPDATE",
            [scope.company_id, warehouse_id, item],
        ):
            raise Conflict("SUPPLIER_RETURN_STOCK_UNAVAILABLE", "The return stock scope is empty.")
    policy = locked_cost_policy(scope)
    for line in lines:
        if line["track_serials"]:
            serial_ids = selected[line["id"]]
            if line["quantity"] != len(serial_ids) or len(set(serial_ids)) != len(serial_ids):
                raise Conflict(
                    "SUPPLIER_SERIAL_COUNT_MISMATCH",
                    "Supplier credit requires one distinct selected serial per unit.",
                )
            serials = _rows(
                "SELECT s.id,s.lot_id,s.item_id,s.current_warehouse_id,"
                "m.source_type AS receipt_source_type,m.source_line_id AS receipt_line_id,"
                "p.on_hand_quantity,p.reserved_quantity,p.value_company "
                "FROM erp.inventory_serials s "
                "JOIN erp.stock_movements m ON m.company_id=s.company_id "
                "AND m.id=s.first_receipt_movement_id "
                "JOIN erp.inventory_positions p ON p.company_id=s.company_id "
                "AND p.item_id=s.item_id AND p.lot_id=s.lot_id "
                "AND p.warehouse_id=s.current_warehouse_id "
                "WHERE s.company_id=%s AND s.id=ANY(%s::uuid[]) "
                "ORDER BY s.id FOR UPDATE OF s,p",
                [scope.company_id, serial_ids],
            )
            expected = _money(
                (line["net_amount"] + line["nonrecoverable_tax"]) * exchange_rate, precision
            )
            if len(serials) != len(serial_ids) or sum(
                (row["value_company"] for row in serials), Decimal(0)
            ) != expected:
                raise Conflict(
                    "SUPPLIER_RETURN_VARIANCE_POLICY_REQUIRED",
                    "Selected serial inventory cost must match the linked supplier credit.",
                )
            for serial in serials:
                if (
                    serial["item_id"] != line["item_id"]
                    or serial["current_warehouse_id"] != warehouse_id
                    or serial["receipt_source_type"] != "purchase_bill"
                    or serial["receipt_line_id"] != line["credit_of_bill_line_id"]
                    or serial["on_hand_quantity"] != 1
                    or serial["reserved_quantity"] != 0
                ):
                    raise Conflict(
                        "SUPPLIER_RETURN_ORIGIN_REQUIRED",
                        "Each selected serial must be available from the original bill receipt.",
                    )
                try:
                    cost = scope_average_issue(
                        on_hand=serial["on_hand_quantity"],
                        reserved=serial["reserved_quantity"],
                        stock_value=serial["value_company"],
                        quantity=Decimal(1),
                        currency_precision=precision,
                    )
                    layers = locked_layers(
                        scope.company_id, (warehouse_id, line["item_id"], serial["lot_id"])
                    )
                    uses = allocate_average_issue(layers, cost) if layers is not None else None
                except CostingError as exc:
                    raise Conflict(exc.code, str(exc)) from exc
                movement = uuid.uuid4()
                _run(
                    "INSERT INTO erp.stock_movements(id,company_id,event_key,occurred_at,"
                    "warehouse_id,item_id,lot_id,movement_kind,quantity_delta,"
                    "unit_cost_company,value_delta_company,source_type,source_id,"
                    "source_line_id,journal_entry_id) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s,'issue',-1,%s,%s,'purchase_bill',%s,%s,%s)",
                    [
                        movement, scope.company_id,
                        f"supplier-return:{credit_id}:{line['id']}:{serial['id']}",
                        credit_date, warehouse_id, line["item_id"], serial["lot_id"],
                        cost.unit_cost_company, -cost.value_company,
                        credit_id, line["id"], journal_id,
                    ],
                )
                record_cost_basis(
                    scope, movement, policy["id"],
                    {
                        "basis_quantity": serial["on_hand_quantity"],
                        "basis_value_company": serial["value_company"],
                        "reserved_quantity": serial["reserved_quantity"],
                        "issue_quantity": Decimal(1),
                        "issue_value_company": cost.value_company,
                        "unit_cost_company": cost.unit_cost_company,
                    },
                    currency_code=currency, currency_precision=precision,
                )
                if uses is not None:
                    record_layer_uses(scope.company_id, movement, uses)
            continue
        origins = _rows(
            "SELECT m.* FROM erp.stock_movements m WHERE m.company_id=%s "
            "AND m.source_type='purchase_bill' AND m.source_line_id=%s AND m.quantity_delta>0",
            [scope.company_id, line["credit_of_bill_line_id"]],
        )
        value = _money((line["net_amount"] + line["nonrecoverable_tax"]) * exchange_rate, precision)
        if (
            len(origins) != 1
            or origins[0]["item_id"] != line["item_id"]
            or origins[0]["lot_id"] is not None
            or origins[0]["quantity_delta"] < line["credit_quantity_offset"] + line["quantity"]
            or _money(
                origins[0]["value_delta_company"]
                * (line["credit_quantity_offset"] + line["quantity"])
                / origins[0]["quantity_delta"],
                precision,
            )
            - _money(
                origins[0]["value_delta_company"]
                * line["credit_quantity_offset"]
                / origins[0]["quantity_delta"],
                precision,
            )
            != value
        ):
            raise Conflict(
                "SUPPLIER_RETURN_ORIGIN_REQUIRED",
                "Return quantity, cost and warehouse must match the linked bill receipt.",
            )
        if origins[0]["warehouse_id"] != warehouse_id and not _rows(
            "SELECT 1 FROM erp.stock_movements inbound JOIN erp.stock_movements outbound "
            "ON outbound.company_id=inbound.company_id AND outbound.inventory_document_line_id="
            "inbound.inventory_document_line_id "
            "AND outbound.quantity_delta=-inbound.quantity_delta "
            "AND outbound.value_delta_company=-inbound.value_delta_company "
            "JOIN erp.inventory_documents d "
            "ON d.company_id=inbound.company_id AND d.id=inbound.source_id "
            "WHERE inbound.company_id=%s AND inbound.source_type='inventory_document' "
            "AND inbound.movement_kind='transfer' AND d.document_kind='transfer' "
            "AND d.status='posted' "
            "AND inbound.warehouse_id=%s AND inbound.item_id=%s AND inbound.lot_id IS NULL "
            "AND inbound.quantity_delta=%s AND inbound.value_delta_company=%s "
            "AND outbound.warehouse_id=%s AND outbound.item_id=%s AND outbound.lot_id IS NULL "
            "AND outbound.quantity_delta=-%s AND outbound.value_delta_company=-%s "
            "AND NOT EXISTS(SELECT 1 FROM erp.stock_movements other WHERE other.company_id=%s "
            "AND other.warehouse_id=%s AND other.item_id=%s AND other.lot_id IS NULL "
            "AND other.quantity_delta>0 AND other.id<>%s) "
            "AND NOT EXISTS(SELECT 1 FROM erp.stock_movements other WHERE other.company_id=%s "
            "AND other.warehouse_id=%s AND other.item_id=%s AND other.lot_id IS NULL "
            "AND other.quantity_delta>0 AND other.id<>inbound.id) "
            "AND NOT EXISTS(SELECT 1 FROM erp.stock_movements other WHERE other.company_id=%s "
            "AND other.warehouse_id=%s AND other.item_id=%s AND other.lot_id IS NULL "
            "AND other.quantity_delta<0)",
            [
                scope.company_id,
                warehouse_id,
                line["item_id"],
                line["quantity"],
                value,
                origins[0]["warehouse_id"],
                line["item_id"],
                line["quantity"],
                value,
                scope.company_id,
                origins[0]["warehouse_id"],
                line["item_id"],
                origins[0]["id"],
                scope.company_id,
                warehouse_id,
                line["item_id"],
                scope.company_id,
                warehouse_id,
                line["item_id"],
            ],
        ):
            raise Conflict(
                "SUPPLIER_RETURN_TRANSFER_PROVENANCE_REQUIRED",
                "Return warehouse needs one provable transfer of the original stock.",
            )
        position = _rows(
            "SELECT * FROM erp.inventory_positions WHERE company_id=%s "
            "AND warehouse_id=%s AND item_id=%s AND lot_id IS NULL FOR UPDATE",
            [scope.company_id, warehouse_id, line["item_id"]],
        )[0]
        try:
            cost = scope_average_issue(
                on_hand=position["on_hand_quantity"],
                reserved=position["reserved_quantity"],
                stock_value=position["value_company"],
                quantity=line["quantity"],
                currency_precision=precision,
            )
            if cost.value_company != value:
                raise Conflict(
                    "SUPPLIER_RETURN_VARIANCE_POLICY_REQUIRED",
                    "Issue cost differs from the supplier credit; a variance policy is required.",
                )
            layers = locked_layers(scope.company_id, (warehouse_id, line["item_id"], None))
            uses = allocate_average_issue(layers, cost) if layers is not None else None
        except CostingError as exc:
            raise Conflict(exc.code, str(exc)) from exc
        movement = uuid.uuid4()
        _run(
            "INSERT INTO erp.stock_movements(id,company_id,event_key,occurred_at,warehouse_id,"
            "item_id,movement_kind,quantity_delta,unit_cost_company,value_delta_company,"
            "source_type,source_id,source_line_id,journal_entry_id) VALUES "
            "(%s,%s,%s,%s,%s,%s,'issue',%s,%s,%s,'purchase_bill',%s,%s,%s)",
            [
                movement,
                scope.company_id,
                f"supplier-return:{credit_id}:{line['id']}",
                credit_date,
                warehouse_id,
                line["item_id"],
                -line["quantity"],
                cost.unit_cost_company,
                -value,
                credit_id,
                line["id"],
                journal_id,
            ],
        )
        record_cost_basis(
            scope,
            movement,
            policy["id"],
            {
                "basis_quantity": position["on_hand_quantity"],
                "basis_value_company": position["value_company"],
                "reserved_quantity": position["reserved_quantity"],
                "issue_quantity": line["quantity"],
                "issue_value_company": value,
                "unit_cost_company": cost.unit_cost_company,
            },
            currency_code=currency,
            currency_precision=precision,
        )
        if uses is not None:
            record_layer_uses(scope.company_id, movement, uses)
