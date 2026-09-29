"""Scoring rules of the locator eval."""

import pytest
from harness.locate import (
    check_target,
    found_pages,
    labelled_ranges,
    pool_summary,
    recall,
    top_share,
    truth_label,
    verdict_label,
)

from fra_core.schemas import Document, StatementType
from fra_ingest.results import IndustrySignal, LocateResult, StatementRange

B = StatementType.BALANCE
INC = StatementType.INCOME


def result(
    *ranges: StatementRange, kind: str = "corporate", subkind: str | None = None
) -> LocateResult:
    return LocateResult(
        version="1",
        document=Document(sha256="0" * 64, filename="x.pdf", page_count=20),
        pages=[],
        ranges=list(ranges),
        convert_ranges=[],
        industry=IndustrySignal(kind=kind, subkind=subkind),
    )


def test_labelled_ranges_accept_one_or_several() -> None:
    assert labelled_ranges([5, 6]) == [(5, 6)]
    assert labelled_ranges([[5, 6], [9, 9]]) == [(5, 6), (9, 9)]
    assert labelled_ranges(None) == []


def test_a_labelled_page_is_found_inside_a_padded_range() -> None:
    located = result(StatementRange(type=B, first_page=6, last_page=6, score=8, rank=1))
    assert found_pages(located, B, pad=1) == {5, 6, 7}
    assert recall({5, 6}, found_pages(located, B, pad=1)) == 1.0
    assert recall({5, 9}, found_pages(located, B, pad=1)) == 0.5


def test_top_ranked_pages_ignore_lower_candidates() -> None:
    located = result(
        StatementRange(type=B, first_page=6, last_page=6, score=8, rank=1),
        StatementRange(type=B, first_page=15, last_page=15, score=5, rank=2),
    )
    assert found_pages(located, B, pad=1, max_rank=1) == {5, 6, 7}
    assert found_pages(located, B, pad=1) == {5, 6, 7, 14, 15, 16}
    assert recall({15}, found_pages(located, B, pad=1, max_rank=1)) == 0.0


def test_top_share_counts_merged_top_ranges_of_enabled_types() -> None:
    located = result(
        StatementRange(type=B, first_page=6, last_page=6, score=8, rank=1),
        StatementRange(type=INC, first_page=7, last_page=7, score=8, rank=1),
        StatementRange(type=B, first_page=15, last_page=15, score=5, rank=2),
        StatementRange(type=StatementType.CASH_FLOW, first_page=10, last_page=10, score=8, rank=1),
    )
    # pages 5-8 once each: the balance and income ranges overlap after padding
    assert top_share(located, {B, INC}, pad=1) == 4 / 20


def test_blind_is_refused() -> None:
    with pytest.raises(SystemExit) as caught:
        check_target("blind", checkpoint=False)
    assert caught.value.code == 2


def test_model_test_needs_a_checkpoint() -> None:
    with pytest.raises(SystemExit):
        check_target("model_test", checkpoint=False)
    check_target("model_test", checkpoint=True)
    check_target("train", checkpoint=False)
    check_target("golden", checkpoint=False)


def test_industry_labels_line_up() -> None:
    assert truth_label({"role": "corporate"}) == "corporate"
    assert truth_label({"role": "negative_control", "sector": "bank"}) == "bank"
    assert (
        truth_label(
            {"role": "negative_control", "sector": "other_financial", "subsector": "brokerage"}
        )
        == "other_financial/brokerage"
    )
    assert (
        verdict_label(result(kind="other_financial", subkind="brokerage"))
        == "other_financial/brokerage"
    )
    assert verdict_label(result(kind="insurer")) == "insurer"


def test_errored_and_missing_documents_are_counted_not_dropped() -> None:
    rows = [
        {"id": "a", "truth": "corporate", "verdict": "corporate", "covered": True},
        {"id": "b", "truth": "corporate", "error": "unreadable_pdf"},
        {"id": "c", "truth": "bank", "verdict": "bank", "covered": True},
    ]
    summary = pool_summary(rows, missing=["d"])
    assert summary["coverage"] == 0.5  # the unreadable corporate counts as not covered
    assert summary["uncovered"] == ["b"]
    assert summary["errored"] == ["b"]
    assert summary["missing"] == ["d"]
