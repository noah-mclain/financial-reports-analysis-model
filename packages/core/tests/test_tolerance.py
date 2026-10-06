"""The identity tolerance rule (D6): n x 0.5 reported units, n the number of addends."""

from decimal import Decimal, getcontext, localcontext

import pytest

from fra_core.tolerance import HALF_UNIT, rounding_tolerance, within_rounding


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


@pytest.mark.parametrize(("count", "expected"), [(0, "0"), (1, "0.5"), (2, "1.0"), (3, "1.5")])
@pytest.mark.parametrize("sign", [-1, 1])
def test_rounding_boundaries_are_inclusive(count: int, expected: str, sign: int) -> None:
    tolerance = rounding_tolerance(count)
    assert Decimal("0.5") == HALF_UNIT
    assert tolerance == Decimal(expected)
    assert within_rounding(sign * Decimal(expected), count)
    assert not within_rounding(sign * (Decimal(expected) + Decimal("0.000001")), count)


def test_negative_addend_count_is_rejected_without_changing_context() -> None:
    before = getcontext().copy()
    with localcontext() as context:
        context.prec = 3
        with pytest.raises(ValueError, match=r"zero or more, got -1"):
            rounding_tolerance(-1)
        assert context.prec == 3
    assert getcontext().prec == before.prec
    assert getcontext().flags == before.flags
    assert getcontext().traps == before.traps


def test_high_precision_difference_is_not_converted_or_quantized() -> None:
    with localcontext() as context:
        context.prec = 50
        difference = Decimal("0.5000000000000000000000000000000000000001")
        assert not within_rounding(difference, 1)
        assert within_rounding(-Decimal("0.4999999999999999999999999999999999999999"), 1)
        assert context.prec == 50
