"""The identity tolerance rule (D6): n x 0.5 reported units, n the number of addends."""

from decimal import Decimal

import pytest

from fra_core.tolerance import rounding_tolerance, within_rounding


@pytest.mark.parametrize(("addends", "expected"), [(0, "0.0"), (1, "0.5"), (2, "1.0"), (7, "3.5")])
def test_tolerance_is_half_a_unit_per_addend(addends: int, expected: str) -> None:
    assert rounding_tolerance(addends) == Decimal(expected)


def test_seven_addends_off_by_three_pass_and_off_by_four_fail() -> None:
    assert within_rounding(Decimal("3"), 7)
    assert within_rounding(Decimal("-3.5"), 7)
    assert not within_rounding(Decimal("4"), 7)
    assert not within_rounding(Decimal("-4"), 7)


def test_a_negative_count_is_an_error_naming_the_count() -> None:
    with pytest.raises(ValueError, match="zero or more, got -1"):
        rounding_tolerance(-1)
