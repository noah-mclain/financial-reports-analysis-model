"""The identity-check tolerance (D6), in one place for structure and analytics.

Printed figures are rounded to whole reported units, so each can be off by half a unit and a
sum of n of them by n x 0.5. A difference within that is rounding, not an error.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["HALF_UNIT", "rounding_tolerance", "within_rounding"]

HALF_UNIT = Decimal("0.5")


def rounding_tolerance(addends: int) -> Decimal:
    """The largest difference rounding alone explains across ``addends`` printed figures."""
    if addends < 0:
        msg = f"a tolerance needs a count of addends of zero or more, got {addends}"
        raise ValueError(msg)
    return HALF_UNIT * addends


def within_rounding(difference: Decimal, addends: int) -> bool:
    return abs(difference) <= rounding_tolerance(addends)
