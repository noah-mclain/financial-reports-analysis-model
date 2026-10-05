"""The unit-caveat stage: what a statement's scale and currency assumptions mean for a metric.

Structure records what it assumed (blueprint 13): a caveat when no multiplier or no currency is
printed, and flags such as ``scale_missing``, ``scale_conflict`` and ``currency_missing``. Here
each metric takes the ones that touch it and no others, and nothing changes a value:

- A currency amount or a per-share amount carries the caveat ids of every statement it was
  built from, and a flag ``<flag>:<statement id>`` for each unit flag those statements hold.
- A ratio, a times figure or a number of days built from one statement is scale-invariant and
  carries neither. Built from several statements it is not: an assumed scale on one of them
  decides the result, so it carries the same caveats and flags a currency amount does.
- Statements that print different currencies cannot be combined: any metric built from them is
  null, flagged ``currency_mismatch``.
- Statements printed at different scales cannot be added into an amount: a currency or
  per-share metric built from them is null, flagged ``scale_mismatch``. A ratio is computed from
  the scaled values, each as read, and carries the flag as a warning.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fra_analytics.frame import FrameStatement
from fra_core.schemas import MetricUnit
from fra_core.schemas.caveat import CaveatId

# Statement flags that say a unit or a currency was assumed, missing or contradicted.
UNIT_FLAGS = (
    "scale_missing",
    "scale_conflict",
    "scale_implausible",
    "currency_missing",
    "currency_conflict",
    "currency_from_domicile",
    "currency_inferred",
)
_AMOUNTS = (MetricUnit.CURRENCY, MetricUnit.PER_SHARE)


@dataclass(frozen=True)
class UnitNotes:
    caveats: tuple[CaveatId, ...]
    flags: tuple[str, ...]
    blocked: bool
    """The statements cannot be combined into this unit: the metric is null."""


def unit_notes(unit: MetricUnit, statements: Sequence[FrameStatement]) -> UnitNotes:
    """The caveats and flags a metric of ``unit`` takes from the statements it was built from."""
    ordered = sorted({s.id: s for s in statements}.values(), key=lambda s: s.id)
    flags: list[str] = []
    blocked = False
    if len({s.currency for s in ordered}) > 1:
        flags.append("currency_mismatch")
        blocked = True
    if len({s.scale for s in ordered}) > 1:
        flags.append("scale_mismatch")
        blocked = blocked or unit in _AMOUNTS
    caveats: set[CaveatId] = set()
    if (unit in _AMOUNTS or len(ordered) > 1) and not blocked:
        for statement in ordered:
            caveats.update(statement.caveats)
            flags.extend(f"{f}:{statement.id}" for f in UNIT_FLAGS if f in statement.flags)
    return UnitNotes(caveats=tuple(sorted(caveats)), flags=tuple(flags), blocked=blocked)
