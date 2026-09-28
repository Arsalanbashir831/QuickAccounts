"""Pure scope-average issue valuation. Receipt-layer allocation is a separate slice."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

SIX_PLACES = Decimal("0.000001")


class CostingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IssueCost:
    quantity: Decimal
    value_company: Decimal
    unit_cost_company: Decimal
    remaining_quantity: Decimal
    remaining_value_company: Decimal


def scope_average_issue(
    *,
    on_hand: Decimal,
    stock_value: Decimal,
    reserved: Decimal,
    quantity: Decimal,
    currency_precision: int,
) -> IssueCost:
    """Consume an approved available scope without losing residual ledger value.

    Caller supplies locked facts after releasing/consuming its own reservations.
    Currency precision mismatch fails closed, matching P5.8's approved policy.
    """
    values = (on_hand, stock_value, reserved, quantity)
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise CostingError("INVALID_COST_FACTS", "Cost facts must be finite Decimal values.")
    if not isinstance(currency_precision, int) or not 0 <= currency_precision <= 6:
        raise CostingError("INVALID_COST_PRECISION", "Currency precision must be between 0 and 6.")
    with localcontext() as context:
        context.prec = 48
        if any(
            abs(value) >= Decimal("1e14") or value.quantize(SIX_PLACES) != value for value in values
        ):
            raise CostingError("INVALID_COST_FACTS", "Cost facts must fit numeric(20,6).")
        if on_hand < 0 or stock_value < 0 or reserved < 0 or reserved > on_hand:
            raise CostingError(
                "INVALID_COST_POSITION", "Position quantities/value are inconsistent."
            )
        if on_hand == 0 and stock_value != 0:
            raise CostingError("INVALID_COST_POSITION", "An empty scope cannot retain value.")
        if quantity <= 0:
            raise CostingError("INVALID_COST_QUANTITY", "Issue quantity must be positive.")
        if quantity > on_hand - reserved:
            raise CostingError("INSUFFICIENT_STOCK", "Available stock is insufficient.")
        # Draining a scope takes its exact residual, rather than multiplying a rounded unit cost.
        value = (
            stock_value
            if quantity == on_hand
            else (stock_value * quantity / on_hand).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
        )
        if value.quantize(Decimal(1).scaleb(-currency_precision), rounding=ROUND_HALF_UP) != value:
            raise CostingError(
                "STOCK_COST_ROUNDING_REQUIRED", "Cost needs a reviewed GL rounding policy."
            )
        unit_cost = (value / quantity).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
        if unit_cost >= Decimal("1e14"):
            raise CostingError("INVALID_COST_FACTS", "Unit cost does not fit numeric(20,6).")
        return IssueCost(quantity, value, unit_cost, on_hand - quantity, stock_value - value)
