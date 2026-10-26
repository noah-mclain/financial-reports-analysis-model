"""Append-only records of the one-time owner-run checkpoint (D13 and D17)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from fra_ingest.config import REPO_ROOT

STEPS = ("model_test_gate_c", "model_test_pipeline", "blind_pipeline")
LOG_HEADER = (
    "# Checkpoint log (D13, D17; docs/blueprint/08-revised-plan.md).\n"
    "# Append-only: one row per step, written before any scoring starts.\n"
    "# Never edit or delete a row. A marker spends the step even if the run fails.\n"
    "# candidate: caller-supplied artifact identity; commit: clean tracked repository HEAD.\n"
    "date\tstep\tcandidate\tcommit\n"
)


class CheckpointError(Exception):
    """A checkpoint would violate its one-time or reproducibility boundary."""


def commit(root: Path) -> str:
    """Return HEAD only for a clean tracked tree; untracked runtime records are allowed."""

    def git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
            ).strip()
        except subprocess.CalledProcessError as exc:
            raise CheckpointError(f"{root}: git {args[0]} failed") from exc

    if git("status", "--porcelain", "--untracked-files=no"):
        raise CheckpointError(f"{root}: dirty tracked tree; checkpoint refused")
    return git("rev-parse", "HEAD")


def _path(root: Path, step: str, suffix: str) -> Path:
    if step not in STEPS:
        raise CheckpointError(f"unknown checkpoint step {step!r}")
    return root / "var/checkpoint" / f"{step}.{suffix}"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"{path}: missing or damaged checkpoint record") from exc
    if not isinstance(value, dict):
        raise CheckpointError(f"{path}: checkpoint record must be an object")
    return value


def _exclusive(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise CheckpointError(f"{path}: checkpoint already started or finished") from exc


def _prerequisites(root: Path, step: str) -> None:
    if step != "blind_pipeline":
        return
    for required in STEPS[:2]:
        marker = _read(_path(root, required, "marker"))
        done = _read(_path(root, required, "done"))
        if done.get("candidate") != marker.get("candidate") or done.get("commit") != marker.get(
            "commit"
        ):
            raise CheckpointError(f"{required}: done record differs from marker")
        try:
            digest = hashlib.sha256(Path(done["result"]).read_bytes()).hexdigest()
        except (OSError, KeyError, TypeError) as exc:
            raise CheckpointError(f"{required}: missing result") from exc
        if digest != done.get("sha256"):
            raise CheckpointError(f"{required}: result sha256 mismatch")


def _log_row(step: str, candidate: str, revision: str) -> str:
    return "\t".join((date.today().isoformat(), step, candidate, revision)) + "\n"


def begin(step: str, *, candidate: str, root: Path = REPO_ROOT) -> None:
    """Spend a step exclusively, then append its log row before returning to the caller."""
    marker = _path(root, step, "marker")
    if not candidate or any(c in candidate for c in "\t\r\n"):
        raise CheckpointError("candidate must be nonempty single-line text without tabs")
    revision = commit(root)
    _prerequisites(root, step)
    _exclusive(marker, {"candidate": candidate, "commit": revision})
    log = root / "eval/corpus/checkpoint_log.tsv"
    log.parent.mkdir(parents=True, exist_ok=True)
    # Lock the shared log across distinct steps. A failed append retains the marker: fail closed.
    with log.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        text = stream.read()
        if text and (not text.startswith(LOG_HEADER) or not text.endswith("\n")):
            raise CheckpointError(f"{log}: damaged checkpoint log")
        if not text:
            stream.write(LOG_HEADER)
        stream.write(_log_row(step, candidate, revision))
        stream.flush()


def resume(step: str, *, candidate: str, root: Path = REPO_ROOT) -> None:
    """Continue a spent step, never create a second look or alter its log row."""
    marker = _read(_path(root, step, "marker"))
    revision = commit(root)
    if marker.get("commit") != revision:
        raise CheckpointError(f"{step}: commit changed since checkpoint start")
    if marker.get("candidate") != candidate:
        raise CheckpointError(f"{step}: candidate changed since checkpoint start")
    if _path(root, step, "done").exists():
        raise CheckpointError(f"{step}: checkpoint already finished")
    _prerequisites(root, step)
    log = root / "eval/corpus/checkpoint_log.tsv"
    try:
        text = log.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckpointError(f"{log}: missing checkpoint log") from exc
    matching = [line.split("\t")[1:] for line in text[len(LOG_HEADER) :].splitlines()]
    if not text.startswith(LOG_HEADER) or matching.count([step, candidate, revision]) != 1:
        raise CheckpointError(f"{log}: missing or duplicated checkpoint start row")


def finish(step: str, *, result: Path, root: Path = REPO_ROOT) -> None:
    """Seal a step with its result path and digest, without changing a marker or log row."""
    marker = _read(_path(root, step, "marker"))
    try:
        digest = hashlib.sha256(result.read_bytes()).hexdigest()
    except OSError as exc:
        raise CheckpointError(f"{result}: checkpoint result missing") from exc
    _exclusive(
        _path(root, step, "done"),
        {
            **marker,
            "result": str(result.resolve()),
            "sha256": digest,
        },
    )
