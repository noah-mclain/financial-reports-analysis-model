"""The owner's decisions D3 and D4, read from ``configs/analytics.toml``.

D1, D2, D5 and D6 are fixed rules and live in the code that applies them. The caller names the
file: nothing here reads the environment.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

# The one table of the file; its keys are the field names of ``Policy``.
_SECTION = "metrics"


class Policy(BaseModel):
    """No defaults: a missing setting is an error, not a quiet choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    include_lease_liabilities: bool
    """D3: IFRS 16 lease liabilities count in total debt."""

    day_count_basis: Literal[360, 365]
    """D4: days in a year for DSO, DIO and DPO. Interim periods use their actual days."""


def load_policy(path: Path) -> Policy:
    """Read the settings, rejecting any key the policy does not know."""
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    values: dict[str, Any] = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            msg = f"{path}: [{section}] must be a table"
            raise ValueError(msg)
        for key, value in table.items():
            if section != _SECTION or key not in Policy.model_fields:
                msg = f"{path}: unknown setting {section}.{key}"
                raise ValueError(msg)
            values[key] = value
    try:
        return Policy.model_validate(values)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        msg = f"{path}: {problems}"
        raise ValueError(msg) from error
