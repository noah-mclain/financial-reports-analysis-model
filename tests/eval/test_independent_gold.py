"""Independent gold checks use synthetic PDFs/labels, never held-out content."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from harness.dry_run import rows_path as rows_file
from harness.independent_gold import FrozenGold, Reading, aggregate_gold, counts, freeze
from pydantic import ValidationError
from tests.eval.test_expected import SHA, item, result, statement

from fra_core.schemas import StatementType


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


@pytest.mark.parametrize("missing", [None, "row", "cell", "period", "cell_period"])
def test_aligned_blank_requires_explicit_evidence(missing: str | None) -> None:
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["statements"][0]["rows"][0]["values"]["2025-12-31"] = None
    frozen = FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))
    blank = item(1, "Total assets", "")
    actual = statement(1, [blank])
    if missing == "row":
        actual.line_items = []
    elif missing == "cell":
        blank.cells = []
    elif missing == "period":
        actual.periods = [actual.periods[0].model_copy(update={"key": "another-period"})]
        blank.cells[0].period_key = "another-period"
    elif missing == "cell_period":
        actual.periods.append(actual.periods[0].model_copy(update={"key": "another-period"}))
        blank.cells[0].period_key = "another-period"
    scored = counts(frozen, result([actual]))[0]
    assert (scored["cells"], scored["right"]) == (1, int(missing is None))


def test_a_blank_is_not_credited_when_the_period_column_was_dropped() -> None:
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["statements"][0]["periods"].append(
            {"key": "2024-12-31", "end_date": "2024-12-31", "kind": "instant"}
        )
        data["statements"][0]["rows"][0]["values"] = {"2025-12-31": "5", "2024-12-31": None}
    frozen = FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))
    scored = counts(frozen, result([statement(1, [item(1, "Total assets", "5")])]))[0]
    assert (scored["cells"], scored["right"]) == (2, 1)


def test_an_unparsed_cell_is_not_a_printed_blank() -> None:
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["statements"][0]["rows"][0]["values"]["2025-12-31"] = None
    frozen = FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))
    unparsed = item(1, "Total assets", "")
    unparsed.cells[0].raw_text = "1x5"
    unparsed.cells[0].flags = ["unparsed"]
    scored = counts(frozen, result([statement(1, [unparsed])]))[0]
    assert (scored["cells"], scored["right"]) == (1, 0)


def test_mapping_credits_a_readable_label_whose_figure_is_wrong() -> None:
    noisy = item(1, "Tota1 assets", "99")
    noisy.canonical_id = "total_assets"
    scored = counts(gold(), result([statement(1, [noisy])]))[0]
    assert (scored["cells"], scored["right"]) == (1, 0)
    assert (scored["mapping_rows"], scored["mapping_right"]) == (1, 1)
    unrelated = item(1, "Cash", "99")
    unrelated.canonical_id = "total_assets"
    scored = counts(gold(), result([statement(1, [unrelated])]))[0]
    assert (scored["mapping_rows"], scored["mapping_right"]) == (1, 0)


def test_a_readable_label_under_a_wrong_id_is_still_a_mapping_miss() -> None:
    noisy = item(1, "Tota1 assets", "99")
    noisy.canonical_id = "total_equity"
    scored = counts(gold(), result([statement(1, [noisy])]))[0]
    assert (scored["mapping_rows"], scored["mapping_right"]) == (1, 0)


@pytest.mark.parametrize("balances,incomes", [(1, 0), (2, 0), (0, 1), (1, 1), (2, 2), (0, 0)])
def test_all_output_predictions_count_without_choosing_duplicate_statements(
    balances: int,
    incomes: int,
) -> None:
    statements = []
    for n in range(balances + incomes):
        kind = StatementType.BALANCE if n < balances else StatementType.INCOME
        mapped = item(1, "Total assets", "5")
        mapped.canonical_id = "total_assets"
        extra = item(2, "Extra output", "9")
        extra.canonical_id = "total_equity"
        heading = item(3, "Heading", None)
        heading.canonical_id = "total_assets"
        statements.append(statement(n, [mapped, extra, heading], kind))
    scored = counts(gold(), result(statements))
    unique = int(balances == 1)
    assert sum(s["right"] for s in scored) == unique
    assert sum(s["cells"] for s in scored) == 1
    assert sum(s["statements"] for s in scored) == 1
    assert sum(s["statements_found"] for s in scored) == unique
    assert sum(s["mapping_rows"] for s in scored) == 1
    assert sum(s["mapping_right"] for s in scored) == unique
    assert sum(s["mapping_predictions"] for s in scored) == 3 * (balances + incomes)
    assert sum(s["extra_rows"] for s in scored) == 2 * (balances + incomes) - unique
    report = aggregate_gold([{"period": "annual", "gold": scored, "gold_method": "double_pdf"}])
    precision = report["strata"]["total"]["measures"]["mapping_predictions"]
    assert precision["total"] == 3 * (balances + incomes)
    assert precision["right"] == unique
    assert not any(
        value in json.dumps(scored)
        for value in ("Total assets", "total_assets", "Extra output", "2025-12-31")
    )


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


def test_gold_counts_stay_private_to_the_run(tmp_path: Path) -> None:
    import hashlib

    from harness.holdout_records import read_looks
    from tests.eval.test_dry_run import HOLDOUT_ISSUERS, Setup, candidate_record, stub_stages

    setup = Setup(tmp_path, [candidate_record(HOLDOUT_ISSUERS[0])])
    document_sha = hashlib.sha256(b"").hexdigest()
    first, second = reading(), reading("reader-b")
    for data in (first, second):
        data["id"] = setup.documents[0]["id"]
        data["sha256"] = document_sha
    frozen = FrozenGold(readings=(Reading.model_validate(first), Reading.model_validate(second)))
    directory = tmp_path / "gold"
    directory.mkdir()
    (directory / f"{frozen.document.id}.json").write_text(frozen.model_dump_json())
    actual = result([statement(1, [item(1, "Total assets", "5")])])
    actual.sha256 = document_sha
    report = call_run(setup, directory, stub_stages(result=actual))
    assert len(read_looks(setup.log)) == 1
    written = [json.loads(line) for line in rows_file(setup.report).read_text().splitlines()]
    assert len(written) == len(report["rows"]) == 1
    for row in [*written, *report["rows"]]:
        assert not {"gold", "gold_method"} & set(row)
    total = report["gold_accuracy"]["strata"]["total"]
    assert total["figures"]["digital"]["right"] == 1
    assert (total["documents"], total["labeled_documents"]) == (1, 1)
    assert total["methods"]["double_pdf"] == 1


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
