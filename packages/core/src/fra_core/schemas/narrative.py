"""Narrative output, structured so every number can be checked.

Each sentence carries claims of (metric, period, exact text span). The grounding checker
requires every numeric mention to sit inside a claim whose metric exists and matches at the
written precision. Direct templates have explicit origin and version metadata; the legacy
fallback result remains decodable for historical generated summaries.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas.caveat import CaveatId


class SectionHeading(StrEnum):
    OVERVIEW = "overview"
    PROFITABILITY = "profitability"
    LIQUIDITY = "liquidity"
    LEVERAGE = "leverage"
    WATCH_ITEMS = "watch_items"


class GroundingResult(StrEnum):
    UNCHECKED = "unchecked"

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


class Footnote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    caveat_id: CaveatId
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    text: str = Field(min_length=1)


class Sentence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    footnotes: list[str] = Field(default_factory=list)


class NarrativeSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: SectionHeading
    sentences: list[Sentence] = Field(default_factory=list)


class Narrative(BaseModel):
    """A complete written analysis for one document, in one language."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="en", description="'en' or 'ar'")
    sections: list[NarrativeSection] = Field(default_factory=list)
    origin: Literal["unknown", "direct_template", "generated", "fallback_template"] = "unknown"
    template_version: str | None = None
    footnotes: list[Footnote] = Field(default_factory=list)
    model_id: str | None = None
    adapter_id: str | None = None
    grounding: GroundingResult = GroundingResult.UNCHECKED

    @model_validator(mode="after")
    def _origin_contract(self) -> Narrative:
        if self.origin == "direct_template" and (
            not self.template_version
            or self.model_id is not None
            or self.adapter_id is not None
            or self.grounding is GroundingResult.FALLBACK_TEMPLATE
        ):
            raise ValueError(
                "direct_template requires a version, absent model/adapter ids "
                "and no generation failure"
            )
        ids = [note.id for note in self.footnotes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate narrative footnotes")
        if any(
            ref not in ids
            for section in self.sections
            for sentence in section.sentences
            for ref in sentence.footnotes
        ):
            raise ValueError("sentence names an unknown footnote")
        return self

    @property
    def cited_metric_ids(self) -> set[str]:
        return {
            claim.metric_id
            for section in self.sections
            for sentence in section.sentences
            for claim in sentence.claims
        }
