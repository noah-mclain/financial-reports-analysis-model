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

from fra_core.schemas import (
    Analysis,
    BBox,
    MetricInput,
    MetricUnit,
    MetricValue,
    Narrative,
    Provenance,
    Sentence,
    Statement,
    StatementReview,
    StatementType,
)

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
    """A printed amount as statements print it: grouped, a negative in parentheses."""
    text = f"{abs(value):,}"
    return f"({text})" if value < 0 else text


@dataclass(frozen=True)
class CellView:
    text: str
    printed: str
    flags: tuple[str, ...]
    source: str | None


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


@dataclass(frozen=True)
class InputView:
    canonical_id: str
    period: str
    text: str
    source: str


@dataclass(frozen=True)
class MetricView:
    name: str
    anchor: str
    period: str
    value: str | None
    formula: str
    flags: tuple[str, ...]
    caveats: tuple[str, ...]
    inputs: tuple[InputView, ...] = field(default=())


def statement_view(statement: Statement, review: StatementReview) -> StatementView:
    """Periods newest first, whatever order the page printed them in, so the English and the
    Arabic edition of a report read the same way (in right-to-left, the newest sits beside the
    label, as Arabic statements print it)."""
    ordered = sorted(statement.periods, key=lambda p: (p.end_date, p.months or 0), reverse=True)
    keys = [p.key for p in ordered]
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
    )


def metric_text(metric: MetricValue) -> str | None:
    """The computed value rounded for display, or None when it was not computed."""
    if metric.value is None:
        return None
    value = metric.value
    if metric.unit is MetricUnit.RATIO:
        return f"{value * 100:.1f}%"
    if metric.unit is MetricUnit.TIMES:
        return f"{value:.2f}×"
    if metric.unit is MetricUnit.DAYS:
        return f"{value:.0f} days"
    currencies = sorted({i.currency for cells in metric.inputs.values() for i in cells})
    return f"{value:,.0f} {'/'.join(currencies)}".strip()


def _input_view(sha256: str, cell: MetricInput) -> InputView:
    scale = SCALE_WORDS[cell.scale]
    return InputView(
        canonical_id=cell.canonical_id,
        period=cell.period_key,
        text=f"{amount(cell.reported)} {cell.currency} {scale}".strip(),
        source=source_url(sha256, cell.provenance),
    )


def metric_anchor(metric_id: str, period_key: str) -> str:
    """The id of a metric's row, which a summary's claim links to."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", f"metric-{metric_id}-{period_key}")


@dataclass(frozen=True)
class SentencePart:
    text: str
    href: str | None


@dataclass(frozen=True)
class SectionView:
    heading: str
    sentences: tuple[tuple[SentencePart, ...], ...]


def sentence_parts(sentence: Sentence, anchors: frozenset[str]) -> tuple[SentencePart, ...]:
    """The sentence with each claimed span a link to the metric it states. A span missing
    from the sentence, overlapping another, or naming a metric the analysis did not compute
    is an error: a figure in the summary is shown only with its trace."""
    placed = []
    taken: dict[str, int] = {}  # the same text claimed twice is its next occurrence
    for claim in sentence.claims:
        at = sentence.text.find(claim.text_span, taken.get(claim.text_span, 0))
        anchor = metric_anchor(claim.metric_id, claim.period_key)
        if at < 0:
            raise ValueError(f"claim {claim.text_span!r} is not in {sentence.text!r}")
        if anchor not in anchors:
            raise ValueError(f"claim {claim.text_span!r} names no computed metric ({anchor})")
        placed.append((at, at + len(claim.text_span), anchor))
        taken[claim.text_span] = at + len(claim.text_span)
    parts: list[SentencePart] = []
    cursor = 0
    for start, end, anchor in sorted(placed):
        if start < cursor:
            raise ValueError(f"claims overlap in {sentence.text!r}")
        if start > cursor:
            parts.append(SentencePart(sentence.text[cursor:start], None))
        parts.append(SentencePart(sentence.text[start:end], f"#{anchor}"))
        cursor = end
    if cursor < len(sentence.text):
        parts.append(SentencePart(sentence.text[cursor:], None))
    return tuple(parts)


def narrative_view(narrative: Narrative, anchors: frozenset[str]) -> tuple[SectionView, ...]:
    return tuple(
        SectionView(
            heading=section.heading.value.replace("_", " "),
            sentences=tuple(sentence_parts(s, anchors) for s in section.sentences),
        )
        for section in narrative.sections
    )


def metric_view(sha256: str, metric: MetricValue) -> MetricView:
    return MetricView(
        name=metric.metric_id.replace("_", " "),
        anchor=metric_anchor(metric.metric_id, metric.period_key),
        period=metric.period_key,
        value=metric_text(metric),
        formula=metric.formula,
        flags=tuple(metric.flags),
        caveats=tuple(metric.caveats),
        inputs=tuple(
            _input_view(sha256, cell) for cells in metric.inputs.values() for cell in cells
        ),
    )


@dataclass(frozen=True)
class AnalysisView:
    statements: tuple[StatementView, ...]
    metrics: tuple[MetricView, ...]
    computed: int
    narrative: tuple[SectionView, ...] | None


def analysis_view(analysis: Analysis) -> AnalysisView:
    """Statements in the order found, each with its review (an error when one is missing);
    metrics computed first, then the ones that could not be, each group in registry order."""
    reviews = {r.statement_id: r for r in analysis.reviews}
    missing = [s.id for s in analysis.statements if s.id not in reviews]
    if missing:
        raise ValueError(f"analysis {analysis.job_id}: statements without a review: {missing}")
    metrics = [metric_view(analysis.upload_sha256, m) for m in analysis.metrics]
    return AnalysisView(
        statements=tuple(statement_view(s, reviews[s.id]) for s in analysis.statements),
        metrics=tuple(
            [m for m in metrics if m.value is not None] + [m for m in metrics if m.value is None]
        ),
        computed=sum(1 for m in metrics if m.value is not None),
        narrative=(
            narrative_view(
                analysis.narrative,
                frozenset(m.anchor for m in metrics if m.value is not None),
            )
            if analysis.narrative is not None
            else None
        ),
    )
