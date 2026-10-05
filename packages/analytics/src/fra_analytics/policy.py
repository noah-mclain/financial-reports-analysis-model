"""Explicit policy for the first margins slice, loaded at the configuration boundary."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Policy:
    """Provisional D1 margin policy; there is no implicit configuration default."""

    negative_margin_denominator: Literal["compute_and_flag", "null"]

    def __post_init__(self) -> None:
        option = self.negative_margin_denominator
        if not isinstance(option, str) or option not in ("compute_and_flag", "null"):
            raise ValueError(
                f"negative_margin_denominator must be 'compute_and_flag' or 'null', got {option!r}"
            )


def load_policy(path: Path) -> Policy:
    """Load the required setting, refusing unknown keys, sections and invalid values."""
    with path.open("rb") as source:
        raw = tomllib.load(source)
    required = {"negative_margin_denominator"}
    if set(raw) != required:
        raise ValueError(
            f"{path}: required keys {sorted(required)}; missing {sorted(required - raw.keys())}, "
            f"unknown {sorted(raw.keys() - required)}"
        )
    return Policy(negative_margin_denominator=raw["negative_margin_denominator"])
