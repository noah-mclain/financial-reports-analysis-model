"""Synthetic statements exercise the entire direct-template contract."""

from datetime import date
from decimal import Decimal

import pytest

from fra_analytics import Policy, compute
from fra_core.schemas import (
    Analysis,
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementReview,
    StatementType,
)
from fra_core.schemas.narrative import GroundingResult
from fra_model import SummaryBuilt, SummarySuppressed, build_summary


def analysis(language: str = "en") -> Analysis:
    statements = []
    for kind, amounts in [
        (
            StatementType.BALANCE,
            {
                "total_current_assets": "300",
                "total_current_liabilities": "100",
                "short_term_borrowings": "10",
                "current_portion_long_term_debt": "20",
                "long_term_borrowings": "30",
                "lease_liabilities_current": "5",
                "lease_liabilities_non_current": "5",
                "cash_and_equivalents": "5",
                "short_term_investments": "5",
                "trade_receivables": "20",
            },
        ),
        (
            StatementType.INCOME,
            {
                "revenue": "200",
                "net_income": "25",
                "operating_income": "40",
                "depreciation_amortization": "10",
            },
        ),
    ]:
        periods = [
            Period(
                key=str(year),
                end_date=date(year, 12, 31),
                kind=PeriodKind.INSTANT if kind is StatementType.BALANCE else PeriodKind.DURATION,
                months=None if kind is StatementType.BALANCE else 12,
            )
            for year in [2024, 2025]
        ]
        rows = [
            LineItem(
                id=f"{kind}-{i}",
                raw_label=key,
                canonical_id=key,
                cells=[
                    Cell(
                        period_key=p.key,
                        reported=Decimal(amount),
                        raw_text=amount,
                        provenance=Provenance(
                            page_no=1,
                            bbox=BBox(left=0, top=0, right=10, bottom=10),
                            table_ref=str(kind),
                            row=i,
                            col=j,
                        ),
                    )
                    for j, p in enumerate(periods)
                ],
            )
            for i, (key, amount) in enumerate(amounts.items())
        ]
        statements.append(
            Statement(
                id=str(kind),
                document_sha256="a" * 64,
                type=kind,
                currency="EGP",
                scale=1,
                language=language,
                periods=periods,
                line_items=rows,
                source_pages=[1],
            )
        )
    return Analysis(
        job_id="b" * 32,
        upload_sha256="a" * 64,
        page_count=1,
        language=language,
        statements=statements,
        reviews=[
            StatementReview(
                statement_id=s.id,
                status="passed",
                numeric_cells=2 * len(s.line_items),
                checked_cells=2 * len(s.line_items),
                flagged_cells=0,
            )
            for s in statements
        ],
        metrics=compute(
            statements,
            Policy(
                negative_margin_denominator="compute_and_flag",
                include_lease_liabilities=True,
                day_count_basis=365,
            ),
        ),
    )


@pytest.mark.parametrize("language", ["en", "ar"])
def test_six_neutral_latest_facts_with_explicit_periods(language: str) -> None:
    source = analysis(language)
    built = build_summary(source)
    assert isinstance(built, SummaryBuilt)
    narrative = built.narrative
    assert narrative.origin == "direct_template" and narrative.model_id is None
    assert narrative.grounding is GroundingResult.PASSED
    sentences = [s for section in narrative.sections for s in section.sentences]
    assert len(sentences) == 6
    assert [s.claims[0].metric_id for s in sentences] == [
        "current_ratio",
        "total_debt",
        "net_debt_to_ebitda",
        "net_margin",
        "ebitda",
        "dso",
    ]
    assert all(s.claims[0].period_key == "2025" for s in sentences)
    assert "2025-12-31" in sentences[0].text
    assert ("As at" if language == "en" else "في") in sentences[0].text
    assert ("months: 12" if language == "en" else "المدة بالأشهر: 12") in sentences[3].text
    assert source.narrative is None  # pure builder


def test_latest_null_does_not_substitute_older_value() -> None:
    source = analysis()
    latest = next(
        m for m in source.metrics if m.metric_id == "current_ratio" and m.period_key == "2025"
    )
    latest.value = None
    latest.flags = ["undefined_zero_denominator"]
    built = build_summary(source)
    assert isinstance(built, SummaryBuilt)
    assert "current_ratio" not in built.narrative.cited_metric_ids


def test_no_facts_suppresses_without_placeholder() -> None:
    source = analysis()
    source.metrics = []
    result = build_summary(source)
    assert isinstance(result, SummarySuppressed)
    assert result.reasons
