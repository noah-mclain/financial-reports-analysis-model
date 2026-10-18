"""Conservative eligibility and traced facts for the direct-template summary."""

import math
from dataclasses import dataclass

from fra_analytics import metric_descriptor, unit_notes
from fra_core.presentation import Language, caveat_text
from fra_core.schemas import (
    PROVENANCE_WARNINGS,
    Analysis,
    Footnote,
    JobStatus,
    MetricValue,
    Period,
    PeriodKind,
    Statement,
    StatementType,
    is_provenance_warning,
)
from fra_core.schemas.caveat import CaveatId


@dataclass(frozen=True)
class SummaryFact:
    metric: MetricValue
    period: Period
    footnotes: tuple[Footnote, ...]


@dataclass(frozen=True)
class SummaryPayload:
    language: Language
    facts: tuple[SummaryFact, ...]


@dataclass(frozen=True)
class SummarySuppressed:
    reasons: tuple[str, ...]


# Ordering is part of the authorized narrative grammar, not a formula catalog.
SUMMARY_IDS = ("current_ratio", "total_debt", "net_debt_to_ebitda", "net_margin", "ebitda", "dso")
_ASSUMPTIONS: dict[str, CaveatId] = {
    "scale_missing": "scale_assumed_units",
    "currency_from_domicile": "currency_from_domicile",
    "currency_inferred": "currency_inferred",
}


def _unique(values: list[object], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {name}")


def _row_provenance(flag: str, statement: Statement) -> bool:
    """A provenance flag that names a row of this statement: ``label_words_from_page:<row id>``."""
    name, colon, row = flag.partition(":")
    return (
        bool(colon)
        and name in PROVENANCE_WARNINGS
        and any(item.id == row for item in statement.line_items)
    )


def _source_contract(analysis: Analysis) -> None:
    _unique([s.id for s in analysis.statements], "statement ids")
    _unique([s.type for s in analysis.statements], "statement types")
    _unique([r.statement_id for r in analysis.reviews], "reviews")
    if {s.id for s in analysis.statements} != {r.statement_id for r in analysis.reviews}:
        raise ValueError("missing or orphan statement review")
    if analysis.status() is not JobStatus.COMPLETED:
        raise ValueError("analysis needs review")
    for review in analysis.reviews:
        if (
            review.reasons
            or review.flagged_cells
            or review.checked_cells != review.numeric_cells
            or any(w not in _ASSUMPTIONS and not is_provenance_warning(w) for w in review.warnings)
        ):
            raise ValueError(f"contradictory or unsupported review: {review.statement_id}")
    for statement in analysis.statements:
        if statement.language not in ("en", "ar"):
            raise ValueError(f"unsupported statement language: {statement.id}")
        if statement.document_sha256 != analysis.upload_sha256:
            raise ValueError(f"statement document mismatch: {statement.id}")
        if statement.period_conflicts or statement.mapping_findings:
            raise ValueError(f"unresolved statement findings: {statement.id}")
        _unique([c.id for c in statement.caveats], "source caveats")
        caveats = {c.id for c in statement.caveats}
        if any(
            not _row_provenance(flag, statement)
            and (flag not in _ASSUMPTIONS or _ASSUMPTIONS[flag] not in caveats)
            for flag in statement.flags
        ):
            raise ValueError(f"unsupported statement notices: {statement.id}")
        if "scale_assumed_units" in caveats and statement.scale != 1:
            raise ValueError(f"contradictory scale assumption: {statement.id}")
        if {"currency_from_domicile", "currency_inferred"} <= caveats:
            raise ValueError(f"contradictory currency assumptions: {statement.id}")
        _unique([p.key for p in statement.periods], "period keys")
        _unique([(p.end_date, p.kind, p.months) for p in statement.periods], "period identities")
        for period in statement.periods:
            Period.model_validate(period.model_dump())
        _unique([r.id for r in statement.line_items], "row ids")
        for row in statement.line_items:
            _unique([c.period_key for c in row.cells], "row cells")
            for cell in row.cells:
                if cell.period_key not in {p.key for p in statement.periods}:
                    raise ValueError(f"unknown cell period: {row.id}")
        if any(page < 1 or page > analysis.page_count for page in statement.source_pages):
            raise ValueError(f"source page outside document: {statement.id}")
    _unique([(m.metric_id, m.period_key) for m in analysis.metrics], "metric-period pairs")
    if any(metric_descriptor(m.metric_id) is None for m in analysis.metrics):
        raise ValueError("unknown metric id")


def _fact(
    analysis: Analysis, metric: MetricValue, period: Period, language: Language
) -> SummaryFact:
    descriptor = metric_descriptor(metric.metric_id)
    if descriptor is None or metric.unit != descriptor.unit:
        raise ValueError("metric unit differs from its descriptor")
    if metric.value is None or not math.isfinite(metric.value):
        raise ValueError("undefined metric")
    sources: dict[str, Statement] = {}
    references = [cell for cells in metric.inputs.values() for cell in cells]
    if not references:
        raise ValueError("metric has no source inputs")
    for ref in references:
        statement = next((s for s in analysis.statements if s.id == ref.statement_id), None)
        if statement is None:
            raise ValueError("metric source statement missing")
        source_period = next((p for p in statement.periods if p.key == ref.period_key), None)
        if source_period is None or source_period.end_date > period.end_date:
            raise ValueError("metric source period missing or in the future")
        if statement.type == descriptor.anchor:
            if (source_period.end_date, source_period.kind, source_period.months) != (
                period.end_date,
                period.kind,
                period.months,
            ):
                raise ValueError("metric input differs from its anchor period")
        elif (
            not (metric.metric_id == "dso" and statement.type is StatementType.BALANCE)
            and source_period.end_date != period.end_date
        ):
            raise ValueError("cross-statement closing date mismatch")
        row = next((r for r in statement.line_items if r.id == ref.line_item_id), None)
        if row is None or row.canonical_id != ref.canonical_id or row.mapping_flag is not None:
            raise ValueError("metric source row mismatch")
        cell = next((c for c in row.cells if c.period_key == ref.period_key), None)
        if (
            cell is None
            or cell.reported is None
            or not cell.reported.is_finite()
            or cell.reported != ref.reported
            or cell.provenance != ref.provenance
            or not all(is_provenance_warning(f) for f in cell.flags)
            or statement.currency != ref.currency
            or statement.scale != ref.scale
        ):
            raise ValueError("metric source cell mismatch or flagged")
        box = ref.provenance.bbox
        if (
            ref.provenance.page_no not in statement.source_pages
            or not 1 <= ref.provenance.page_no <= analysis.page_count
            or not all(
                math.isfinite(v) and v >= 0 for v in (box.left, box.top, box.right, box.bottom)
            )
            or box.right <= box.left
            or box.bottom <= box.top
        ):
            raise ValueError("metric provenance outside document")
        sources[statement.id] = statement
    if not any(s.type == descriptor.anchor for s in sources.values()):
        raise ValueError("metric has no input from its anchor statement")
    if len({s.currency for s in sources.values()}) != 1:
        raise ValueError("metric currency mismatch")
    notes = unit_notes(metric.unit, list(sources.values()))
    if notes.blocked or notes.flags or sorted(metric.caveats) != list(notes.caveats):
        raise ValueError("metric unit caveats do not match sources")
    flags = [
        f
        for f in metric.flags
        if not is_provenance_warning(f) and not any(_row_provenance(f, s) for s in sources.values())
    ]
    for flag in flags:
        caveat = _ASSUMPTIONS.get(flag)
        if caveat is None or not any(c.id == caveat for s in sources.values() for c in s.caveats):
            raise ValueError("metric has unsupported flags")
    currency = next(iter(sources.values())).currency
    footnotes = tuple(
        Footnote(
            id=f"{c}-{currency}",
            caveat_id=c,
            currency=currency,
            text=caveat_text(c, currency, language),
        )
        for c in notes.caveats
    )
    return SummaryFact(metric.model_copy(update={"flags": flags}, deep=True), period, footnotes)


def summary_payload(analysis: Analysis) -> SummaryPayload | SummarySuppressed:
    """Select latest anchor periods first; never replace a bad latest with an older fact."""
    if analysis.language not in ("en", "ar"):
        return SummarySuppressed((f"unsupported summary language: {analysis.language}",))
    language: Language = "ar" if analysis.language == "ar" else "en"
    try:
        _source_contract(analysis)
    except ValueError as error:
        return SummarySuppressed((str(error),))
    facts: list[SummaryFact] = []
    omitted: list[str] = []
    for metric_id in SUMMARY_IDS:
        descriptor = metric_descriptor(metric_id)
        assert descriptor is not None
        anchor = next((s for s in analysis.statements if s.type == descriptor.anchor), None)
        if anchor is None:
            continue
        latest_date = max(p.end_date for p in anchor.periods)
        latest = [p for p in anchor.periods if p.end_date == latest_date]
        if len(latest) != 1:
            omitted.append(f"{metric_id}: ambiguous latest period")
            continue
        period = latest[0]
        if (anchor.type.value == "balance" and period.kind is not PeriodKind.INSTANT) or (
            anchor.type.value != "balance" and period.kind is not PeriodKind.DURATION
        ):
            omitted.append(f"{metric_id}: unsupported period kind")
            continue
        metric = next(
            (
                m
                for m in analysis.metrics
                if m.metric_id == metric_id and m.period_key == period.key
            ),
            None,
        )
        if metric is None:
            continue
        try:
            facts.append(_fact(analysis, metric, period, language))
        except ValueError as error:
            omitted.append(f"{metric_id}: {error}")
    return (
        SummaryPayload(language, tuple(facts))
        if facts
        else SummarySuppressed(tuple(omitted) or ("no eligible latest metric facts",))
    )
