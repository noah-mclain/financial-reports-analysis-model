"""The one division every ratio goes through, and the flags it raises.

Zero denominator: null, ``undefined_zero_denominator``. Negative denominator: null with
``undefined_negative_denominator``, or, where the policy says so (a margin, D1), computed and
flagged ``negative_base``. A quotient that overflows is null with ``arithmetic_result``; one
that a float cannot hold, or that underflows to zero from a nonzero numerator, is null with
``unrepresentable_result``. A missing part is null without a flag: the caller already named it.
"""

from __future__ import annotations

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
)
from math import isfinite
from typing import Literal

NegativeDenominator = Literal["compute_and_flag", "null"]

# Every setting and trap is explicit, so the caller's decimal context never leaks in.
_CONTEXT = Context(
    prec=34,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
)


def divide(
    numerator: Decimal | None,
    denominator: Decimal | None,
    *,
    negative: NegativeDenominator,
    flags: list[str],
) -> Decimal | None:
    """``numerator / denominator``, appending to ``flags`` the reason when there is no result."""
    if denominator is not None and denominator.is_zero():
        flags.append("undefined_zero_denominator")
        return None
    if numerator is None or denominator is None:
        return None
    if denominator.is_signed():
        if negative == "null":
            flags.append("undefined_negative_denominator")
            return None
        flags.append("negative_base")
    try:
        ratio = _CONTEXT.divide(numerator, denominator)
    except DecimalException:
        flags.append("arithmetic_result")
        return None
    as_float = float(ratio)
    if not isfinite(as_float) or (as_float == 0.0 and not numerator.is_zero()):
        flags.append("unrepresentable_result")
        return None
    return ratio
