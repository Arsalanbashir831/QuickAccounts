"""Pure return previews; posting must supply locked, authoritative source facts.

These calculations do not authorize a credit, refund, or inventory movement.
Tax components must be apportioned individually from original snapshots by the
posting workflow; gross value alone is not sufficient to construct tax entries.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext


def _nonnegative(*values: Decimal) -> None:
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("Amounts and quantities must be finite and nonnegative.")


def _quantum(precision: int) -> Decimal:
    if not 0 <= precision <= 6:
        raise ValueError("Precision must be between zero and six.")
    return Decimal(1).scaleb(-precision)


def _share(total: Decimal, quantity: Decimal, sold: Decimal, precision: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return (total * quantity / sold).quantize(_quantum(precision), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ReturnPreview:
    remaining_quantity: Decimal
    gross_value: Decimal
    historical_cost: Decimal


def preview_return(
    *,
    sold_quantity: Decimal,
    returned_quantity: Decimal,
    requested_quantity: Decimal,
    original_gross: Decimal,
    original_issue_cost: Decimal,
    prior_return_gross: Decimal,
    prior_return_cost: Decimal,
    currency_precision: int,
) -> ReturnPreview:
    """Use cumulative apportionment so the final return consumes rounding residue.

    Quantities refer to the same original line/UOM. Original issue cost is the
    positive historical value of that line's fulfilled stock movements, never
    a current item price or average cost. Prior values are immutable posted
    physical-return facts, excluding unrelated price adjustments.
    """
    _nonnegative(
        sold_quantity,
        returned_quantity,
        requested_quantity,
        original_gross,
        original_issue_cost,
        prior_return_gross,
        prior_return_cost,
    )
    money_quantum = _quantum(currency_precision)
    if any(
        value != value.quantize(money_quantum) for value in (original_gross, prior_return_gross)
    ):
        raise ValueError("Source gross values exceed currency precision.")
    if any(
        value != value.quantize(_quantum(6)) for value in (original_issue_cost, prior_return_cost)
    ):
        raise ValueError("Source cost values exceed inventory precision.")
    if sold_quantity <= 0 or requested_quantity <= 0:
        raise ValueError("Sold and requested quantities must be positive.")
    if returned_quantity + requested_quantity > sold_quantity:
        raise ValueError("Return exceeds the remaining sold quantity.")
    if prior_return_gross != _share(
        original_gross, returned_quantity, sold_quantity, currency_precision
    ):
        raise ValueError("Prior returned gross does not reconcile to source quantities.")
    if prior_return_cost != _share(original_issue_cost, returned_quantity, sold_quantity, 6):
        raise ValueError("Prior returned cost does not reconcile to historical quantities.")
    cumulative = returned_quantity + requested_quantity
    return ReturnPreview(
        remaining_quantity=sold_quantity - cumulative,
        gross_value=_share(original_gross, cumulative, sold_quantity, currency_precision)
        - prior_return_gross,
        historical_cost=_share(original_issue_cost, cumulative, sold_quantity, 6)
        - prior_return_cost,
    )


def refundable_balance(
    *, eligible_cash_received: Decimal, eligible_credit: Decimal, refunded: Decimal
) -> Decimal:
    """Withholding/non-cash settlements are not included in cash received."""
    _nonnegative(eligible_cash_received, eligible_credit, refunded)
    capacity = min(eligible_cash_received, eligible_credit)
    if refunded > capacity:
        raise ValueError("Recorded refunds exceed eligible cash or credit.")
    return capacity - refunded


@dataclass(frozen=True)
class ReplacementDifference:
    collect: Decimal
    refund_or_credit: Decimal


def replacement_difference(
    *, approved_return_credit: Decimal, replacement_gross: Decimal
) -> ReplacementDifference:
    """Compare same-currency, tax-inclusive values after approved discounts.

    A negative difference is credit entitlement, not authorization to pay cash.
    Refund eligibility must be checked separately with refundable_balance.
    """
    _nonnegative(approved_return_credit, replacement_gross)
    difference = replacement_gross - approved_return_credit
    return ReplacementDifference(
        collect=max(difference, Decimal(0)),
        refund_or_credit=max(-difference, Decimal(0)),
    )
