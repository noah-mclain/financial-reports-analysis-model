"""Independent gold checks use synthetic PDFs/labels, never held-out content."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from harness.independent_gold import FrozenGold, Reading, aggregate_gold, counts, freeze
from pydantic import ValidationError
from tests.eval.test_expected import SHA, item, result, statement


def reading(reader: str = "reader-a") -> dict[str, Any]:
    return {
        "id": "synthetic",
        "sha256": SHA,
        "reader": reader,
        "source": "pdf_only",
        "complete": True,
        "statements": [
            {
                "type": "balance",
                "pages": [5],
                "page_mode": "digital",
                "scale": 1,
                "currency": "EGP",
                "periods": [{"key": "2025-12-31", "end_date": "2025-12-31", "kind": "instant"}],
                "rows": [
                    {
                        "label": "Total assets",
                        "values": {"2025-12-31": "5"},
                        "canonical_id": "total_assets",
                    }
                ],
            }
        ],
    }


def gold() -> FrozenGold:
    return FrozenGold(
        readings=(Reading.model_validate(reading()), Reading.model_validate(reading("reader-b")))
    )


def test_disagreement_and_same_reader_refused() -> None:
    first = reading()
    with pytest.raises(ValidationError, match="distinct"):
        FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(first)))
    second = reading("reader-b")
    second["statements"][0]["rows"][0]["values"]["2025-12-31"] = "6"
    with pytest.raises(ValidationError, match="disagree"):
        FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))


@pytest.mark.parametrize("change", ["unconfirmed", "missing_period", "bad_taxonomy", "nonfinite"])
def test_incomplete_answers_refused(change: str) -> None:
    data = reading()
    row = data["statements"][0]["rows"][0]
    if change == "unconfirmed":
        row["unconfirmed"] = ["2025-12-31"]
    elif change == "missing_period":
        row["values"] = {}
    elif change == "bad_taxonomy":
        row["canonical_id"] = "revenue"
    else:
        row["values"]["2025-12-31"] = "NaN"
    with pytest.raises(ValidationError):
        Reading.model_validate(data)


def test_freeze_never_replaces_answers(tmp_path: Path) -> None:
    first, second, out = (tmp_path / name for name in ("a.json", "b.json", "gold.json"))
    first.write_text(json.dumps(reading()))
    second.write_text(json.dumps(reading("reader-b")))
    freeze(first, second, out)
    original = out.read_bytes()
    with pytest.raises(FileExistsError):
        freeze(first, second, out)
    assert out.read_bytes() == original


def test_missing_statement_and_failed_run_stay_in_denominator() -> None:
    score = counts(gold(), None)[0]
    assert (score["cells"], score["right"], score["mapping_rows"], score["mapping_right"]) == (
        1,
        0,
        1,
        0,
    )
    report = aggregate_gold(
        [{"period": "annual", "language": "en", "gold": [score], "gold_method": "double_pdf"}]
    )
    assert report["strata"]["total"]["figures"]["digital"]["met"] is False
    assert report["strata"]["interim"]["figures"]["digital"]["accuracy"] is None


def test_accuracy_and_mapping_are_independent_and_answers_do_not_escape() -> None:
    extracted = item(1, "Total assets", "5")
    extracted.canonical_id = "total_equity"
    scored = counts(gold(), result([statement(1, [extracted])]))[0]
    assert scored["right"] == 1
    assert scored["mapping_right"] == 0
    assert not any(
        key in json.dumps(scored) for key in ("Total assets", "total_assets", "2025-12-31")
    )
    extracted.cells[0].reported = Decimal("6")
    assert counts(gold(), result([statement(1, [extracted])]))[0]["right"] == 0


def test_hash_and_duplicate_statement_safety() -> None:
    actual = result([statement(1, [item(1, "Total assets", "5")])])
    actual.sha256 = "b" * 64
    with pytest.raises(ValueError, match="hash"):
        counts(gold(), actual)
    duplicate = result(
        [statement(1, [item(1, "Total assets", "5")]), statement(2, [item(1, "Total assets", "5")])]
    )
    assert counts(gold(), duplicate)[0]["right"] == 0


def test_missing_blank_row_is_not_a_correct_cell() -> None:
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["statements"][0]["rows"][0]["values"]["2025-12-31"] = None
    frozen = FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))
    assert counts(frozen, result([statement(1, [])]))[0]["right"] == 0


def call_run(setup: Any, directory: Path, stages: Any) -> dict[str, Any]:
    from harness.dry_run import run
    from harness.holdout_records import log_look, read_looks
    from tests.eval.test_dry_run import LOOK

    from fra_ingest.config import IngestConfig

    return run(
        setup.documents,
        setup.fetched,
        [],
        read_looks(setup.log),
        look=LOOK,
        record=lambda look: log_look(setup.log, look),
        store=setup.store,
        report_path=setup.report,
        artifacts_root=setup.artifacts,
        config=IngestConfig(artifact_root=setup.tmp / "work"),
        stages=stages,
        layers_by_pool={},
        gold_dir=directory,
    )


def test_missing_gold_refuses_before_a_look(tmp_path: Path) -> None:
    from harness.dry_run import PreflightFailed
    from harness.holdout_records import read_looks
    from tests.eval.test_dry_run import HOLDOUT_ISSUERS, Setup, candidate_record, stub_stages

    setup = Setup(tmp_path, [candidate_record(HOLDOUT_ISSUERS[0])])
    directory = tmp_path / "gold"
    directory.mkdir()
    with pytest.raises(PreflightFailed, match="no look"):
        call_run(setup, directory, stub_stages())
    assert read_looks(setup.log) == []
    assert not setup.report.exists()


@pytest.mark.parametrize("bad_hash", [False, True])
def test_gold_run_hash_checks_and_count_only_report(tmp_path: Path, bad_hash: bool) -> None:
    import hashlib

    from harness.dry_run import PreflightFailed
    from harness.holdout_records import read_looks
    from tests.eval.test_dry_run import HOLDOUT_ISSUERS, Setup, candidate_record, stub_stages

    setup = Setup(tmp_path, [candidate_record(HOLDOUT_ISSUERS[0])])
    document_sha = hashlib.sha256(b"").hexdigest()
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["id"] = setup.documents[0]["id"]
        data["sha256"] = SHA if bad_hash else document_sha
    frozen = FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))
    directory = tmp_path / "gold"
    directory.mkdir()
    (directory / f"{frozen.document.id}.json").write_text(frozen.model_dump_json())
    actual = result([statement(1, [item(1, "Total assets", "5")])])
    actual.sha256 = document_sha
    if bad_hash:
        with pytest.raises(PreflightFailed, match="no look"):
            call_run(setup, directory, stub_stages(result=actual))
        assert read_looks(setup.log) == []
    else:
        report = call_run(setup, directory, stub_stages(result=actual))
        assert len(read_looks(setup.log)) == 1
        assert report["gold_accuracy"]["strata"]["total"]["figures"]["digital"]["right"] == 1
        assert report["reproducibility"]["expected"][frozen.document.id]
        assert "Total assets" not in json.dumps(report)
        assert not setup.artifacts.exists()


@pytest.mark.parametrize("method,complete", [("assisted_review", True), ("double_pdf", False)])
def test_assisted_or_partial_scores_never_claim_a_gate_pass(method: str, complete: bool) -> None:
    score = counts(gold(), result([statement(1, [item(1, "Total assets", "5")])]))
    report = aggregate_gold(
        [{"period": "annual", "language": "en", "gold": score, "gold_method": method}],
        complete=complete,
    )
    total = report["strata"]["total"]
    assert total["methods"][method] == 1
    assert total["figures"]["digital"]["met"] is None
    assert total["figures"]["digital"]["observed_accuracy"] == 1.0
    if not complete:
        assert total["figures"]["digital"]["accuracy"] is None
        assert report["measurement"] == "incomplete_not_a_gate_verdict"


def test_packet_subset_refuses_before_a_holdout_look(tmp_path: Path) -> None:
    import hashlib

    from harness.dry_run import PreflightFailed
    from harness.holdout_records import read_looks
    from tests.eval.test_dry_run import HOLDOUT_ISSUERS, Setup, candidate_record, stub_stages

    setup = Setup(tmp_path, [candidate_record(HOLDOUT_ISSUERS[0])])
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["id"] = setup.documents[0]["id"]
        data["sha256"] = hashlib.sha256(b"").hexdigest()
    packet = FrozenGold(
        readings=(Reading.model_validate(first), Reading.model_validate(second)),
        reference_scope="reviewed_packet_slots",
    )
    directory = tmp_path / "gold"
    directory.mkdir()
    (directory / f"{packet.document.id}.json").write_text(packet.model_dump_json())
    with pytest.raises(PreflightFailed, match="no look"):
        call_run(setup, directory, stub_stages())
    assert read_looks(setup.log) == []
