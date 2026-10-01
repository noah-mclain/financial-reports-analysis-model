"""Unit caveats in structure: an assumed scale, a fallback currency (blueprint 13).

A statement that prints no unit multiplier is used at scale 1. That is normal for filings in
whole currency units, but it cannot be told apart from a multiplier that was lost, so the
statement carries a caveat that later stages turn into a footnote on every amount. Figures too
small to be single units are flagged as implausible, which holds the statement.

A statement does not take a multiplier from another table of its document. That was tried: in
Edita 2025 EAS a notes table printed in thousands is classified as a second income statement,
and its multiplier would have been applied to three statements printed in single pounds.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from statistics import median

from fra_core.schemas import Caveat, LineItem
from fra_ingest.metadata import Metadata

# A statement of five figures or more whose median is under this, in single units, is not
# plausible: a lost multiplier or a broken read. Set from the golden set, where the smallest
# median of a readable statement with no printed multiplier is 3.4 x 10^7.
IMPLAUSIBLE_MEDIAN = Decimal(10_000)
_MIN_FIGURES = 5
_NOT_AMOUNTS = {"per_share", "percent", "implausible_magnitude"}


def median_figure(items: Sequence[LineItem]) -> tuple[Decimal | None, int]:
    """The median size of the statement's amounts as printed, and how many there are. Zeros,
    per-share and percentage cells and merged figures are left out."""
    sizes = [
        abs(c.reported)
        for item in items
        for c in item.cells
        if c.reported and not _NOT_AMOUNTS & set(c.flags)
    ]
    return (median(sizes), len(sizes)) if sizes else (None, 0)


def _source(meta: Metadata, kind: str) -> str:
    return next((s.split(":", 1)[1] for s in meta.signals if s.startswith(f"{kind}:")), "none")


def unit_caveats(meta: Metadata, items: Sequence[LineItem]) -> tuple[list[Caveat], list[str]]:
    """The caveats a statement's metadata calls for, and the flags that go with them."""
    caveats: list[Caveat] = []
    flags: list[str] = []
    if "scale_missing" in meta.flags:
        middle, count = median_figure(items)
        evidence = {"currency_source": _source(meta, "currency")}
        if middle is not None:
            evidence["median_figure"] = str(int(middle))
        caveats.append(Caveat(id="scale_assumed_units", evidence=evidence))
        if middle is not None and count >= _MIN_FIGURES and middle < IMPLAUSIBLE_MEDIAN:
            flags.append("scale_implausible")
    for flag in ("currency_from_domicile", "currency_inferred"):
        if flag in meta.flags:
            caveats.append(Caveat(id=flag, evidence={"currency": meta.currency or ""}))
    return caveats, flags
