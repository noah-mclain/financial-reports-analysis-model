"""Run the convert stage in a child process per document (ADR 0007, spec 10).

PyTorch on MPS returns its memory reliably only when the process exits, so each document is
converted by ``fra-ingest convert`` in its own process. A crash, an out-of-memory kill or a
hang ends only the child; this module reports how it ended.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast, get_args

from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError, IngestErrorReason
from fra_ingest.pages import sha256_file
from fra_ingest.results import ConvertResult

_REASONS: tuple[str, ...] = get_args(IngestErrorReason)
_EXIT_ERROR = 2
_EXIT_ALL_FAILED = 3
_TAIL_LINES = 5
_TAIL_CHARS = 500


def convert_in_child(
    pdf: Path,
    config: IngestConfig,
    *,
    config_path: Path | None = None,
    extra_args: Sequence[str] = (),
    command: Sequence[str] | None = None,
) -> ConvertResult:
    """Only ``artifact_root`` and ``child_timeout_s`` from ``config`` reach the child; every
    other setting comes from the TOML file the child loads (``config_path``, else
    ``FRA_INGEST_CONFIG``, else the repository default)."""
    if not pdf.is_file():
        raise IngestError("unreadable_pdf", f"{pdf}: not a file")
    sha256 = sha256_file(pdf)
    argv = [
        *(command or [sys.executable, "-m", "fra_ingest.cli"]),
        "convert",
        str(pdf),
        "--artifacts",
        str(config.artifact_root),
    ]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    argv += list(extra_args)
    # The child sees the same packages as this process: uv writes the workspace .pth files
    # hidden on macOS and Python skips hidden .pth files.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    started = time.perf_counter()
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=config.child_timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"{pdf.name}: still running after {config.child_timeout_s:.0f} s"
        raise IngestError("convert_timeout", detail) from exc
    wall = time.perf_counter() - started

    if done.returncode < 0:
        raise IngestError("convert_crashed", f"signal {-done.returncode}")
    if done.returncode == _EXIT_ERROR and (reason := _reason(done.stderr, pdf)) is not None:
        raise IngestError(reason, _tail(done.stderr))
    stored = config.artifact_root / sha256 / "convert.json"
    if done.returncode not in (0, _EXIT_ALL_FAILED) or not stored.is_file():
        raise IngestError("convert_crashed", _tail(done.stderr) or f"exit {done.returncode}")

    result = ConvertResult.model_validate_json(stored.read_text(encoding="utf-8"))
    if done.returncode == _EXIT_ALL_FAILED:
        failed = ", ".join(f"{r.first_page}-{r.last_page}" for r in result.ranges)
        raise IngestError("convert_failed", f"every range failed: {failed}")
    return result.model_copy(update={"timings": {**result.timings, "child_wall": wall}})


def _reason(stderr: str, pdf: Path) -> IngestErrorReason | None:
    """The reason the child printed as ``<pdf>: <reason> <detail>`` on its last line."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    word = lines[-1].removeprefix(f"{pdf}: ").split(" ", 1)[0]
    return cast(IngestErrorReason, word) if word in _REASONS else None


def _tail(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return " | ".join(lines[-_TAIL_LINES:])[-_TAIL_CHARS:]
