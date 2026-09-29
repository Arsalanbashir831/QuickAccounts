from decimal import Decimal as D
from decimal import localcontext

import pytest

from apps.manufacturing.planning import PlanningError, required_quantity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "planned,basis,component,scrap,expected",
    [
        ("10", "2", "3", "0", "15"),
        ("10", "2", "3", "10", "16.5"),
        ("1", "1", "1", "100", "2"),
        ("0.25", "1", "0.5", "0", "0.125"),
        ("0.000001", "1", "1", "0", "0.000001"),
        ("99999999999999.999999", "1", "1", "0", "99999999999999.999999"),
    ],
)
def test_exact_requirement_formula(planned, basis, component, scrap, expected) -> None:
    assert required_quantity(D(planned), D(basis), D(component), D(scrap)) == D(expected)


@pytest.mark.parametrize("invalid", ["0", "-1", "NaN", "Infinity", "1e20", "0.0000001"])
@pytest.mark.parametrize("field", [0, 1, 2])
def test_invalid_basis_and_quantities_fail_closed(invalid: str, field: int) -> None:
    values = [D(1), D(1), D(1)]
    values[field] = D(invalid)
    with pytest.raises(PlanningError) as failure:
        required_quantity(*values)
    assert failure.value.code == "MFG_QUANTITY_INVALID"


@pytest.mark.parametrize("scrap", ["-1", "101", "NaN", "Infinity", "0.0000001"])
def test_invalid_scrap_allowance(scrap: str) -> None:
    with pytest.raises(PlanningError):
        required_quantity(D(1), D(1), D(1), D(scrap))


def test_fractional_precision_and_serial_requirements_are_not_rounded() -> None:
    with pytest.raises(PlanningError) as failure:
        required_quantity(D(1), D(3), D(1))
    assert failure.value.code == "MFG_QUANTITY_ROUNDING_REQUIRED"
    with pytest.raises(PlanningError) as serial:
        required_quantity(D(1), D(2), D(1), serial_tracked=True)
    assert serial.value.code == "MFG_SERIAL_QUANTITY_INVALID"
    assert required_quantity(D(2), D(2), D(1), serial_tracked=True) == 1


def test_caller_decimal_context_does_not_change_requirements() -> None:
    with localcontext() as context:
        context.prec = 3
        assert required_quantity(D("10000"), D("2"), D("3"), D("10")) == D("16500")
