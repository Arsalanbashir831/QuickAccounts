import uuid
from decimal import Decimal as D
from decimal import localcontext

import pytest

from apps.inventory.costing import CostingError, scope_average_issue
from apps.inventory.layer_costing import LayerBalance, allocate_average_issue


def test_proportional_layer_consumption_is_not_fifo() -> None:
    layers = [
        LayerBalance(uuid.UUID(int=1), D(8), D(800)),
        LayerBalance(uuid.UUID(int=2), D(2), D(400)),
    ]
    issue = scope_average_issue(
        on_hand=D(10), stock_value=D(1200), reserved=D(0), quantity=D(5), currency_precision=2
    )
    with localcontext() as context:
        context.prec = 6
        uses = allocate_average_issue(list(reversed(layers)), issue)
    assert [use.quantity for use in uses] == [D(4), D(1)]
    assert [use.value for use in uses] == [D(400), D(200)]
    assert sum(use.value for use in uses) == issue.value_company


def test_layer_full_depletion_preserves_exact_residual() -> None:
    layers = [
        LayerBalance(uuid.UUID(int=1), D(3), D("0.010001")),
        LayerBalance(uuid.UUID(int=2), D(1), D("0.009999")),
    ]
    issue = scope_average_issue(
        on_hand=D(4), stock_value=D("0.02"), reserved=D(0), quantity=D(4), currency_precision=2
    )
    uses = allocate_average_issue(layers, issue)
    assert [use.value for use in uses] == [layer.value for layer in layers]


def test_fractional_layer_quantity_requires_policy_not_silent_fifo() -> None:
    layers = [
        LayerBalance(uuid.UUID(int=1), D(1), D(100)),
        LayerBalance(uuid.UUID(int=2), D(2), D(200)),
    ]
    issue = scope_average_issue(
        on_hand=D(3), stock_value=D(300), reserved=D(0), quantity=D(1), currency_precision=2
    )
    with pytest.raises(CostingError, match="rounding policy"):
        allocate_average_issue(layers, issue)


def test_layer_drift_rejected() -> None:
    issue = scope_average_issue(
        on_hand=D(8), stock_value=D(800), reserved=D(0), quantity=D(2), currency_precision=2
    )
    with pytest.raises(CostingError, match="reconcile"):
        allocate_average_issue([LayerBalance(uuid.uuid4(), D(10), D(1000))], issue)


def test_free_layers_conserve_quantity_without_value() -> None:
    issue = scope_average_issue(
        on_hand=D(10), stock_value=D(0), reserved=D(0), quantity=D(5), currency_precision=2
    )
    uses = allocate_average_issue([LayerBalance(uuid.uuid4(), D(10), D(0))], issue)
    assert uses[0].quantity == 5
    assert uses[0].value == uses[0].unit_cost == 0


@pytest.mark.parametrize(
    "quantity,value", [(D(-1), D(100)), (D(1), D(-1)), (D("NaN"), D(0)), (D(1), D("Infinity"))]
)
def test_invalid_layer_facts_rejected(quantity: D, value: D) -> None:
    issue = scope_average_issue(
        on_hand=D(8), stock_value=D(800), reserved=D(0), quantity=D(2), currency_precision=2
    )
    with pytest.raises(CostingError, match="invalid"):
        allocate_average_issue([LayerBalance(uuid.uuid4(), quantity, value)], issue)
