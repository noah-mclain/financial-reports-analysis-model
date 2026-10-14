"""Shared schemas.

Extraction produces :class:`Statement`, analytics derives :class:`MetricValue` from it, and
the model writes a :class:`Narrative` that can only cite existing metrics.
"""

from fra_core.schemas.caveat import Caveat
from fra_core.schemas.check import CheckResult
from fra_core.schemas.document import Document, PageMode, PageProfile
from fra_core.schemas.metric import MetricInput, MetricUnit, MetricValue
from fra_core.schemas.narrative import Claim, Narrative, NarrativeSection, Sentence
from fra_core.schemas.statement import (
    BBox,
    Cell,
    Framework,
    LineItem,
    MappingFinding,
    MappingSource,
    Period,
    PeriodCandidate,
    PeriodConflict,
    PeriodEvidence,
    PeriodKind,
    PeriodObservation,
    Provenance,
    Statement,
    StatementType,
    TextSource,
)

__all__ = [
    "BBox",
    "Caveat",
    "Cell",
    "CheckResult",
    "Claim",
    "Document",
    "Framework",
    "LineItem",
    "MappingFinding",
    "MappingSource",
    "MetricInput",
    "MetricUnit",
    "MetricValue",
    "Narrative",
    "NarrativeSection",
    "PageMode",
    "PageProfile",
    "Period",
    "PeriodCandidate",
    "PeriodConflict",
    "PeriodEvidence",
    "PeriodKind",
    "PeriodObservation",
    "Provenance",
    "Sentence",
    "Statement",
    "StatementType",
    "TextSource",
]
