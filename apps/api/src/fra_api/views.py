"""What the results page shows, built from an analysis without computing anything new.

Every figure on the page is a printed cell or a computed metric. A cell is shown as parsed,
with its printed text beside it, and links to the page region it was read from; a metric
shows its formula and links to each input cell. Formatting here only rounds for display: a
ratio as a percentage, times and days to the precision a reader uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlencode

from fra_core.schemas import (
    Analysis,
    BBox,
    MetricInput,
    MetricUnit,
    MetricValue,
    Provenance,
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
    cells: tuple[CellView | None, ...]


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
        by_period = {c.period_key: c for c in item.cells}
        cells: list[CellView | None] = []
        for key in keys:
            cell = by_period.get(key)
            if cell is None:
                cells.append(None)
                continue
            cells.append(
                CellView(
                    text="" if cell.reported is None else amount(cell.reported),
                    printed=cell.raw_text,
                    flags=tuple(cell.flags),
                    source=source_url(statement.document_sha256, cell.provenance),
                )
            )
        rows.append(
            RowView(
                label=item.raw_label,
                depth=item.depth,
                subtotal=item.is_subtotal,
                mapped_to=item.canonical_id,
                mapping_flag=item.mapping_flag,
                cells=tuple(cells),
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


def metric_view(sha256: str, metric: MetricValue) -> MetricView:
    return MetricView(
        name=metric.metric_id.replace("_", " "),
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
    )
