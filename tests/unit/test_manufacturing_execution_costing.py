from decimal import Decimal as D
from decimal import localcontext

import pytest

from apps.manufacturing.execution_costing import output_cost
from apps.manufacturing.planning import PlanningError

pytestmark = pytest.mark.unit


def test_cumulative_output_cost_retains_final_residual_and_ignores_ambient_precision():
    with localcontext() as context:
        context.prec = 3
        first = output_cost(D(3), D(0), D(1), D(10), D(0), 2)
        second = output_cost(D(3), D(1), D(1), D(10), first, 2)
        third = output_cost(D(3), D(2), D(1), D(10), D("6.67"), 2)
    assert (first, second, third) == (D("3.33"), D("3.34"), D("3.33"))


@pytest.mark.parametrize("quantity", ["0", "-1", "4", "NaN", "Infinity"])
def test_invalid_output_quantity_fails_closed(quantity):
    with pytest.raises(PlanningError):
        output_cost(D(3), D(0), D(quantity), D(10), D(0), 2)


def test_prior_cost_drift_and_fractional_gl_cost_fail_closed():
    with pytest.raises(PlanningError, match="Prior output"):
        output_cost(D(3), D(1), D(1), D(10), D(3), 2)
    with pytest.raises(PlanningError):
        output_cost(D(3), D(0), D(1), D("10.001"), D(0), 2)
