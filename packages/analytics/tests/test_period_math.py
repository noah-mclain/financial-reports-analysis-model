"""Calendar arithmetic for opening balances, prior periods and actual days (D2, D4)."""

from datetime import date

import pytest

from fra_analytics.period_math import months_before


@pytest.mark.parametrize(
    ("end", "months", "expected"),
    [
        (date(2025, 12, 31), 12, date(2024, 12, 31)),
        (date(2025, 6, 30), 6, date(2024, 12, 31)),  # month ends stay month ends
        (date(2025, 3, 31), 3, date(2024, 12, 31)),
        (date(2025, 2, 28), 12, date(2024, 2, 29)),  # 2024 is a leap year
        (date(2024, 2, 29), 12, date(2023, 2, 28)),
        (date(2025, 5, 15), 3, date(2025, 2, 15)),
        (date(2025, 3, 30), 1, date(2025, 2, 28)),  # not a month end, clamped to the month
        (date(2025, 1, 31), 12, date(2024, 1, 31)),
    ],
)
def test_months_before(end: date, months: int, expected: date) -> None:
    assert months_before(end, months) == expected


def test_a_negative_count_is_an_error() -> None:
    with pytest.raises(ValueError, match="-1"):
        months_before(date(2025, 12, 31), -1)
