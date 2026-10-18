"""Ambiguous identities and unsupported source states never become narrative facts."""

from collections.abc import Callable
from decimal import Decimal

import pytest
from test_summary import analysis

from fra_core.schemas import Analysis, Caveat
from fra_model import SummaryBuilt, SummarySuppressed, build_summary
from fra_model.payload import summary_payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: setattr(a, "language", "fr"),
        lambda a: setattr(a, "hold", "review"),
        lambda a: a.flags.append("unknown"),
        lambda a: a.statements.append(a.statements[0]),
        lambda a: a.reviews.append(a.reviews[0]),
        lambda a: a.reviews.pop(),
        lambda a: a.reviews.append(a.reviews[0].model_copy(update={"statement_id": "orphan"})),
        lambda a: a.reviews.__setitem__(
            0, a.reviews[0].model_copy(update={"reasons": ["identity_failed"]})
        ),
        lambda a: a.statements[0].flags.append("unknown"),
        lambda a: a.statements[0].line_items.append(a.statements[0].line_items[0]),
        lambda a: (
            a.statements[0].line_items[0].cells.append(a.statements[0].line_items[0].cells[0])
        ),
        lambda a: setattr(a.statements[0], "document_sha256", "c" * 64),
        lambda a: a.metrics.append(a.metrics[0]),
        lambda a: setattr(a.metrics[0], "metric_id", "invented"),
    ],
)
def test_ineligible_analysis(mutate: Callable[[Analysis], object]) -> None:
    source = analysis()
    mutate(source)
    assert isinstance(build_summary(source), SummarySuppressed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reported", Decimal("999")),
        ("currency", "USD"),
        ("scale", 1000),
        ("canonical_id", "fake"),
        ("line_item_id", "fake"),
        ("statement_id", "fake"),
        ("period_key", "fake"),
    ],
)
def test_fabricated_metric_inputs_are_omitted(field: str, value: object) -> None:
    source = analysis()
    metric = next(
        m for m in source.metrics if m.metric_id == "current_ratio" and m.period_key == "2025"
    )
    role = next(iter(metric.inputs))
    metric.inputs[role][0] = metric.inputs[role][0].model_copy(update={field: value})
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert "current_ratio" not in result.narrative.cited_metric_ids


def test_caveats_follow_amounts_and_cross_statement_ratios_only() -> None:
    source = analysis("ar")
    for statement in source.statements:
        statement.caveats = [Caveat(id="scale_assumed_units"), Caveat(id="currency_inferred")]
    from fra_analytics import Policy, compute

    source.metrics = compute(
        source.statements,
        Policy(
            negative_margin_denominator="compute_and_flag",
            include_lease_liabilities=True,
            day_count_basis=365,
        ),
    )
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert len(result.narrative.footnotes) == 2
    for section in result.narrative.sections:
        for sentence in section.sentences:
            metric_id = sentence.claims[0].metric_id
            assert len(sentence.footnotes) == (
                0 if metric_id in ("current_ratio", "net_margin") else 2
            )
    assert all("EGP" in f.text for f in result.narrative.footnotes)


def test_missing_metric_caveats_omit_affected_facts() -> None:
    source = analysis()
    source.statements[0].caveats = [Caveat(id="scale_assumed_units")]
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert "total_debt" not in result.narrative.cited_metric_ids
    assert "current_ratio" in result.narrative.cited_metric_ids


@pytest.mark.parametrize("change", ["null", "flags", "unit", "provenance", "empty_inputs"])
def test_bad_latest_metric_is_omitted_without_older_substitution(change: str) -> None:
    from fra_core.schemas import MetricUnit

    source = analysis()
    metric = next(
        m for m in source.metrics if m.metric_id == "current_ratio" and m.period_key == "2025"
    )
    if change == "null":
        metric.value = None
        metric.flags = ["undefined"]
    elif change == "flags":
        metric.flags = ["invented_warning"]
    elif change == "unit":
        metric.unit = MetricUnit.RATIO
    elif change == "provenance":
        role = next(iter(metric.inputs))
        ref = metric.inputs[role][0]
        metric.inputs[role][0] = ref.model_copy(
            update={"provenance": ref.provenance.model_copy(update={"page_no": 2})}
        )
    else:
        metric.inputs = {}
    result = build_summary(source)
    assert (
        isinstance(result, SummaryBuilt)
        and "current_ratio" not in result.narrative.cited_metric_ids
    )


def test_assumption_flags_are_eligible_only_with_the_source_caveat() -> None:
    from fra_analytics import Policy, compute

    source = analysis()
    for statement in source.statements:
        statement.flags = ["scale_missing"]
        statement.caveats = [Caveat(id="scale_assumed_units")]
    source.metrics = compute(
        source.statements,
        Policy(
            negative_margin_denominator="compute_and_flag",
            include_lease_liabilities=True,
            day_count_basis=365,
        ),
    )
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt) and "net_margin" in result.narrative.cited_metric_ids
    source.statements[0].caveats = []
    assert isinstance(build_summary(source), SummarySuppressed)


def test_decline_review_conflicts_and_missing_duration_are_suppressed() -> None:
    from fra_core.schemas import Decline

    source = analysis()
    source.decline = Decline(code="bank", reason="out of scope")
    assert isinstance(build_summary(source), SummarySuppressed)
    source = analysis()
    source.reviews.append(source.reviews[0].model_copy(update={"status": "needs_review"}))
    assert isinstance(build_summary(source), SummarySuppressed)
    source = analysis()
    source.statements[1].periods[1] = (
        source.statements[1].periods[1].model_copy(update={"months": None})
    )
    assert isinstance(build_summary(source), SummarySuppressed)


def test_periods_are_resolved_on_anchor_and_interim_months_are_explicit() -> None:
    source = analysis()
    income = source.statements[1]
    # The same key on different statement types is not a global year/date lookup.
    income.periods[1] = income.periods[1].model_copy(
        update={"end_date": income.periods[1].end_date.replace(month=6, day=30), "months": 6}
    )
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    sentences = [s for section in result.narrative.sections for s in section.sentences]
    margin = next(s for s in sentences if s.claims[0].metric_id == "net_margin")
    current = next(s for s in sentences if s.claims[0].metric_id == "current_ratio")
    assert "2025-06-30 (months: 6)" in margin.text
    assert "2025-12-31" in current.text


def test_an_original_cell_from_the_wrong_period_is_not_an_authorized_current_fact() -> None:
    source = analysis()
    metric = next(
        m for m in source.metrics if m.metric_id == "current_ratio" and m.period_key == "2025"
    )
    role = next(iter(metric.inputs))
    ref = metric.inputs[role][0]
    row = next(r for r in source.statements[0].line_items if r.id == ref.line_item_id)
    old = row.cells[0]
    metric.inputs[role][0] = ref.model_copy(
        update={
            "period_key": old.period_key,
            "reported": old.reported,
            "provenance": old.provenance,
        }
    )
    result = build_summary(source)
    assert (
        isinstance(result, SummaryBuilt)
        and "current_ratio" not in result.narrative.cited_metric_ids
    )


def test_provenance_warnings_do_not_block_or_reach_the_narrative() -> None:
    baseline = build_summary(analysis())
    assert isinstance(baseline, SummaryBuilt)
    source = analysis()
    source.reviews = [
        r.model_copy(update={"warnings": ["label_merged:6", "label_words_from_page:70"]})
        for r in source.reviews
    ]
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert result.narrative == baseline.narrative
    assert "label_" not in result.narrative.model_dump_json()


@pytest.mark.parametrize(
    "warning", ["label_merged_x:1", "label_merged:x", "label_merged:", "other"]
)
def test_an_unknown_review_warning_still_suppresses_the_summary(warning: str) -> None:
    source = analysis()
    source.reviews[0] = source.reviews[0].model_copy(update={"warnings": [warning]})
    assert isinstance(build_summary(source), SummarySuppressed)


def _recomputed(source: Analysis) -> Analysis:
    from fra_analytics import Policy, compute

    source.metrics = compute(
        source.statements,
        Policy(
            negative_margin_denominator="compute_and_flag",
            include_lease_liabilities=True,
            day_count_basis=365,
        ),
    )
    return source


def test_provenance_flags_on_source_cells_do_not_block_or_reach_the_narrative() -> None:
    baseline = build_summary(_recomputed(analysis()))
    assert isinstance(baseline, SummaryBuilt)
    source = analysis()
    for statement in source.statements:
        for row in statement.line_items:
            for cell in row.cells:
                cell.flags = ["label_words_from_page", "label_merged"]
    result = build_summary(_recomputed(source))
    assert isinstance(result, SummaryBuilt)
    assert result.narrative == baseline.narrative
    assert "label_" not in result.narrative.model_dump_json()
    payload = summary_payload(_recomputed(source))
    assert not isinstance(payload, SummarySuppressed)
    assert all("label_" not in flag for fact in payload.facts for flag in fact.metric.flags)


def test_a_non_provenance_cell_flag_still_blocks_its_facts() -> None:
    source = analysis()
    for row in source.statements[0].line_items:
        for cell in row.cells:
            cell.flags = ["digit_suspect"]
    result = build_summary(_recomputed(source))
    assert isinstance(result, SummarySuppressed) or (
        "current_ratio" not in result.narrative.cited_metric_ids
    )


def test_a_provenance_flag_naming_a_row_of_its_own_statement_does_not_block() -> None:
    source = analysis()
    statement = source.statements[0]
    statement.flags = [f"label_words_from_page:{statement.line_items[0].id}"]
    result = build_summary(_recomputed(source))
    assert isinstance(result, SummaryBuilt)
    assert "label_" not in result.narrative.model_dump_json()


@pytest.mark.parametrize("flag", ["label_words_from_page:no-such-row", "label_words_from_page"])
def test_a_provenance_statement_flag_must_name_a_row_of_its_own_statement(flag: str) -> None:
    source = analysis()
    source.statements[0].flags = [flag]
    assert isinstance(build_summary(_recomputed(source)), SummarySuppressed)
