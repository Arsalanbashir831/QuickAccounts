"""Retain linked historical return cost; never substitute warehouse-average cost."""

import uuid
from decimal import Decimal
from typing import Any

from apps.inventory.cost_layers import locked_layers, record_layer_uses
from apps.inventory.costing import CostingError, IssueCost
from apps.inventory.layer_costing import LayerBalance, LayerUse, allocate_average_issue
from apps.payments.services import _rows
from common.api.errors import Conflict


def prepare_return_costing(
    company: uuid.UUID,
    warehouse: uuid.UUID,
    item: uuid.UUID,
    line: uuid.UUID,
    position: dict[str, Any],
    *,
    basis_quantity: Decimal,
    basis_value: Decimal,
    quantity: Decimal,
    value: Decimal,
    unit_cost: Decimal,
) -> list[LayerUse] | None:
    # Caller locks all positions before layers, and locks the return/source aggregate.
    layers = locked_layers(company, (warehouse, item, None))
    if layers is None:
        return None
    if (
        sum(layer.quantity for layer in layers) != position["on_hand_quantity"]
        or sum(layer.value for layer in layers) != position["value_company"]
    ):
        raise Conflict("STOCK_COST_LAYER_DRIFT", "Layers do not reconcile with returned stock.")
    claims = _rows(
        "SELECT l.id,c.quantity,c.value_company FROM erp.inventory_cost_layers l "
        "CROSS JOIN LATERAL erp.return_layer_claim(l.company_id,l.id,%s) c "
        "WHERE l.company_id=%s AND l.id=ANY(%s::uuid[]) ORDER BY l.id",
        [line, company, [layer.id for layer in layers]],
    )
    if any(
        row["quantity"] < 0
        or row["value_company"] < 0
        or (row["quantity"] == 0 and row["value_company"] != 0)
        for row in claims
    ):
        raise Conflict("RETURN_COST_PROVENANCE_REQUIRED", "Return layer ownership is inconsistent.")
    eligible = [
        LayerBalance(row["id"], row["quantity"], row["value_company"])
        for row in claims
        if row["quantity"] > 0
    ]
    if (
        sum(layer.quantity for layer in eligible) != basis_quantity
        or sum(layer.value for layer in eligible) != basis_value
    ):
        raise Conflict(
            "RETURN_COST_PROVENANCE_REQUIRED",
            "Return ownership cannot be recovered from retained layer history.",
        )
    try:
        return allocate_average_issue(
            eligible,
            IssueCost(quantity, value, unit_cost, basis_quantity - quantity, basis_value - value),
        )
    except CostingError as exc:
        raise Conflict(exc.code, str(exc)) from exc


def record_return_costing(
    company: uuid.UUID,
    movement: uuid.UUID,
    action: uuid.UUID | None,
    line: uuid.UUID,
    position: dict[str, Any],
    uses: list[LayerUse],
    *,
    basis_quantity: Decimal,
    basis_value: Decimal,
    quantity: Decimal,
    value: Decimal,
    unit_cost: Decimal,
    currency: str,
    precision: int,
) -> dict[str, Any]:
    result = _rows(
        "INSERT INTO erp.inventory_return_cost_basis(company_id,movement_id,return_stock_action_id,"
        "return_line_id,currency_code,currency_precision,scope_quantity,scope_value_company,"
        "reserved_quantity,basis_quantity,basis_value_company,issue_quantity,issue_value_company,"
        "unit_cost_company) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        [
            company,
            movement,
            action,
            line,
            currency,
            precision,
            position["on_hand_quantity"],
            position["value_company"],
            position["reserved_quantity"],
            basis_quantity,
            basis_value,
            quantity,
            value,
            unit_cost,
        ],
    )[0]
    record_layer_uses(company, movement, uses, historical_basis_id=result["id"])
    result.pop("allocation_seal_xid", None)
    result["method"] = "linked_return_history"
    return result
