"""Explicit policy for the metrics, loaded at the configuration boundary.

D1 for margins, D3 and D4 are settings; D2, D5 and D6 are fixed rules and live in the code that
applies them. The caller names the file: nothing here reads the environment, and there is no
implicit default for any setting.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Policy:
    """Every setting is required; a missing one is an error, not a quiet choice."""

    negative_margin_denominator: Literal["compute_and_flag", "null"]
    """D1 (provisional): a margin on a negative revenue is computed and flagged, or null."""

    include_lease_liabilities: bool
    """D3: IFRS 16 lease liabilities count in total debt."""

    day_count_basis: Literal[360, 365]
    """D4: days in a year for DSO, DIO and DPO. Interim periods use their actual days."""

    def __post_init__(self) -> None:
        option = self.negative_margin_denominator
        if not isinstance(option, str) or option not in ("compute_and_flag", "null"):
            raise ValueError(
                f"negative_margin_denominator must be 'compute_and_flag' or 'null', got {option!r}"
            )
        if not isinstance(self.include_lease_liabilities, bool):
            raise ValueError(
                f"include_lease_liabilities must be true or false, got "
                f"{self.include_lease_liabilities!r}"
            )
        basis = self.day_count_basis
        if isinstance(basis, bool) or not isinstance(basis, int) or basis not in (360, 365):
            raise ValueError(f"day_count_basis must be 360 or 365, got {basis!r}")


_REQUIRED = ("negative_margin_denominator", "include_lease_liabilities", "day_count_basis")


def load_policy(path: Path) -> Policy:
    """Load the required settings, refusing unknown keys, sections and invalid values."""
    with path.open("rb") as source:
        raw = tomllib.load(source)
    if set(raw) != set(_REQUIRED):
        raise ValueError(
            f"{path}: required keys {sorted(_REQUIRED)}; missing "
            f"{sorted(set(_REQUIRED) - raw.keys())}, unknown {sorted(raw.keys() - set(_REQUIRED))}"
        )
    return Policy(
        negative_margin_denominator=raw["negative_margin_denominator"],
        include_lease_liabilities=raw["include_lease_liabilities"],
        day_count_basis=raw["day_count_basis"],
    )
