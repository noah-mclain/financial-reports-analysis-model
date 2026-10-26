"""Synthetic pool metadata and fake stages; no protected corpus documents."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import test_checkpoint_records
import yaml
from harness import checkpoint_records as records
from harness import checkpoint_run as runner
from harness.gates import NO_REFERENCE
from test_checkpoint_records import git

from fra_ingest.config import IngestConfig


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return test_checkpoint_records.repo.__wrapped__(tmp_path)


def metadata(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    documents = [
        {
            "id": f"issuer-{i}",
            "issuer": f"Issuer {i}",
            "pool": "blind",
            "language": "en",
            "period": "annual",
            "role": "corporate",
        }
        for i in range(12)
    ] + [
        {
            "id": f"test-{i}",
            "issuer": f"Test {i}",
            "pool": "model_test",
            "language": "en",
            "period": "interim",
            "role": "corporate",
        }
        for i in range(3)
    ]
    documents.append({**documents[0], "id": "arabic", "language": "ar"})
    fetched = {
        d["id"]: {
            "pool": d["pool"],
            "text_layer": "digital",
            "sha256": hashlib.sha256(d["id"].encode()).hexdigest(),
        }
        for d in documents
    }
    folder = root / "eval/corpus"
    folder.mkdir(parents=True)
    (folder / "candidates.yaml").write_text(yaml.safe_dump({"documents": documents}))
    (folder / "fetched.yaml").write_text(yaml.safe_dump({"documents": fetched}))
    (folder / "rehearsal_reserve.yaml").write_text(
        yaml.safe_dump(
            {"rule": runner.RESERVE_RULE, "issuers": list(runner.reserve_issuers(documents))}
        )
    )
    for d in documents:
        pdf = root / "var/corpus" / d["pool"] / f"{d['id']}.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(d["id"].encode())
    return documents, fetched


def fake_row(entry: dict[str, Any], layer: str, pdf: Path, config: IngestConfig) -> dict[str, Any]:
    if entry["id"] == "test-1":
        raise ValueError("private failure detail")
    return {
        "id": entry["id"],
        "period": entry["period"],
        "text_layer": layer,
        "language": "en",
        "failure": None,
        "seconds": 2.0,
        "peak_gb": 0.1,
        "identity": "ok",
        "identity_outcome": "ok",
        "own_process_group": os.getpgrp() == os.getpid(),
        "statements": {"balance": {"review": "passed"}},
        "industry": {"outcome": "pass"},
        "critical": {"mapped": 6},
        "critical_failure": None,
        "metrics_failure": None,
    }


def test_reservation_deterministic_english_only_and_exactly_eight(repo: Path) -> None:
    documents, fetched = metadata(repo)
    reserve = runner.reserve_issuers(documents)
    assert len(reserve) == 8
    assert reserve == runner.reserve_issuers(list(reversed(documents)))
    selected = runner.select_documents("blind", documents, fetched, reserve)
    assert len(selected) == 4
    assert all(d["language"] == "en" and d["issuer"].lower() not in reserve for d in selected)
    with pytest.raises(records.CheckpointError, match="8"):
        runner.reserve_issuers(documents[:7])
    with pytest.raises(records.CheckpointError, match="reservation"):
        runner.select_documents("blind", documents, fetched, ("arabic-only",) * 8)


def test_cli_requires_checkpoint_before_loading_any_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "load_metadata", lambda *_: pytest.fail("metadata read"))
    assert runner.main(["model_test"]) == 3
    assert runner.main(["blind", "--continue"]) == 3


def test_blind_refused_before_model_test_and_before_pdf_access(repo: Path) -> None:
    metadata(repo)
    with pytest.raises(records.CheckpointError, match="model_test"):
        runner.run_pool("blind", root=repo, config=IngestConfig(), execute=fake_row)


def test_failure_continues_and_summary_cannot_claim_accuracy(repo: Path) -> None:
    metadata(repo)
    report = runner.run_pool("model_test", root=repo, config=IngestConfig(), execute=fake_row)
    assert report["outcomes"] == {"completed": 2, "held_for_review": 0, "declined": 0, "failed": 1}
    assert report["accuracy"] == NO_REFERENCE
    assert report["identity"] == {"passed": 2, "held_with_explanation": 0, "not_met": 1}
    assert "private failure detail" not in json.dumps(report)
    assert report["critical_items"]["mapped"] == 12
    assert report["by_period"]["interim"]["outcomes"] == report["outcomes"]
    assert (repo / "var/checkpoint/model_test_pipeline.done").exists()
    with pytest.raises(records.CheckpointError, match="already"):
        runner.run_pool("model_test", root=repo, config=IngestConfig(), execute=fake_row)


def test_continue_skips_recorded_failures_and_successes(repo: Path) -> None:
    metadata(repo)
    calls = []

    def interrupted(
        entry: dict[str, Any], layer: str, pdf: Path, cfg: IngestConfig
    ) -> dict[str, Any]:
        calls.append(entry["id"])
        if len(calls) == 3:
            raise KeyboardInterrupt
        return fake_row(entry, layer, pdf, cfg)

    with pytest.raises(KeyboardInterrupt):
        runner.run_pool("model_test", root=repo, config=IngestConfig(), execute=interrupted)
    calls.clear()

    def resumed(entry: dict[str, Any], layer: str, pdf: Path, cfg: IngestConfig) -> dict[str, Any]:
        calls.append(entry["id"])
        return fake_row(entry, layer, pdf, cfg)

    report = runner.run_pool(
        "model_test", root=repo, config=IngestConfig(), execute=resumed, continuing=True
    )
    assert calls == ["test-2"]
    assert report["outcomes"]["failed"] == 1
    assert (
        len((repo / "eval/corpus/checkpoint_log.tsv").read_text().splitlines())
        == len(records.LOG_HEADER.splitlines()) + 1
    )


@pytest.mark.parametrize("change", ["commit", "config", "metadata"])
def test_continue_refuses_changed_candidate(repo: Path, change: str) -> None:
    metadata(repo)

    def stop(*args: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    runner_config = IngestConfig()
    with pytest.raises(KeyboardInterrupt):
        runner.run_pool("model_test", root=repo, config=runner_config, execute=stop)
    if change == "commit":
        git(repo, "commit", "--allow-empty", "-qm", "next")
    elif change == "config":
        runner_config = runner_config.model_copy(update={"min_text_chars": 99})
    else:
        path = repo / "eval/corpus/fetched.yaml"
        data = yaml.safe_load(path.read_text())
        data["documents"]["test-0"]["text_layer"] = "mixed"
        path.write_text(yaml.safe_dump(data))
    with pytest.raises(records.CheckpointError, match=r"commit|candidate|configuration"):
        runner.run_pool(
            "model_test", root=repo, config=runner_config, execute=fake_row, continuing=True
        )


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_mismatched_pdf_is_named(repo: Path, missing: bool) -> None:
    metadata(repo)
    pdf = repo / "var/corpus/model_test/test-0.pdf"
    if missing:
        pdf.unlink()
    else:
        pdf.write_bytes(b"wrong")
    with pytest.raises(records.CheckpointError, match="test-0"):
        runner.run_pool("model_test", root=repo, config=IngestConfig(), execute=fake_row)
    assert not (repo / "var/checkpoint/model_test_pipeline.done").exists()


def test_entire_document_child_is_isolated(repo: Path) -> None:
    documents, _ = metadata(repo)
    d = next(d for d in documents if d["id"] == "test-0")
    row = runner.document_in_child(
        d,
        "digital",
        repo / "var/corpus/model_test/test-0.pdf",
        IngestConfig(artifact_root=repo / "artifacts"),
        worker=fake_row,
    )
    assert row["id"] == "test-0"
    assert row["wall_s"] > 0
    assert row["peak_gb"] > 0
    assert row["own_process_group"]


def test_unknown_identity_is_never_accepted_as_explained() -> None:
    rows = [fake_row({"id": "x", "period": "annual"}, "digital", Path("x"), IngestConfig())]
    rows[0]["identity_outcome"] = "failed_unexplained"
    rows[0]["statements"]["balance"]["review"] = "needs_review"
    report = runner.summarize(rows)
    assert report["identity"]["not_met"] == 1
    assert report["identity"]["held_with_explanation"] == 0


def test_reuses_dry_run_pipeline_and_preserves_private_values(tmp_path: Path) -> None:
    from test_dry_run import LABEL, VALUE, Calls, stub_stages

    calls = Calls()
    entry = {"id": "synthetic", "period": "annual", "language": "en", "role": "corporate"}
    row = runner.pipeline_document(
        entry,
        "digital",
        tmp_path / "synthetic.pdf",
        IngestConfig(artifact_root=tmp_path),
        stages=stub_stages(calls),
    )
    assert calls.events == ["locate synthetic", "convert synthetic", "structure synthetic"]
    assert row["identity_outcome"] == "ok"
    assert row["critical"]["mapped"] >= 0
    assert LABEL not in json.dumps(row)
    assert VALUE not in json.dumps(row)
    assert runner.summarize([row])["outcomes"]["held_for_review"] == 1


def test_checkpoint_precedes_pdf_hash_and_any_stage(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata(repo)
    original = runner.sha256_file

    def checked(path: Path) -> str:
        assert (repo / "var/checkpoint/model_test_pipeline.marker").exists()
        assert "\tmodel_test_pipeline\t" in (repo / "eval/corpus/checkpoint_log.tsv").read_text()
        return original(path)

    monkeypatch.setattr(runner, "sha256_file", checked)
    runner.run_pool("model_test", root=repo, config=IngestConfig(), execute=fake_row)


def test_partial_and_declined_outcomes_keep_unmeasured_truth() -> None:
    row = fake_row({"id": "x", "period": "annual"}, "digital", Path("x"), IngestConfig())
    row["industry"] = {"outcome": "declined"}
    row["critical"] = None
    row["identity_outcome"] = "no_balance"
    report = runner.summarize([row])
    assert report["outcomes"]["declined"] == 1
    assert report["identity"]["not_met"] == 1
    assert report["accuracy"] == NO_REFERENCE
    row["industry"] = {"outcome": "pass"}
    row["critical_failure"] = "crash:ValueError"
    assert runner.summarize([row])["outcomes"]["failed"] == 1


def test_projection_uses_saved_holdout_time_and_selected_metadata(repo: Path) -> None:
    documents, fetched = metadata(repo)
    report = repo / "var/eval/dry_run/2026-10-01-fixture.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps({"rows": [{"text_layer": "digital", "failure": None, "seconds": 60}]})
    )
    selected = runner.select_documents(
        "blind", documents, fetched, runner.reserve_issuers(documents)
    )
    projected = runner._projection(repo, "blind", selected, fetched)
    assert projected["pools"]["blind"]["documents"] == 4
    assert projected["pools"]["blind"]["minutes"]["median"] == 4


def sleeping_worker(
    entry: dict[str, Any], layer: str, pdf: Path, config: IngestConfig
) -> dict[str, Any]:
    import time

    time.sleep(30)
    return fake_row(entry, layer, pdf, config)


def crashing_worker(
    entry: dict[str, Any], layer: str, pdf: Path, config: IngestConfig
) -> dict[str, Any]:
    import os

    os._exit(7)


@pytest.mark.parametrize(
    "worker,expected",
    [(sleeping_worker, "pipeline_timeout"), (crashing_worker, "pipeline_child_crashed")],
)
def test_child_timeout_and_crash_have_unknown_peak(
    tmp_path: Path,
    worker: Any,
    expected: str,
) -> None:
    entry = {"id": "synthetic", "period": "annual", "language": "en"}
    config = IngestConfig(artifact_root=tmp_path, child_timeout_s=2.0)
    row = runner.document_in_child(
        entry, "digital", tmp_path / "synthetic.pdf", config, worker=worker
    )
    assert row["failure"] == expected
    assert row["peak_gb"] is None
    assert row["wall_s"] > 0
