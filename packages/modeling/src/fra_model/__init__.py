"""Checked direct-template summaries, with explicit expected suppression."""

from dataclasses import dataclass

from fra_core.schemas import Analysis, Narrative
from fra_model.grounding import GroundingError, check_template
from fra_model.payload import SummaryPayload, SummarySuppressed, summary_payload
from fra_model.templates import templates


@dataclass(frozen=True)
class SummaryBuilt:
    narrative: Narrative


def build_summary(analysis: Analysis) -> SummaryBuilt | SummarySuppressed:
    payload = summary_payload(analysis)
    if isinstance(payload, SummarySuppressed):
        return payload
    return SummaryBuilt(check_template(payload, templates(payload)))


def validate_summary(analysis: Analysis, narrative: Narrative) -> Narrative:
    payload = summary_payload(analysis)
    if not isinstance(payload, SummaryPayload):
        raise GroundingError("; ".join(payload.reasons))
    return check_template(payload, narrative)


__all__ = [
    "GroundingError",
    "SummaryBuilt",
    "SummarySuppressed",
    "build_summary",
    "validate_summary",
]
