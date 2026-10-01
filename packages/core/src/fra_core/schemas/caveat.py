"""What a figure rests on when the page does not say (blueprint 13).

A caveat is created once, in the structure stage, and carried unchanged: analytics decides
which metrics it reaches, narration attaches it to sentences as a footnote, and the display
marks the amounts it applies to. It never changes a value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CaveatId = Literal[
    "scale_assumed_units",
    "currency_from_domicile",
    "currency_inferred",
]


class Caveat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CaveatId = Field(
        description="scale_assumed_units: no unit multiplier is printed and the figures are "
        "taken as single units. currency_from_domicile: the currency comes from the country of "
        "incorporation. currency_inferred: the currency is the one the document names most."
    )
    scope: Literal["currency_amounts"] = Field(
        default="currency_amounts",
        description="What the caveat applies to. Ratios, times and days do not depend on it.",
    )
    evidence: dict[str, str] = Field(
        default_factory=dict, description="What the assumption rests on, for a reviewer"
    )
