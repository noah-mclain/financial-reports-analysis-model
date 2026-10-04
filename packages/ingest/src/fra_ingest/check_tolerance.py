"""Reported-unit rounding tolerance for ingest checks (D6)."""

from decimal import Decimal
from typing import Final

ROUNDING_UNIT: Final[Decimal] = Decimal("0.5")


def rounding_tolerance(addend_count: int) -> Decimal:
    if addend_count < 0:
        raise ValueError(f"addend_count must be nonnegative, got {addend_count}")
    return ROUNDING_UNIT * addend_count


def within_tolerance(difference: Decimal, tolerance: Decimal) -> bool:
    return abs(difference) <= tolerance
