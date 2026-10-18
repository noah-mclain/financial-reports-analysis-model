"""Whether a statement passed its checks or is held for a person to look at.

Produced by ``fra_ingest.review``; read by the worker and the API, which do not import ingest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Warnings that only record how a cell's label or blank was recovered. A person sees them on the
# review; they say nothing about the figures, so they neither hold a statement nor reach the model.
PROVENANCE_WARNINGS = ("label_merged", "label_words_from_page", "blank_confirmed")


# Notes on how a number was read, kept on the cell for people. They are not doubts about a
# figure, so they do not hold a statement or reach the model, and unlike the warnings above they
# are not listed on the review.
READ_NOTES = (
    "digits_reversed",
    "arabic_indic_digits",
    "parentheses_negative",
    "dash_as_zero",
)


def is_provenance_flag(flag: str) -> bool:
    """A flag on a cell or metric that records how the figure or its label was read."""
    return flag in READ_NOTES or is_provenance_warning(flag)


def is_provenance_warning(warning: str) -> bool:
    """A provenance warning, with or without its numeric ``:count`` suffix."""
    name, colon, count = warning.partition(":")
    return name in PROVENANCE_WARNINGS and (not colon or count.isdecimal())


class StatementReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str
    status: Literal["passed", "needs_review"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    numeric_cells: int = Field(ge=0)
    checked_cells: int = Field(ge=0)
    flagged_cells: int = Field(ge=0)
