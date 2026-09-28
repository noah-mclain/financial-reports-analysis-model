"""Scoring rules of the locator eval."""

import pytest
from harness.locate import (
    check_target,
    found_pages,
    labelled_ranges,
    pool_summary,
    recall,
    truth_label,
    verdict_label,
)

from fra_core.schemas import Document, StatementType
from fra_ingest.results import IndustrySignal, LocateResult, StatementRange

B = StatementType.BALANCE


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
