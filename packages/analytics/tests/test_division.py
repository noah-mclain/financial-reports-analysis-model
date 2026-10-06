"""The one ratio division: its flags are the vocabulary every metric shares."""

from decimal import Decimal

import pytest

from fra_analytics.metrics.division import divide


def run(
    numerator: str | None, denominator: str | None, negative: str
) -> tuple[Decimal | None, list[str]]:
    flags: list[str] = []
    value = divide(
        None if numerator is None else Decimal(numerator),
        None if denominator is None else Decimal(denominator),
        negative=negative,  # type: ignore[arg-type]
        flags=flags,
    )
    return value, flags


@pytest.mark.parametrize("zero", ["0", "-0"])
def test_a_zero_denominator_is_undefined_even_when_the_numerator_is_missing(zero: str) -> None:
    assert run("5", zero, "null") == (None, ["undefined_zero_denominator"])
    assert run(None, zero, "compute_and_flag") == (None, ["undefined_zero_denominator"])


def test_a_missing_part_is_null_without_a_flag() -> None:
    assert run(None, "2", "null") == (None, [])
    assert run("2", None, "null") == (None, [])


def test_a_negative_denominator_is_null_or_computed_and_flagged_by_policy() -> None:
    assert run("6", "-3", "null") == (None, ["undefined_negative_denominator"])
    assert run("6", "-3", "compute_and_flag") == (Decimal("-2"), ["negative_base"])


def test_the_quotient_is_exact_to_34_digits() -> None:
    value, flags = run("1", "3", "null")
    assert value == Decimal("0.3333333333333333333333333333333333")
    assert flags == []


def test_an_overflow_is_null_and_flagged() -> None:
    assert run("1E+999999", "1E-999999", "null") == (None, ["arithmetic_result"])


def test_a_nonzero_quotient_that_floats_cannot_hold_is_null_and_flagged() -> None:
    assert run("1E-200", "1E+200", "null") == (None, ["unrepresentable_result"])
