from decimal import Decimal as D

import pytest

from apps.sales.return_calculations import (
    preview_return,
    refundable_balance,
    replacement_difference,
)

pytestmark = pytest.mark.unit


def test_partial_returns_consume_final_money_and_cost_residue() -> None:
    gross = D(0)
    cost = D(0)
    for returned in range(3):
        result = preview_return(
            sold_quantity=D(3),
            returned_quantity=D(returned),
            requested_quantity=D(1),
            original_gross=D("10.00"),
            original_issue_cost=D("1.000000"),
            prior_return_gross=gross,
            prior_return_cost=cost,
            currency_precision=2,
        )
        gross += result.gross_value
        cost += result.historical_cost
    assert result.remaining_quantity == 0
    assert gross == D("10.00")
    assert cost == D("1.000000")


@pytest.mark.parametrize("requested", [D(0), D(-1), D(2), D("NaN"), D("Infinity")])
def test_invalid_return_quantities_rejected(requested: D) -> None:
    with pytest.raises(ValueError):
        preview_return(
            sold_quantity=D(2),
            returned_quantity=D(1),
            requested_quantity=requested,
            original_gross=D(20),
            original_issue_cost=D(12),
            prior_return_gross=D(10),
            prior_return_cost=D(6),
            currency_precision=2,
        )


def test_inconsistent_prior_cost_is_rejected() -> None:
    with pytest.raises(ValueError, match="historical"):
        preview_return(
            sold_quantity=D(2),
            returned_quantity=D(1),
            requested_quantity=D(1),
            original_gross=D(20),
            original_issue_cost=D(12),
            prior_return_gross=D(10),
            prior_return_cost=D(5),
            currency_precision=2,
        )


@pytest.mark.parametrize(
    "cash,credit,paid,expected", [(100, 40, 10, 30), (20, 40, 5, 15), (0, 40, 0, 0)]
)
def test_refund_requires_both_cash_and_credit(
    cash: int, credit: int, paid: int, expected: int
) -> None:
    assert refundable_balance(
        eligible_cash_received=D(cash), eligible_credit=D(credit), refunded=D(paid)
    ) == D(expected)


def test_over_refund_rejected() -> None:
    with pytest.raises(ValueError):
        refundable_balance(eligible_cash_received=D(20), eligible_credit=D(40), refunded=D(21))


@pytest.mark.parametrize("replacement,collect,credit", [(120, 20, 0), (80, 0, 20), (100, 0, 0)])
def test_replacement_differences(replacement: int, collect: int, credit: int) -> None:
    result = replacement_difference(approved_return_credit=D(100), replacement_gross=D(replacement))
    assert result.collect == D(collect)
    assert result.refund_or_credit == D(credit)
