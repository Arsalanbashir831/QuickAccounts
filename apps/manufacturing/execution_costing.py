"""Material-only cumulative output apportionment, with a final residual."""

from decimal import ROUND_HALF_UP, Decimal, localcontext

from apps.manufacturing.planning import MAX_QUANTITY, PlanningError


def output_cost(
    planned: Decimal,
    completed: Decimal,
    quantity: Decimal,
    material_cost: Decimal,
    allocated_cost: Decimal,
    precision: int,
) -> Decimal:
    values = (planned, completed, quantity, material_cost, allocated_cost)
    if (
        not 0 <= precision <= 6
        or any(not v.is_finite() or v < 0 or v > MAX_QUANTITY for v in values)
        or planned <= 0
        or quantity <= 0
    ):
        raise PlanningError("MFG_OUTPUT_INVALID", "Invalid output quantity or cost basis.")
    with localcontext() as context:
        context.prec = 100
        if completed + quantity > planned or any(
            v.quantize(Decimal("0.000001")) != v for v in values[:3]
        ):
            raise PlanningError(
                "MFG_OUTPUT_INVALID", "Output quantities must fit the plan and six-decimal units."
            )
        quantum = Decimal(1).scaleb(-precision)
        if material_cost.quantize(quantum) != material_cost or allocated_cost != (
            material_cost * completed / planned
        ).quantize(quantum, rounding=ROUND_HALF_UP):
            raise PlanningError("MFG_WIP_DRIFT", "Prior output costs do not reconcile.")
        cumulative = (material_cost * (completed + quantity) / planned).quantize(
            quantum, rounding=ROUND_HALF_UP
        )
        return cumulative - allocated_cost
