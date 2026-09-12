"""Narrative output, structured so every number can be checked.

Each sentence carries claims of (metric, period, exact text span). The grounding checker
requires every numeric mention to sit inside a claim whose metric exists and matches at the
written precision. Failures get one retry, then fall back to a fixed template.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SectionHeading(StrEnum):
    OVERVIEW = "overview"
    PROFITABILITY = "profitability"
    LIQUIDITY = "liquidity"
    LEVERAGE = "leverage"
    WATCH_ITEMS = "watch_items"


class GroundingResult(StrEnum):
    PASSED = "passed"
    """Every numeric mention matched a computed metric."""

    FALLBACK_TEMPLATE = "fallback_template"
    """Generation failed the check twice, so deterministic sentences were used instead."""


class Claim(BaseModel):
    """An assertion that a piece of text states the value of one metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str
    period_key: str
    text_span: str = Field(
        min_length=1,
        description="The exact substring of the sentence that states the value",
    )


class Sentence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)


class NarrativeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: SectionHeading
    sentences: list[Sentence] = Field(default_factory=list)


class Narrative(BaseModel):
    """A complete written analysis for one document, in one language."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="en", description="'en' or 'ar'")
    sections: list[NarrativeSection] = Field(default_factory=list)
    model_id: str
    adapter_id: str | None = None
    grounding: GroundingResult = GroundingResult.PASSED

    @property
    def cited_metric_ids(self) -> set[str]:
        return {
            claim.metric_id
            for section in self.sections
            for sentence in section.sentences
            for claim in sentence.claims
        }
