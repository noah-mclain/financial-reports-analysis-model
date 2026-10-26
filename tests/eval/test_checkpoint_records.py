"""Checkpoint lifecycle on disposable repositories only."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from harness import checkpoint_records as records


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked").write_text("clean")
    git(tmp_path, "add", "tracked")
    git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def test_start_logs_before_return_and_refuses_second_start(repo: Path) -> None:
    records.begin("model_test_pipeline", candidate="adapter hash", root=repo)
    log = repo / "eval/corpus/checkpoint_log.tsv"
    text = log.read_text()
    assert text.startswith(records.LOG_HEADER)
    assert "\tmodel_test_pipeline\tadapter hash\t" + git(repo, "rev-parse", "HEAD") in text
    with pytest.raises(records.CheckpointError, match="already"):
        records.begin("model_test_pipeline", candidate="adapter hash", root=repo)
    assert log.read_text() == text


def test_blind_requires_the_model_test_pipeline_done_record(repo: Path) -> None:
    with pytest.raises(records.CheckpointError, match="model_test"):
        records.begin("blind_pipeline", candidate="blind", root=repo)
    records.begin("model_test_pipeline", candidate="model_test_pipeline", root=repo)
    with pytest.raises(records.CheckpointError, match="model_test"):
        records.begin("blind_pipeline", candidate="blind", root=repo)
    result = repo / "model_test_pipeline.json"
    result.write_text("{}")
    records.finish("model_test_pipeline", result=result, root=repo)
    records.begin("blind_pipeline", candidate="blind", root=repo)
    done = json.loads((repo / "var/checkpoint/model_test_pipeline.done").read_text())
    assert done["sha256"] == hashlib.sha256(b"{}").hexdigest()


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_tracked_tree_refused(repo: Path, staged: bool) -> None:
    (repo / "tracked").write_text("dirty")
    if staged:
        git(repo, "add", "tracked")
    with pytest.raises(records.CheckpointError, match="dirty"):
        records.begin("model_test_pipeline", candidate="candidate", root=repo)
    assert not (repo / "var/checkpoint/model_test_pipeline.marker").exists()


def test_invalid_step_candidate_and_finish_without_marker(repo: Path) -> None:
    with pytest.raises(records.CheckpointError, match="unknown"):
        records.begin("other", candidate="x", root=repo)
    with pytest.raises(records.CheckpointError, match="unknown"):
        records.begin("model_test_gate_c", candidate="x", root=repo)
    with pytest.raises(records.CheckpointError, match="candidate"):
        records.begin("model_test_pipeline", candidate="x\ty", root=repo)
    with pytest.raises(records.CheckpointError, match="marker"):
        records.finish("model_test_pipeline", result=repo / "missing", root=repo)


def test_finish_is_exclusive_and_result_integrity_is_checked(repo: Path) -> None:
    records.begin("model_test_pipeline", candidate="x", root=repo)
    result = repo / "result.json"
    result.write_text("{}")
    records.finish("model_test_pipeline", result=result, root=repo)
    with pytest.raises(records.CheckpointError, match="already"):
        records.finish("model_test_pipeline", result=result, root=repo)
    result.write_text("changed")
    with pytest.raises(records.CheckpointError, match="result"):
        records.begin("blind_pipeline", candidate="x", root=repo)


def test_resume_requires_same_commit_candidate_and_logged_start(repo: Path) -> None:
    records.begin("model_test_pipeline", candidate="candidate", root=repo)
    records.resume("model_test_pipeline", candidate="candidate", root=repo)
    with pytest.raises(records.CheckpointError, match="candidate"):
        records.resume("model_test_pipeline", candidate="different", root=repo)
    git(repo, "commit", "--allow-empty", "-qm", "next")
    with pytest.raises(records.CheckpointError, match="commit"):
        records.resume("model_test_pipeline", candidate="candidate", root=repo)
