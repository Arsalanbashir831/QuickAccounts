"""Deterministic proportional attribution of an already calculated average issue."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from uuid import UUID

from apps.inventory.costing import SIX_PLACES, CostingError, IssueCost


@dataclass(frozen=True)
class LayerBalance:
    id: UUID
    quantity: Decimal
    value: Decimal


@dataclass(frozen=True)
class LayerUse:
    layer_id: UUID
    basis_quantity: Decimal
    basis_value: Decimal
    preceding_value: Decimal
    quantity: Decimal
    value: Decimal
    unit_cost: Decimal


def allocate_average_issue(layers: list[LayerBalance], issue: IssueCost) -> list[LayerUse]:
    """Exact proportional quantities; cumulative six-place value apportionment.

    Does not change the approved movement or GL value. Unrepresentable fractional
    quantities fail closed rather than silently becoming FIFO or rounding stock.
    """
    with localcontext() as context:
        context.prec = 48
        if len({layer.id for layer in layers}) != len(layers) or any(
            not layer.quantity.is_finite()
            or not layer.value.is_finite()
            or layer.quantity <= 0
            or layer.value < 0
            or layer.quantity >= Decimal("1e14")
            or layer.value >= Decimal("1e14")
            or layer.quantity.quantize(SIX_PLACES) != layer.quantity
            or layer.value.quantize(SIX_PLACES) != layer.value
            for layer in layers
        ):
            raise CostingError("INVALID_COST_LAYERS", "Layer balances are invalid.")
        total_quantity = sum((layer.quantity for layer in layers), Decimal(0))
        total_value = sum((layer.value for layer in layers), Decimal(0))
        if (
            total_quantity != issue.quantity + issue.remaining_quantity
            or total_value != issue.value_company + issue.remaining_value_company
        ):
            raise CostingError("STOCK_COST_LAYER_DRIFT", "Cost layers do not reconcile with stock.")
        result: list[LayerUse] = []
        preceding = Decimal(0)
        allocated_value = Decimal(0)
        for layer in sorted(layers, key=lambda row: row.id):
            quantity = layer.quantity * issue.quantity / total_quantity
            if quantity.quantize(SIX_PLACES) != quantity:
                raise CostingError(
                    "STOCK_LAYER_ROUNDING_REQUIRED",
                    "Proportional layer quantity needs a reviewed rounding policy.",
                )
            cumulative_value = (
                ((preceding + layer.value) * issue.value_company / total_value).quantize(
                    SIX_PLACES, rounding=ROUND_HALF_UP
                )
                if total_value
                else Decimal(0)
            )
            value = cumulative_value - allocated_value
            unit_cost = (value / quantity).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
            if value < 0 or value > layer.value or unit_cost >= Decimal("1e14"):
                raise CostingError(
                    "STOCK_LAYER_ROUNDING_REQUIRED", "Layer cost cannot be represented."
                )
            if quantity == layer.quantity and value != layer.value:
                raise CostingError(
                    "STOCK_COST_LAYER_DRIFT", "Full depletion must consume layer residual."
                )
            result.append(
                LayerUse(
                    layer.id, layer.quantity, layer.value, preceding, quantity, value, unit_cost
                )
            )
            preceding += layer.value
            allocated_value = cumulative_value
        return result
