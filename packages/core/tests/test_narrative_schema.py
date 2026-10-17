"""Stored origin and unchecked state never imply a successful model run."""

import pytest
from pydantic import ValidationError

from fra_core.schemas.narrative import GroundingResult, Narrative


def test_legacy_is_unchecked_unknown() -> None:
    narrative = Narrative.model_validate({"model_id": "old", "sections": []})
    assert narrative.origin == "unknown"
    assert narrative.grounding == GroundingResult.UNCHECKED
    assert Narrative.model_validate_json(narrative.model_dump_json()) == narrative
    assert (
        Narrative(model_id="old", grounding=GroundingResult.FALLBACK_TEMPLATE).grounding.value
        == "fallback_template"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"template_version": "1", "model_id": "model"},
        {"template_version": "1", "adapter_id": "adapter"},
    ],
)
def test_direct_template_requires_honest_metadata(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Narrative.model_validate({"origin": "direct_template", **extra})


def test_direct_template_cannot_claim_failed_generation() -> None:
    with pytest.raises(ValidationError, match="direct_template"):
        Narrative(
            origin="direct_template",
            template_version="1",
            grounding=GroundingResult.FALLBACK_TEMPLATE,
        )


def test_direct_template_footnotes_and_claims_round_trip() -> None:
    from fra_core.schemas import Claim, Footnote, NarrativeSection, Sentence
    from fra_core.schemas.narrative import SectionHeading

    note = Footnote(
        id="scale-EGP", caveat_id="scale_assumed_units", currency="EGP", text="As printed"
    )
    narrative = Narrative(
        origin="direct_template",
        template_version="1",
        footnotes=[note],
        sections=[
            NarrativeSection(
                heading=SectionHeading.LEVERAGE,
                sentences=[
                    Sentence(
                        text="1 EGP",
                        claims=[Claim(metric_id="total_debt", period_key="p", text_span="1 EGP")],
                        footnotes=[note.id],
                    )
                ],
            )
        ],
    )
    decoded = Narrative.model_validate_json(narrative.model_dump_json())
    assert decoded == narrative and decoded.grounding is GroundingResult.UNCHECKED
