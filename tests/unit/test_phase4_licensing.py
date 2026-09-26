import datetime as dt

import pytest

from apps.licensing.platform_services import _add_term


@pytest.mark.unit
@pytest.mark.parametrize(
    ("start", "timezone_name", "expected"),
    [
        (
            dt.datetime(2025, 1, 31, 15, 0, tzinfo=dt.UTC),
            "America/New_York",
            dt.datetime(2025, 2, 28, 15, 0, tzinfo=dt.UTC),
        ),
        (
            dt.datetime(2024, 1, 31, 15, 0, tzinfo=dt.UTC),
            "America/New_York",
            dt.datetime(2024, 2, 29, 15, 0, tzinfo=dt.UTC),
        ),
        (
            dt.datetime(2026, 2, 28, 15, 0, tzinfo=dt.UTC),
            "America/New_York",
            dt.datetime(2026, 3, 28, 14, 0, tzinfo=dt.UTC),
        ),
    ],
)
def test_monthly_term_uses_calendar_months_in_the_billing_timezone(
    start: dt.datetime,
    timezone_name: str,
    expected: dt.datetime,
) -> None:
    assert _add_term(start, "month", 1, timezone_name) == expected


@pytest.mark.unit
def test_lifetime_term_has_no_expiry() -> None:
    start = dt.datetime(2026, 9, 27, tzinfo=dt.UTC)
    assert _add_term(start, "lifetime", None, "UTC") is None
