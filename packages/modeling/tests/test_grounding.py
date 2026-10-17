"""A passed marker cannot authorize changed prose, references or metadata."""

import pytest
from test_summary import analysis

from fra_core.schemas import Claim
from fra_model import SummaryBuilt, build_summary
from fra_model.grounding import GroundingError, check_template
from fra_model.payload import SummaryPayload, summary_payload


@pytest.mark.parametrize("suffix", [" increased.", " million", " 2024", " تحسن", " ١٢", " -3"])
def test_extra_text_is_rejected(suffix: str) -> None:
    source = analysis()
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    candidate = result.narrative.model_copy(deep=True)
    candidate.sections[0].sentences[0].text += suffix
    payload = summary_payload(source)
    assert isinstance(payload, SummaryPayload)
    with pytest.raises(GroundingError):
        check_template(payload, candidate)


@pytest.mark.parametrize(
    "change",
    [
        "value",
        "ref",
        "missing",
        "duplicate",
        "span",
        "date",
        "section",
        "origin",
        "version",
        "arabic_digits",
        "unit",
    ],
)
def test_corrupted_contract_is_rejected(change: str) -> None:
    source = analysis()
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    candidate = result.narrative.model_copy(deep=True)
    sentence = candidate.sections[0].sentences[0]
    claim = sentence.claims[0]
    if change == "value":
        sentence.text = sentence.text.replace("3.00×", "4.00×")
    elif change == "ref":
        sentence.claims = [claim.model_copy(update={"period_key": "2024"})]
    elif change == "missing":
        sentence.claims = []
    elif change == "duplicate":
        sentence.claims.append(claim)
    elif change == "span":
        sentence.claims = [
            Claim(metric_id=claim.metric_id, period_key=claim.period_key, text_span="3")
        ]
    elif change == "date":
        sentence.text = sentence.text.replace("2025-12-31", "2024-12-31")
    elif change == "section":
        candidate.sections.reverse()
    elif change == "origin":
        candidate.origin = "generated"
    elif change == "version":
        candidate.template_version = "unknown"
    elif change == "arabic_digits":
        sentence.text = sentence.text.replace("3", "٣")
    elif change == "unit":
        sentence.text = sentence.text.replace("×", "x")
    payload = summary_payload(source)
    assert isinstance(payload, SummaryPayload)
    with pytest.raises(GroundingError):
        check_template(payload, candidate)


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize(
    "change",
    [
        "missing_note",
        "unknown_note",
        "duplicate_note",
        "changed_note",
        "currency",
        "sign",
        "separator",
        "months",
        "overlap",
        "repeated_value",
        "null_ref",
    ],
)
def test_amount_duration_and_caveat_corruption_fails(language: str, change: str) -> None:
    from fra_analytics import Policy, compute
    from fra_core.schemas import Caveat

    source = analysis(language)
    for statement in source.statements:
        statement.caveats = [Caveat(id="scale_assumed_units")]
    source.metrics = compute(
        source.statements,
        Policy(
            negative_margin_denominator="compute_and_flag",
            include_lease_liabilities=True,
            day_count_basis=365,
        ),
    )
    built = build_summary(source)
    assert isinstance(built, SummaryBuilt)
    candidate = built.narrative.model_copy(deep=True)
    sentence = candidate.sections[1].sentences[0]  # total debt
    claim = sentence.claims[0]
    if change == "missing_note":
        sentence.footnotes = []
    elif change == "unknown_note":
        sentence.footnotes = ["invented"]
    elif change == "duplicate_note":
        sentence.footnotes *= 2
    elif change == "changed_note":
        candidate.footnotes[0] = candidate.footnotes[0].model_copy(
            update={"text": "Amounts in millions"}
        )
    elif change == "currency":
        sentence.text = sentence.text.replace("EGP", "USD")
    elif change == "sign":
        sentence.text = sentence.text.replace(claim.text_span, "−" + claim.text_span)
    elif change == "separator":
        sentence.text = sentence.text.replace(claim.text_span, claim.text_span.replace("70", "7,0"))
    elif change == "months":
        duration = candidate.sections[2].sentences[0]
        duration.text = duration.text.replace("12)", "6)")
    elif change == "overlap":
        sentence.claims.append(claim.model_copy(update={"text_span": "70"}))
    elif change == "repeated_value":
        sentence.text += " " + claim.text_span
    else:
        source.metrics = [m for m in source.metrics if m.metric_id != "total_debt"]
    payload = summary_payload(source)
    assert isinstance(payload, SummaryPayload)
    with pytest.raises(GroundingError):
        check_template(payload, candidate)


def test_checker_independently_rejects_dropped_payload_caveats() -> None:
    from dataclasses import replace

    from fra_analytics import Policy, compute
    from fra_core.schemas import Caveat
    from fra_model.templates import templates

    source = analysis()
    for statement in source.statements:
        statement.caveats = [Caveat(id="scale_assumed_units")]
    source.metrics = compute(
        source.statements,
        Policy(
            negative_margin_denominator="compute_and_flag",
            include_lease_liabilities=True,
            day_count_basis=365,
        ),
    )
    payload = summary_payload(source)
    assert isinstance(payload, SummaryPayload)
    corrupted = replace(payload, facts=tuple(replace(f, footnotes=()) for f in payload.facts))
    with pytest.raises(GroundingError):
        check_template(corrupted, templates(corrupted))


@pytest.mark.parametrize("tamper", ["caveat_id", "currency", "text"])
def test_checker_independently_rejects_a_footnote_with_the_right_id_but_tampered_fields(
    tamper: str,
) -> None:
    from dataclasses import replace

    from fra_analytics import Policy, compute
    from fra_core.schemas import Caveat
    from fra_model.templates import templates

    source = analysis()
    for statement in source.statements:
        statement.caveats = [Caveat(id="scale_assumed_units")]
    source.metrics = compute(
        source.statements,
        Policy(
            negative_margin_denominator="compute_and_flag",
            include_lease_liabilities=True,
            day_count_basis=365,
        ),
    )
    payload = summary_payload(source)
    assert isinstance(payload, SummaryPayload)
    fact = payload.facts[1]  # total_debt, has a caveat footnote
    note = fact.footnotes[0]
    if tamper == "caveat_id":
        tampered = note.model_copy(update={"caveat_id": "currency_inferred"})
    elif tamper == "currency":
        tampered = note.model_copy(update={"currency": "USD"})
    else:
        tampered = note.model_copy(update={"text": "Amounts in millions"})
    corrupted = replace(
        payload,
        facts=tuple(replace(f, footnotes=(tampered,)) if f is fact else f for f in payload.facts),
    )
    with pytest.raises(GroundingError):
        check_template(corrupted, templates(corrupted))
