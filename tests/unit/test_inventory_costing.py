from decimal import Decimal as D
from decimal import localcontext

import pytest

from apps.inventory.costing import CostingError, scope_average_issue

pytestmark = pytest.mark.unit


def _cost(on_hand="10", value="1000", reserved="0", quantity="2", precision=2):
    return scope_average_issue(
        on_hand=D(on_hand),
        stock_value=D(value),
        reserved=D(reserved),
        quantity=D(quantity),
        currency_precision=precision,
    )


def test_scope_average_conserves_quantity_and_value() -> None:
    result = _cost(reserved="3")
    assert result.value_company == D(200)
    assert result.unit_cost_company == D(100)
    assert result.remaining_quantity == D(8)
    assert result.remaining_value_company == D(800)


def test_full_drain_consumes_exact_residual_not_rounded_unit_cost() -> None:
    result = _cost(on_hand="3", value="100", quantity="3")
    assert result.value_company == D(100)
    assert result.unit_cost_company == D("33.333333")
    assert result.remaining_quantity == result.remaining_value_company == 0


def test_repeated_issues_preserve_residual() -> None:
    quantity, value = D(7), D("12.34")
    total = D(0)
    for issue in (D(2), D(3), D(2)):
        result = scope_average_issue(
            on_hand=quantity, stock_value=value, reserved=D(0), quantity=issue, currency_precision=6
        )
        total += result.value_company
        quantity, value = result.remaining_quantity, result.remaining_value_company
    assert total == D("12.34")
    assert quantity == value == 0


def test_costing_is_independent_of_ambient_decimal_precision() -> None:
    expected = _cost(on_hand="3", value="100", quantity="3")
    with localcontext() as context:
        context.prec = 4
        assert _cost(on_hand="3", value="100", quantity="3") == expected


@pytest.mark.parametrize(
    "facts,code",
    [
        ({"reserved": "9"}, "INSUFFICIENT_STOCK"),
        ({"quantity": "11"}, "INSUFFICIENT_STOCK"),
        ({"quantity": "0"}, "INVALID_COST_QUANTITY"),
        ({"value": "-1"}, "INVALID_COST_POSITION"),
        ({"reserved": "11"}, "INVALID_COST_POSITION"),
        ({"on_hand": "0", "value": "1"}, "INVALID_COST_POSITION"),
        ({"value": "NaN"}, "INVALID_COST_FACTS"),
        ({"quantity": "Infinity"}, "INVALID_COST_FACTS"),
        ({"quantity": "0.0000001"}, "INVALID_COST_FACTS"),
        ({"value": "100000000000000"}, "INVALID_COST_FACTS"),
        ({"precision": 7}, "INVALID_COST_PRECISION"),
        ({"on_hand": "3", "value": "100", "quantity": "1"}, "STOCK_COST_ROUNDING_REQUIRED"),
    ],
)
def test_costing_fails_closed(facts: dict, code: str) -> None:
    with pytest.raises(CostingError) as error:
        _cost(**facts)
    assert error.value.code == code


def test_free_stock_can_be_consumed_without_inventing_value() -> None:
    result = _cost(value="0")
    assert result.value_company == result.unit_cost_company == result.remaining_value_company == 0
