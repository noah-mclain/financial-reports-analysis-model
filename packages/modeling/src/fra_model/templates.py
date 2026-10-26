"""Versioned neutral English and Arabic grammar over authorized computed facts."""

from fra_analytics import format_metric
from fra_core.schemas import Claim, Footnote, Narrative, NarrativeSection, PeriodKind, Sentence
from fra_core.schemas.narrative import SectionHeading
from fra_model.payload import SUMMARY_IDS, OmissionReason, SummaryFact, SummaryPayload

TEMPLATE_VERSION = "1"
PARTIAL_TEMPLATE_VERSION = "1-partial"
SCAN_CAVEAT = (
    "Figures were read by OCR from a scanned page. Included figures passed the document's own "
    "arithmetic checks; they have not been verified against an independent source."
)
_EXCLUSIONS: dict[OmissionReason, str] = {
    "balance_held": "{label} was not included because the balance sheet could not be confirmed.",
    "missing_statement": "{label} was not included because its source statement is unavailable.",
    "missing_metric": "{label} was not included because the metric is unavailable.",
    "undefined": "{label} was not included because the metric is undefined.",
    "unconfirmed": "{label} was not included because its input figures have not all passed "
    "the document's own arithmetic checks.",
    "invalid_period": "{label} was not included because its reporting period "
    "could not be confirmed.",
    "unverified": "{label} was not included because its source figures or calculation "
    "could not be verified.",
}
# Wording keyed by SUMMARY_IDS so the authorized metric catalog has one source of truth;
# financial metadata (unit, anchor statement) stays in analytics.
_WORDING: tuple[tuple[SectionHeading, str, str], ...] = (
    (SectionHeading.LIQUIDITY, "Current ratio", "نسبة التداول"),
    (SectionHeading.LEVERAGE, "Total debt", "إجمالي الدين"),
    (
        SectionHeading.LEVERAGE,
        "Net debt to EBITDA",
        "صافي الدين إلى الربح قبل الفوائد والضرائب والإهلاك والاستهلاك",
    ),
    (SectionHeading.PROFITABILITY, "Net margin", "هامش صافي الربح"),
    (
        SectionHeading.PROFITABILITY,
        "EBITDA",
        "الربح قبل الفوائد والضرائب والإهلاك والاستهلاك",
    ),
    (SectionHeading.WATCH_ITEMS, "Days sales outstanding", "أيام تحصيل المبيعات"),
)
LABELS = dict(zip(SUMMARY_IDS, _WORDING, strict=True))


def sentence_for(payload: SummaryPayload, fact: SummaryFact) -> Sentence:
    _, en, ar = LABELS[fact.metric.metric_id]
    value = format_metric(fact.metric, payload.language)
    if value is None:
        raise ValueError("template fact must be defined")
    period = fact.period
    if payload.language == "en":
        prefix = (
            f"As at {period.end_date.isoformat()}"
            if period.kind is PeriodKind.INSTANT
            else f"Period ended {period.end_date.isoformat()} (months: {period.months})"
        )
        text = f"{prefix}; {en}: {value}."
    else:
        prefix = (
            f"في {period.end_date.isoformat()}"
            if period.kind is PeriodKind.INSTANT
            else (
                f"الفترة المنتهية في {period.end_date.isoformat()} (المدة بالأشهر: {period.months})"
            )
        )
        text = f"{prefix}؛ {ar}: {value}."
    return Sentence(
        text=text,
        claims=[Claim(metric_id=fact.metric.metric_id, period_key=period.key, text_span=value)],
        footnotes=[f.id for f in fact.footnotes],
    )


def templates(payload: SummaryPayload) -> Narrative:
    sections: dict[SectionHeading, NarrativeSection] = {}
    notes: dict[str, Footnote] = {}
    for fact in payload.facts:
        heading = LABELS[fact.metric.metric_id][0]
        section = sections.setdefault(heading, NarrativeSection(heading=heading))
        section.sentences.append(sentence_for(payload, fact))
        notes.update((f.id, f) for f in fact.footnotes)
    if payload.partial:
        section = sections.setdefault(
            SectionHeading.WATCH_ITEMS, NarrativeSection(heading=SectionHeading.WATCH_ITEMS)
        )
        for omission in payload.omissions:
            label = LABELS[omission.metric_id][1]
            section.sentences.append(
                Sentence(text=_EXCLUSIONS[omission.reason].format(label=label))
            )
        if payload.scanned:
            section.sentences.append(Sentence(text=SCAN_CAVEAT))
    return Narrative(
        language=payload.language,
        origin="direct_template",
        template_version=PARTIAL_TEMPLATE_VERSION if payload.partial else TEMPLATE_VERSION,
        sections=list(sections.values()),
        footnotes=list(notes.values()),
    )
