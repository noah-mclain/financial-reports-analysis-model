"""What the results page shows, built from an analysis without computing anything new.

Every figure on the page is a printed cell or a computed metric. A cell is shown as parsed,
with its printed text beside it, and links to the page region it was read from; a metric
shows its formula and links to each input cell. Formatting here only rounds for display: a
ratio as a percentage, times and days to the precision a reader uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import urlencode

from fra_analytics import format_amount, format_metric
from fra_core.citations import place_claims
from fra_core.presentation import caveat_text
from fra_core.schemas import (
    Analysis,
    BBox,
    JobStatus,
    MetricInput,
    MetricValue,
    Narrative,
    Provenance,
    Sentence,
    Statement,
    StatementReview,
    StatementType,
)
from fra_core.schemas.caveat import CaveatId
from fra_model import GroundingError, validate_summary

STATEMENT_TITLES = {
    StatementType.BALANCE: "Statement of financial position",
    StatementType.INCOME: "Profit or loss",
    StatementType.COMPREHENSIVE_INCOME: "Comprehensive income",
    StatementType.CASH_FLOW: "Cash flows",
    StatementType.EQUITY: "Changes in equity",
}
SCALE_WORDS = {1: "", 1_000: "thousands", 1_000_000: "millions", 1_000_000_000: "billions"}
RTL_LANGUAGES = frozenset({"ar"})
DUPLICATE_CELL = "duplicate_cell"


def source_url(sha256: str, provenance: Provenance) -> str:
    box: BBox = provenance.bbox
    query = urlencode({"box": f"{box.left:.1f},{box.top:.1f},{box.right:.1f},{box.bottom:.1f}"})
    return f"/documents/{sha256}/pages/{provenance.page_no}?{query}"


def amount(value: Decimal) -> str:
    return format_amount(value)


@dataclass(frozen=True)
class CaveatView:
    id: CaveatId
    currency: str
    evidence: dict[str, str]
    anchor: str
    language: str

    @property
    def text(self) -> str:
        if self.language not in ("en", "ar"):
            raise ValueError(f"unsupported caveat language: {self.language}")
        return caveat_text(self.id, self.currency, "ar" if self.language == "ar" else "en")


@dataclass(frozen=True)
class CellView:
    text: str
    printed: str
    flags: tuple[str, ...]
    source: str | None
    caveats: tuple[CaveatView, ...] = ()


@dataclass(frozen=True)
class RowView:
    label: str
    depth: int
    subtotal: bool
    mapped_to: str | None
    mapping_flag: str | None
    cells: tuple[tuple[CellView, ...], ...]  # per period, newest first; () when none was read


@dataclass(frozen=True)
class StatementView:
    id: str
    title: str
    units: str
    rtl: bool
    language: str
    pages: tuple[int, ...]
    periods: tuple[str, ...]
    rows: tuple[RowView, ...]
    status: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    duplicate: bool
    caveats: tuple[CaveatView, ...]


@dataclass(frozen=True)
class InputView:
    canonical_id: str
    period: str
    text: str
    source: str
    caveats: tuple[CaveatView, ...] = ()


@dataclass(frozen=True)
class MetricView:
    name: str
    anchor: str
    period: str
    value: str | None
    formula: str
    flags: tuple[str, ...]
    caveats: tuple[CaveatId, ...]
    inputs: tuple[InputView, ...] = field(default=())
    notes: tuple[CaveatView, ...] = ()


def statement_view(statement: Statement, review: StatementReview) -> StatementView:
    """Periods newest first, whatever order the page printed them in, so the English and the
    Arabic edition of a report read the same way (in right-to-left, the newest sits beside the
    label, as Arabic statements print it)."""
    ordered = sorted(statement.periods, key=lambda p: (p.end_date, p.months or 0), reverse=True)
    keys = [p.key for p in ordered]
    caveats = tuple(
        CaveatView(
            id=c.id,
            currency=statement.currency,
            evidence=c.evidence,
            anchor=f"statement-{statement.id}-{c.id}",
            language=statement.language,
        )
        for c in statement.caveats
    )
    rows = []
    for item in statement.line_items:
        by_period: dict[str, list[CellView]] = {}
        for cell in item.cells:
            by_period.setdefault(cell.period_key, []).append(
                CellView(
                    text="" if cell.reported is None else amount(cell.reported),
                    printed=cell.raw_text,
                    flags=tuple(cell.flags),
                    source=source_url(statement.document_sha256, cell.provenance),
                    caveats=caveats
                    if cell.reported is not None and "percent" not in cell.flags
                    else (),
                )
            )
        # Two cells read for one period are both shown and flagged, never one silently kept.
        cells = tuple(
            tuple(
                replace(c, flags=(*c.flags, DUPLICATE_CELL)) if len(found) > 1 else c for c in found
            )
            for found in (by_period.get(key, []) for key in keys)
        )
        rows.append(
            RowView(
                label=item.raw_label,
                depth=item.depth,
                subtotal=item.is_subtotal,
                mapped_to=item.canonical_id,
                mapping_flag=item.mapping_flag,
                cells=cells,
            )
        )
    scale = SCALE_WORDS[statement.scale]
    return StatementView(
        id=statement.id,
        title=STATEMENT_TITLES[statement.type],
        units=f"{statement.currency} {scale}".strip(),
        rtl=statement.language in RTL_LANGUAGES,
        language=statement.language,
        pages=tuple(statement.source_pages),
        periods=tuple(keys),
        rows=tuple(rows),
        status=review.status,
        reasons=tuple(review.reasons),
        warnings=tuple(review.warnings),
        duplicate="duplicate_statement" in review.reasons,
        caveats=caveats,
    )


def metric_text(metric: MetricValue) -> str | None:
    return format_metric(metric)


def _input_view(sha256: str, cell: MetricInput, caveats: tuple[CaveatView, ...]) -> InputView:
    scale = SCALE_WORDS[cell.scale]
    return InputView(
        canonical_id=cell.canonical_id,
        period=cell.period_key,
        text=f"{amount(cell.reported)} {cell.currency} {scale}".strip(),
        source=source_url(sha256, cell.provenance),
        caveats=caveats,
    )


def metric_anchor(metric_id: str, period_key: str) -> str:
    """The id of a metric's row, which a summary's claim links to."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"metric-{metric_id}-{period_key}")


@dataclass(frozen=True)
class SentencePart:
    text: str
    href: str | None
    caveats: tuple[CaveatView, ...] = ()


@dataclass(frozen=True)
class SectionView:
    heading: str
    sentences: tuple[tuple[SentencePart, ...], ...]


def sentence_parts(sentence: Sentence, anchors: frozenset[str]) -> tuple[SentencePart, ...]:
    """The sentence with each claimed span a link to the metric it states. Each claim takes
    the first occurrence of its span that no earlier claim took, so the same figure claimed
    twice links each occurrence in turn. A span with no free occurrence, or appearing more
    times in the text than claims name it (so which occurrence is meant is ambiguous), or
    naming a metric the analysis did not compute, is an error: a figure in the summary is
    shown only with its trace."""
    placed = place_claims(sentence)
    for item in placed:
        anchor = metric_anchor(item.claim.metric_id, item.claim.period_key)
        if anchor not in anchors:
            raise ValueError(f"claim {item.claim.text_span!r} names no computed metric ({anchor})")
    parts: list[SentencePart] = []
    cursor = 0
    for item in placed:
        start, end = item.start, item.end
        anchor = metric_anchor(item.claim.metric_id, item.claim.period_key)
        if start > cursor:
            parts.append(SentencePart(sentence.text[cursor:start], None))
        parts.append(SentencePart(sentence.text[start:end], f"#{anchor}"))
        cursor = end
    if cursor < len(sentence.text):
        parts.append(SentencePart(sentence.text[cursor:], None))
    return tuple(parts)


def narrative_view(
    narrative: Narrative,
    anchors: frozenset[str],
    notes: dict[str, tuple[CaveatView, ...]] | None = None,
) -> tuple[SectionView, ...]:
    notes = notes or {}

    def parts(sentence: Sentence) -> tuple[SentencePart, ...]:
        linked = sentence_parts(sentence, anchors)
        footnotes = {
            note.anchor: note
            for claim in sentence.claims
            for note in notes.get(metric_anchor(claim.metric_id, claim.period_key), ())
        }
        return (*linked, SentencePart("", None, tuple(footnotes.values()))) if footnotes else linked

    return tuple(
        SectionView(
            heading=(
                {
                    "overview": "نظرة عامة",
                    "liquidity": "السيولة",
                    "leverage": "المديونية",
                    "profitability": "الربحية",
                    "watch_items": "بنود للمتابعة",
                }[section.heading.value]
                if narrative.language == "ar"
                else section.heading.value.replace("_", " ")
            ),
            sentences=tuple(parts(s) for s in section.sentences),
        )
        for section in narrative.sections
    )


def metric_view(
    sha256: str, metric: MetricValue, statement_notes: dict[str, tuple[CaveatView, ...]]
) -> MetricView:
    inputs = tuple(
        _input_view(sha256, cell, statement_notes.get(cell.statement_id, ()))
        for cells in metric.inputs.values()
        for cell in cells
    )
    notes = {
        note.anchor: note for cell in inputs for note in cell.caveats if note.id in metric.caveats
    }
    missing = set(metric.caveats) - {note.id for note in notes.values()}
    if missing:
        raise ValueError(
            f"metric {metric.metric_id}: caveats without source statements: {sorted(missing)}"
        )
    return MetricView(
        name=metric.metric_id.replace("_", " "),
        anchor=metric_anchor(metric.metric_id, metric.period_key),
        period=metric.period_key,
        value=metric_text(metric),
        formula=metric.formula,
        flags=tuple(metric.flags),
        caveats=tuple(metric.caveats),
        inputs=inputs,
        notes=tuple(notes.values()),
    )


@dataclass(frozen=True)
class AnalysisView:
    statements: tuple[StatementView, ...]
    metrics: tuple[MetricView, ...]
    computed: int
    narrative: tuple[SectionView, ...] | None
    footnotes: tuple[CaveatView, ...]
    summary_notice: str | None
    origin_text: str


def analysis_view(analysis: Analysis) -> AnalysisView:
    """Statements in the order found, each with its review (an error when one is missing);
    metrics computed first, then the ones that could not be, each group in registry order."""
    reviews = {r.statement_id: r for r in analysis.reviews}
    missing = [s.id for s in analysis.statements if s.id not in reviews]
    if missing:
        raise ValueError(f"analysis {analysis.job_id}: statements without a review: {missing}")
    statements = tuple(statement_view(s, reviews[s.id]) for s in analysis.statements)
    statement_notes = {s.id: s.caveats for s in statements}
    metrics = [metric_view(analysis.upload_sha256, m, statement_notes) for m in analysis.metrics]
    artifact = analysis.narrative
    ar = analysis.language == "ar"
    summary_notice = None
    origin_text = ""
    if artifact is not None and artifact.language not in ("en", "ar"):
        artifact = None
        summary_notice = (
            "حُجب الملخص: تعذر التحقق من النص أو مراجعه."
            if ar
            else "Summary withheld: its text or references could not be verified."
        )
    elif artifact is not None and artifact.origin == "direct_template":
        try:
            artifact = validate_summary(analysis, artifact)
            origin_text = (
                "قالب مباشر تم التحقق منه مقابل المقاييس المحسوبة."
                if ar
                else "Direct template checked against the computed metrics."
            )
        except GroundingError:
            artifact = None
            summary_notice = (
                "حُجب الملخص: تعذر التحقق من النص أو مراجعه."
                if ar
                else "Summary withheld: its text or references could not be verified."
            )
    elif artifact is not None:
        origin_text = (
            "ملخص محفوظ؛ لم يتم التحقق منه كقالب مباشر."
            if ar
            else "Stored summary; not verified as a direct template."
        )
    elif analysis.narrative_suppression:
        held = analysis.status() is not JobStatus.COMPLETED
        if held:
            summary_notice = (
                "حُجب الملخص: التحليل قيد المراجعة."
                if ar
                else "Summary withheld: the analysis is held for review."
            )
        else:
            summary_notice = (
                "حُجب الملخص: لا تتوفر حقائق مؤهلة من أحدث فترة."
                if ar
                else "Summary withheld: no eligible latest-period facts are available."
            )
    summary_notes = (
        {
            m.anchor: tuple(
                replace(
                    note, anchor=f"summary-{note.id}-{note.currency}", language=artifact.language
                )
                for note in m.notes
            )
            for m in metrics
            if m.value is not None
        }
        if artifact is not None
        else {}
    )
    narrative = None
    if artifact is not None:
        try:
            narrative = narrative_view(
                artifact, frozenset(m.anchor for m in metrics if m.value is not None), summary_notes
            )
        except ValueError:
            summary_notice = (
                "حُجب الملخص: تعذر التحقق من النص أو مراجعه."
                if ar
                else "Summary withheld: its text or references could not be verified."
            )
    footnotes = {
        note.anchor: note
        for section in narrative or ()
        for parts in section.sentences
        for part in parts
        for note in part.caveats
    }
    return AnalysisView(
        statements=statements,
        metrics=tuple(
            [m for m in metrics if m.value is not None] + [m for m in metrics if m.value is None]
        ),
        computed=sum(1 for m in metrics if m.value is not None),
        narrative=narrative,
        footnotes=tuple(footnotes.values()),
        summary_notice=summary_notice,
        origin_text=origin_text,
    )
