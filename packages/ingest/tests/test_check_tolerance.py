"""Reported-unit rounding policy shared by ingest checks (D6)."""

from decimal import Decimal, getcontext, localcontext

import pytest

from fra_ingest.check_tolerance import ROUNDING_UNIT, rounding_tolerance, within_tolerance


@pytest.mark.parametrize(("count", "expected"), [(0, "0"), (1, "0.5"), (2, "1.0"), (3, "1.5")])
@pytest.mark.parametrize("sign", [-1, 1])
def test_rounding_boundaries_are_inclusive(count: int, expected: str, sign: int) -> None:
    tolerance = rounding_tolerance(count)
    assert Decimal("0.5") == ROUNDING_UNIT
    assert tolerance == Decimal(expected)
    assert within_tolerance(sign * Decimal(expected), tolerance)
    assert not within_tolerance(sign * (Decimal(expected) + Decimal("0.000001")), tolerance)


def test_negative_addend_count_is_rejected_without_changing_context() -> None:
    before = getcontext().copy()
    with localcontext() as context:
        context.prec = 3
        with pytest.raises(ValueError, match=r"addend_count.*-1"):
            rounding_tolerance(-1)
        assert context.prec == 3
    assert getcontext().prec == before.prec
    assert getcontext().flags == before.flags
    assert getcontext().traps == before.traps


def test_high_precision_difference_is_not_converted_or_quantized() -> None:
    with localcontext() as context:
        context.prec = 50
        difference = Decimal("0.5000000000000000000000000000000000000001")
        assert not within_tolerance(difference, rounding_tolerance(1))
        assert within_tolerance(
            -Decimal("0.4999999999999999999999999999999999999999"), rounding_tolerance(1)
        )
        assert context.prec == 50
