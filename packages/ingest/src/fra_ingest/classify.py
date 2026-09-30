"""Which statement a table is, if any (spec 11, Data flow step 4).

Evidence: row labels found in the taxonomy (space-free, so Arabic labels without word
boundaries count), the statement titles and cues locate found on the page, and whether the
header binds periods. A table under a note heading, or without a period header, is not a
statement. Every decision keeps its evidence.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import StatementType
from fra_ingest.header import HeaderLayout
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_grid import Grid

_NOTE_HEADING = re.compile(r"^\s*(note|notes|إيضاح|ايضاح)\s*[\d٠-٩]", re.IGNORECASE)
_LABEL_WEIGHT = 0.6
_LABEL_SATURATION = 6
_TITLE_WEIGHT = 0.3
_CUE_WEIGHT = 0.15
_PERIOD_WEIGHT = 0.1


class TableContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title_types: tuple[StatementType, ...] = ()
    cue_types: tuple[StatementType, ...] = ()
    heading_texts: tuple[str, ...] = ()
    industry_flags: tuple[str, ...] = ()


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StatementType | None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    industry_flags: list[str] = Field(default_factory=list)


def classify(
    grid: Grid,
    layout: HeaderLayout,
    context: TableContext,
    index: LabelIndex,
    min_confidence: float,
) -> Classification:
    flags = list(context.industry_flags)
    if not layout.value_cols:
        return Classification(type=None, evidence=["no_period_header"], industry_flags=flags)
    if any(_NOTE_HEADING.match(text) for text in context.heading_texts):
        return Classification(type=None, evidence=["note_heading"], industry_flags=flags)

    labels = (
        [grid.text(r, layout.label_col) for r in layout.data_rows(grid)]
        if layout.label_col is not None
        else []
    )
    scores: dict[StatementType, float] = {}
    evidence: list[str] = []
    for statement in StatementType:
        hits = index.hits(labels, statement)
        score = _LABEL_WEIGHT * min(hits, _LABEL_SATURATION) / _LABEL_SATURATION + _PERIOD_WEIGHT
        if statement in context.title_types:
            score += _TITLE_WEIGHT
        elif statement in context.cue_types:
            score += _CUE_WEIGHT
        scores[statement] = round(score, 3)
        evidence.append(f"{statement.value}:hits={hits}:score={score:.2f}")

    best = max(scores, key=lambda s: scores[s])
    if scores[best] < min_confidence:
        return Classification(
            type=None,
            confidence=scores[best],
            evidence=[*evidence, "below_confidence"],
            industry_flags=flags,
        )
    return Classification(
        type=best, confidence=min(scores[best], 1.0), evidence=evidence, industry_flags=flags
    )
