"""Held summaries require arithmetic evidence for every input in its own period."""

from collections.abc import Callable
from pathlib import Path

import pytest
from test_summary import analysis

from fra_core.schemas import Analysis, CheckResult, Decline
from fra_ingest.review import review_statement
from fra_model import (
    GroundingError,
    SummaryBuilt,
    SummarySuppressed,
    build_summary,
    validate_summary,
)

SCAN_CAVEAT = (
    "Figures were read by OCR from a scanned page. Included figures passed the document's own "
    "arithmetic checks; they have not been verified against an independent source."
)


def held(*, confirmed: bool = True) -> Analysis:
    source = analysis()
    data = source.model_dump(mode="json")
    data["scanned_pages"] = [1]
    data["reviews"][0].update(status="needs_review", reasons=["identity_failed"])
    data["statements"][0]["flags"] = ["needs_review"]
    # Every input is confirmed here, including the held balance: that must still be excluded.
    for statement in data["statements"]:
        for row in statement["line_items"]:
            for cell in row["cells"]:
                cell["arithmetic_checks"] = ["passed-subtotal"] if confirmed else []
    return Analysis.model_validate(data)


def text(result: SummaryBuilt) -> str:
    return "\n".join(s.text for section in result.narrative.sections for s in section.sentences)


def test_held_balance_excludes_all_dependent_facts_even_if_cells_confirmed() -> None:
    source = held()
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert result.partial
    assert result.narrative.cited_metric_ids == {"net_margin", "ebitda"}
    assert text(result).count("because the balance sheet could not be confirmed") == 4
    assert SCAN_CAVEAT in text(result)
    assert source.status().value == "needs_review"


def test_confirmation_is_per_cell_and_period_not_a_pass_or_checked_count() -> None:
    source = held()
    income = source.statements[1]
    # Eight checked figures among thirty-eight do not confer statement-wide approval.
    source.reviews[1] = source.reviews[1].model_copy(
        update={"numeric_cells": 38, "checked_cells": 8}
    )
    # An unchecked current operating-profit cell must exclude EBITDA, even if prior is checked.
    income.line_items[2].cells[1].arithmetic_checks = []
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert result.narrative.cited_metric_ids == {"net_margin"}
    assert "EBITDA was not included because its input figures have not all passed" in text(result)
    income.line_items[1].cells[1].arithmetic_checks = []
    assert isinstance(build_summary(source), SummarySuppressed)


def test_checked_cell_with_reading_change_flag_is_excluded() -> None:
    source = held()
    net_income = source.statements[1].line_items[1].cells[1]
    assert net_income.arithmetic_checks == ["passed-subtotal"]
    net_income.flags.append("ocr_parenthesis_repaired")

    result = build_summary(source)

    assert isinstance(result, SummaryBuilt)
    assert "net_margin" not in result.narrative.cited_metric_ids


def test_critical_cell_review_reason_keeps_unaffected_partial_facts() -> None:
    source = held()
    income = source.statements[1]
    net_income = income.line_items[1]
    cell = net_income.cells[1]
    cell.flags.append("ocr_parenthesis_repaired")
    passed_check = CheckResult(
        id="income:net-income:2025",
        statement_id=income.id,
        kind="subtotal",
        period_key=cell.period_key,
        status="pass",
        line_item_ids=[net_income.id],
    )
    cell.arithmetic_checks = [passed_check.id]
    source.reviews[1] = review_statement(income, [passed_check], primary=True)

    result = build_summary(source)

    assert isinstance(result, SummaryBuilt)
    assert result.narrative.cited_metric_ids == {"ebitda"}
    assert (
        "Net margin was not included because its source figures or calculation could not be verified."
        in text(result)
    )


def test_old_analysis_counts_do_not_authorize_partial_and_zero_confirmed_suppresses() -> None:
    source = held(confirmed=False)
    assert isinstance(build_summary(source), SummarySuppressed)
    data = source.model_dump(mode="json")
    data.pop("scanned_pages")
    for statement in data["statements"]:
        for row in statement["line_items"]:
            for cell in row["cells"]:
                cell.pop("arithmetic_checks")
    assert isinstance(build_summary(Analysis.model_validate(data)), SummarySuppressed)


@pytest.mark.parametrize("language", ["en", "ar"])
def test_completed_summary_matches_prechange_bytes(language: str) -> None:
    result = build_summary(analysis(language))
    assert isinstance(result, SummaryBuilt)
    golden = Path(__file__).parent / "fixtures" / f"completed-{language}.json"
    assert result.narrative.model_dump_json() == golden.read_text()
    assert not result.partial


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: setattr(a, "language", "ar"),
        lambda a: setattr(a.statements[1], "language", "ar"),
        lambda a: setattr(a, "hold", "industry uncertain"),
        lambda a: setattr(a, "decline", Decline(code="bank", reason="unsupported")),
        lambda a: a.flags.append("unknown"),
        lambda a: a.statements[0].flags.append("scale_conflict"),
        lambda a: a.statements[0].flags.append("currency_conflict"),
        lambda a: a.statements[0].flags.append("period_conflict:2"),
        lambda a: a.statements[0].flags.append("unsupported_notice"),
        lambda a: setattr(a, "scanned_pages", None),
    ],
)
def test_document_hazards_suppress_all(mutation: Callable[[Analysis], object]) -> None:
    source = held()
    mutation(source)
    assert isinstance(build_summary(source), SummarySuppressed)


@pytest.mark.parametrize(
    "tamper", ["number", "missing_exclusion", "wrong_exclusion", "missing_scan"]
)
def test_partial_grounding_rejects_tampering(tamper: str) -> None:
    source = held()
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    narrative = result.narrative.model_copy(deep=True)
    watch = narrative.sections[-1].sentences
    if tamper == "number":
        narrative.sections[0].sentences[0].text += " Profit was 36101291."
    elif tamper == "missing_exclusion":
        watch.pop(0)
    elif tamper == "wrong_exclusion":
        watch[0].text = "Current ratio was not included because it is undefined."
    else:
        watch.pop()
    with pytest.raises(GroundingError):
        validate_summary(source, narrative)


def test_missing_undefined_and_flagged_facts_have_honest_exclusions() -> None:
    source = held()
    source.metrics = [m for m in source.metrics if m.metric_id != "ebitda"]
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert "EBITDA was not included because the metric is unavailable." in text(result)
    source = held()
    metric = next(m for m in source.metrics if m.metric_id == "ebitda" and m.period_key == "2025")
    metric.value = None
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert "EBITDA was not included because the metric is undefined." in text(result)
    source = held()
    source.statements[1].line_items[2].cells[1].flags = ["digit_suspect"]
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert (
        "EBITDA was not included because its source figures or calculation could not be verified."
        in text(result)
    )


def test_local_holds_do_not_hide_unaffected_confirmed_income_facts() -> None:
    from fra_analytics import Policy, compute
    from fra_core.schemas.statement import MappingFinding

    source = held()
    income = source.statements[1]
    income.flags = ["needs_review", "subtotal_failed"]
    income.mapping_findings = (
        MappingFinding(item_id="operating_income", observed_period_keys=("2025",)),
    )
    # The unrelated missing operating-profit mapping excludes EBITDA, not net margin.
    income.line_items[2].canonical_id = None
    source.reviews[1] = source.reviews[1].model_copy(
        update={
            "status": "needs_review",
            "reasons": ["subtotal_failed", "critical_item_unmapped:operating_income"],
        }
    )
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
    assert result.narrative.cited_metric_ids == {"net_margin"}
    assert source.reviews[1].status == "needs_review"
    assert "EBITDA was not included because the metric is undefined." in text(result)


def test_partial_never_drops_a_metric_warning_merely_because_it_names_a_known_hold() -> None:
    source = held()
    metric = next(m for m in source.metrics if m.metric_id == "ebitda" and m.period_key == "2025")
    metric.flags = ["subtotal_failed"]  # no source statement carries this notice
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert result.narrative.cited_metric_ids == {"net_margin"}


def test_juhayna_like_eight_of_thirty_eight_and_wrong_unchecked_prior_cells() -> None:
    from decimal import Decimal

    from fra_analytics import Policy, compute
    from fra_core.schemas import LineItem

    source = held()
    income = source.statements[1]
    # Four covered rows in two periods; the other fifteen rows remain merely read.
    prototype = income.line_items[0]
    for index in range(15):
        canonical = "gross_profit" if index == 0 else "profit_before_tax" if index == 1 else None
        cells = [c.model_copy(deep=True) for c in prototype.cells]
        for cell in cells:
            cell.arithmetic_checks = []
        if index < 2:
            cells[0].reported = Decimal("36101291" if index == 0 else "13646719")
            cells[0].raw_text = str(cells[0].reported)
        income.line_items.append(
            LineItem(
                id=f"unchecked-{index}",
                raw_label=f"Unchecked row {index}",
                canonical_id=canonical,
                cells=cells,
            )
        )
    assert sum(c.reported is not None for r in income.line_items for c in r.cells) == 38
    assert sum(bool(c.arithmetic_checks) for r in income.line_items for c in r.cells) == 8
    source.reviews[1] = source.reviews[1].model_copy(
        update={"numeric_cells": 38, "checked_cells": 8}
    )
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
    assert result.narrative.cited_metric_ids == {"net_margin", "ebitda"}
    assert "36101291" not in text(result) and "13646719" not in text(result)
    # Coverage in the previous column never authorizes an unchecked current input.
    income.line_items[1].cells[1].arithmetic_checks = []
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    assert result.narrative.cited_metric_ids == {"ebitda"}
    # Coverage in the current column never authorizes an unchecked previous input either.
    income.line_items[0].cells[1].arithmetic_checks = []
    income.periods = [income.periods[0]]
    for row in income.line_items:
        row.cells = [row.cells[0]]
    income.line_items[0].cells[0].arithmetic_checks = []
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
    assert result.narrative.cited_metric_ids == {"ebitda"}


@pytest.mark.parametrize("notice", ["needs_review:invented", "subtotal_failed:invented"])
def test_unknown_statement_notice_suffix_suppresses_even_unrelated_facts(notice: str) -> None:
    source = held()
    source.statements[0].flags.append(notice)
    assert isinstance(build_summary(source), SummarySuppressed)


def test_unknown_review_reason_suffix_suppresses_even_unrelated_facts() -> None:
    source = held()
    source.reviews[0] = source.reviews[0].model_copy(update={"reasons": ["unparsed:invented"]})
    assert isinstance(build_summary(source), SummarySuppressed)
