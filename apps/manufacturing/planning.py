"""Exact planning quantities; no stock, reservations, costs or posting effects."""

from decimal import Decimal, InvalidOperation, localcontext

SIX = Decimal("0.000001")
MAX_QUANTITY = Decimal("99999999999999.999999")


class PlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def required_quantity(
    planned_quantity: Decimal,
    output_quantity: Decimal,
    component_quantity: Decimal,
    scrap_percent: Decimal = Decimal(0),
    *,
    serial_tracked: bool = False,
) -> Decimal:
    """Component quantity is for the BOM's stated output-quantity basis.

    Scrap is an additive planning allowance, not a yield divisor or a write-off.
    No rounding or financial scrap policy is inferred.
    """
    quantities = (planned_quantity, output_quantity, component_quantity)
    if any(
        not isinstance(q, Decimal) or not q.is_finite() or not 0 < q <= MAX_QUANTITY
        for q in quantities
    ) or (
        not isinstance(scrap_percent, Decimal)
        or not scrap_percent.is_finite()
        or not 0 <= scrap_percent <= 100
    ):
        raise PlanningError("MFG_QUANTITY_INVALID", "Planning quantities must be finite and valid.")
    with localcontext() as ctx:
        ctx.prec = 100
        if any(q.quantize(SIX) != q for q in (*quantities, scrap_percent)):
            raise PlanningError(
                "MFG_QUANTITY_INVALID", "Planning inputs require six-place precision."
            )
        numerator = planned_quantity * component_quantity * (100 + scrap_percent)
        denominator = output_quantity * 100
        raw = numerator / denominator
        if raw > MAX_QUANTITY:
            raise PlanningError(
                "MFG_QUANTITY_INVALID", "Material requirement exceeds ledger range."
            )
        try:
            quantity = raw.quantize(SIX)
        except InvalidOperation as exc:
            raise PlanningError("MFG_QUANTITY_INVALID", "Material requirement is invalid.") from exc
        if quantity <= 0 or quantity * denominator != numerator:
            raise PlanningError(
                "MFG_QUANTITY_ROUNDING_REQUIRED",
                "Material requirement is not exactly representable at six places.",
            )
        if serial_tracked and quantity != quantity.to_integral_value():
            raise PlanningError(
                "MFG_SERIAL_QUANTITY_INVALID", "Serial requirements must be whole units."
            )
        return quantity
