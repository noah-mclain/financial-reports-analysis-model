"""Scoring rules of the locator eval."""

from pathlib import Path

import pytest
import yaml
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
        {
            "id": "a",
            "period": "annual",
            "truth": "corporate",
            "verdict": "corporate",
            "covered": True,
        },
        {"id": "b", "period": "annual", "truth": "corporate", "error": "unreadable_pdf"},
        {"id": "c", "period": "annual", "truth": "bank", "verdict": "bank", "covered": True},
    ]
    summary = pool_summary(rows, missing=["d"])
    assert summary["coverage"] == 0.5  # the unreadable corporate counts as not covered
    assert summary["uncovered"] == ["b"]
    assert summary["errored"] == ["b"]
    assert summary["missing"] == ["d"]


# ---- the decline check: the industry decision against the sector label ------------------------

from harness.locate import (  # noqa: E402
    decision_label,
    decline_summary,
    judge_decision,
    pool_entries,
)

from fra_ingest.industry import industry_decision  # noqa: E402


@pytest.mark.parametrize(
    ("truth", "decision", "judgement"),
    [
        ("bank", "declined/bank", "right"),
        ("insurer", "declined/insurer", "right"),
        ("bank", "declined/insurer", "wrong"),  # declined, but the reason names the wrong kind
        ("bank", "needs_review/weak_verdict", "held"),
        ("bank", "pass", "wrong"),  # a bank that passes as corporate
        ("insurer", "pass", "wrong"),
        ("other_financial/brokerage", "needs_review/other_financial", "held"),
        ("other_financial/brokerage", "pass", "wrong"),
        ("other_financial/brokerage", "declined/bank", "wrong"),
        ("corporate", "pass", "right"),
        ("corporate", "needs_review/near_threshold", "held"),
        ("corporate", "declined/bank", "wrong"),  # a corporate document declined
    ],
)
def test_a_decision_is_right_held_or_wrong_for_its_sector_label(
    truth: str, decision: str, judgement: str
) -> None:
    assert judge_decision(truth, decision) == judgement


def test_the_decision_label_reads_the_decision_not_the_verdict() -> None:
    assert decision_label(None) == "pass"
    held = industry_decision(IndustrySignal(kind="unknown"))
    assert decision_label(held) == "needs_review/no_verdict"
    sure = industry_decision(IndustrySignal(kind="bank", score=20.0, distinct_cues=5))
    assert decision_label(sure) == "declined/bank"


def test_the_decline_summary_counts_by_judgement_and_lists_the_wrong_ones() -> None:
    rows = [
        {
            "id": "a",
            "period": "annual",
            "truth": "bank",
            "decision": "declined/bank",
            "judgement": "right",
        },
        {"id": "b", "period": "annual", "truth": "bank", "decision": "pass", "judgement": "wrong"},
        {
            "id": "c",
            "period": "interim",
            "truth": "insurer",
            "decision": "needs_review/weak_verdict",
            "judgement": "held",
        },
        {
            "id": "d",
            "period": "interim",
            "truth": "corporate",
            "decision": "declined/bank",
            "judgement": "wrong",
        },
        {
            "id": "e",
            "period": "interim",
            "truth": "corporate",
            "decision": "pass",
            "judgement": "right",
        },
        {"id": "f", "period": "annual", "truth": "bank", "error": "unreadable_pdf"},
    ]
    summary = decline_summary(rows)
    assert summary["negative_controls"] == {"right": 1, "held": 1, "wrong": 1, "errored": 1}
    assert summary["corporate"] == {"right": 1, "held": 0, "wrong": 1, "errored": 0}
    assert summary["wrong"] == [
        {"id": "b", "truth": "bank", "decision": "pass"},
        {"id": "d", "truth": "corporate", "decision": "declined/bank"},
    ]
    assert summary["errored"] == ["f"]
    annual, interim = summary["by_period_kind"]["annual"], summary["by_period_kind"]["interim"]
    assert annual["negative_controls"] == {"right": 1, "held": 0, "wrong": 1, "errored": 1}
    assert annual["corporate"] == {"right": 0, "held": 0, "wrong": 0, "errored": 0}
    assert interim["negative_controls"] == {"right": 0, "held": 1, "wrong": 0, "errored": 0}
    assert interim["corporate"] == {"right": 1, "held": 0, "wrong": 1, "errored": 0}


def test_the_decline_summary_names_an_empty_period_kind() -> None:
    rows = [{"id": "a", "period": "annual", "truth": "bank", "judgement": "right"}]
    interim = decline_summary(rows)["by_period_kind"]["interim"]
    assert interim["negative_controls"] == {"right": 0, "held": 0, "wrong": 0, "errored": 0}


def test_the_train_target_is_split_aware_and_never_reads_the_holdout(tmp_path: Path) -> None:
    from harness.holdout_records import LOG_HEADER

    from fra_core.split import Part, hashed_part, issuer_key

    def issuer(part: Part) -> str:
        return next(
            n for n in (f"Issuer {k}" for k in range(300)) if hashed_part(issuer_key(n)) is part
        )

    records = [
        {"id": "f1", "issuer": issuer(Part.FIT), "pool": "train"},
        {"id": "v1", "issuer": issuer(Part.VALIDATION), "pool": "train"},
        {"id": "h1", "issuer": issuer(Part.HOLDOUT), "pool": "train"},
        {"id": "d1", "issuer": "Almarai Company", "pool": "dev"},
    ]
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"documents": records}), encoding="utf-8")
    (tmp_path / "m.yaml").write_text(yaml.safe_dump({"moves": []}), encoding="utf-8")
    (tmp_path / "l.tsv").write_text(LOG_HEADER, encoding="utf-8")
    paths = (tmp_path / "c.yaml", tmp_path / "m.yaml", tmp_path / "l.tsv")
    assert [e["id"] for e in pool_entries("train", *paths)] == ["f1", "v1"]
    assert [e["id"] for e in pool_entries("dev", *paths)] == ["d1"]


# ---- the wiring of the pool run ---------------------------------------------------------------


def _train_records(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A candidates file with a fit, a validation and a holdout document, each with a PDF in a
    tmp store, and a tmp moves file and scoring log."""
    from harness.holdout_records import LOG_HEADER

    from fra_core.split import Part, hashed_part, issuer_key

    def issuer(part: Part) -> str:
        return next(
            n for n in (f"Issuer {k}" for k in range(300)) if hashed_part(issuer_key(n)) is part
        )

    records = [
        {
            "id": "f1",
            "issuer": issuer(Part.FIT),
            "pool": "train",
            "role": "corporate",
            "period": "annual",
        },
        {
            "id": "v1",
            "issuer": issuer(Part.VALIDATION),
            "pool": "train",
            "role": "corporate",
            "period": "annual",
        },
        {
            "id": "h1",
            "issuer": issuer(Part.HOLDOUT),
            "pool": "train",
            "role": "corporate",
            "period": "annual",
        },
    ]
    store = tmp_path / "store"
    (store / "train").mkdir(parents=True)
    for r in records:
        (store / "train" / f"{r['id']}.pdf").write_bytes(b"x")
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"documents": records}), encoding="utf-8")
    (tmp_path / "m.yaml").write_text(yaml.safe_dump({"moves": []}), encoding="utf-8")
    (tmp_path / "l.tsv").write_text(LOG_HEADER, encoding="utf-8")
    return tmp_path / "c.yaml", tmp_path / "m.yaml", tmp_path / "l.tsv", store


def _located_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int | None = None
) -> list[str]:
    import harness.locate as locate_module

    candidates, moves, log, store = _train_records(tmp_path)
    seen: list[str] = []

    def fake_locate(path: Path, *args: object, **kwargs: object) -> LocateResult:
        seen.append(path.stem)
        return result(
            StatementRange(type=B, first_page=1, last_page=1, score=8, rank=1),
            StatementRange(type=INC, first_page=2, last_page=2, score=8, rank=1),
        )

    monkeypatch.setattr(locate_module, "locate_pdf", fake_locate)
    locate_module.run_pool(
        "train", True, limit, candidates=candidates, moves=moves, log=log, store=store
    )
    return seen


def test_the_train_run_locates_the_fit_and_validation_parts_and_never_the_holdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _located_paths(monkeypatch, tmp_path) == ["f1", "v1"]


def test_the_limit_takes_the_first_documents_in_id_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _located_paths(monkeypatch, tmp_path, limit=1) == ["f1"]


@pytest.mark.parametrize("limit", ["0", "-1", "x"])
def test_a_limit_that_is_not_a_positive_number_is_refused(limit: str) -> None:
    from harness.locate import main

    with pytest.raises(SystemExit) as caught:
        main(["train", "--limit", limit])
    assert caught.value.code == 2


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_an_ocr_failure_is_recorded_against_its_document_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: str,
) -> None:
    import harness.locate as locate_module

    from fra_ingest.ocr import OcrEngineError, OcrTimeoutError

    candidates, moves, log, store = _train_records(tmp_path)
    raised = {"timeout": OcrTimeoutError, "engine": OcrEngineError}[error]

    def fake_locate(path: Path, *args: object, **kwargs: object) -> LocateResult:
        if path.stem == "f1":
            raise raised("tesseract did not finish within 120.0 s")
        return result(
            StatementRange(type=B, first_page=1, last_page=1, score=8, rank=1),
            StatementRange(type=INC, first_page=2, last_page=2, score=8, rank=1),
        )

    monkeypatch.setattr(locate_module, "locate_pdf", fake_locate)
    summary = locate_module.run_pool(
        "train", True, candidates=candidates, moves=moves, log=log, store=store
    )
    captured = capsys.readouterr()
    printed = captured.out
    assert f"unreadable: f1: ocr_{error} (tesseract did not finish within 120.0 s)" in printed
    assert captured.err.splitlines() == ["1/2 f1 ...", "2/2 v1 ..."]
    assert summary["errored"] == ["f1"]
    failed = next(r for r in summary["documents"] if r["id"] == "f1")
    assert failed["error"] == f"ocr_{error}"
    assert failed["detail"] == "tesseract did not finish within 120.0 s"
    assert [r["id"] for r in summary["documents"]] == ["f1", "v1"]


def test_a_run_where_every_document_fails_on_the_engine_stops_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import harness.locate as locate_module

    from fra_ingest.ocr import OcrEngineError

    candidates, moves, log, store = _train_records(tmp_path)

    def broken(path: Path, *args: object, **kwargs: object) -> LocateResult:
        raise OcrEngineError("tesseract --list-langs failed")

    monkeypatch.setattr(locate_module, "locate_pdf", broken)
    with pytest.raises(RuntimeError, match="every document failed with ocr_engine"):
        locate_module.run_pool(
            "train", True, candidates=candidates, moves=moves, log=log, store=store
        )


def test_a_missing_file_is_named_as_the_pool_run_reaches_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import harness.locate as locate_module

    candidates, moves, log, store = _train_records(tmp_path)
    (store / "train" / "f1.pdf").unlink()

    def fake_locate(path: Path, *args: object, **kwargs: object) -> LocateResult:
        return result(
            StatementRange(type=B, first_page=1, last_page=1, score=8, rank=1),
            StatementRange(type=INC, first_page=2, last_page=2, score=8, rank=1),
        )

    monkeypatch.setattr(locate_module, "locate_pdf", fake_locate)
    summary = locate_module.run_pool(
        "train", True, candidates=candidates, moves=moves, log=log, store=store
    )
    assert capsys.readouterr().err.splitlines() == ["1/2 f1 missing", "2/2 v1 ..."]
    assert summary["missing"] == ["f1"]
