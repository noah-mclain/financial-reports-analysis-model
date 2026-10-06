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

from fra_ingest.config import EFFECTIVE_CONFIG_ENV, OCR_ENGINE_ENV, IngestConfig
from fra_ingest.convert import CONVERT_VERSION, settings_hash
from fra_ingest.converter import docling_version
from fra_ingest.errors import IngestError, IngestErrorReason
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.pages import sha256_file
from fra_ingest.progress import last_progress, without_progress
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
    """Transport the full validated effective config; the child does not reload settings.

    ``config_path`` is kept in the command for ordinary command fixtures, but the transported
    effective config is authoritative. The returned digest must match this parent's settings.
    """
    config = IngestConfig.model_validate(config.model_dump())
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
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        OCR_ENGINE_ENV: config.convert_ocr,
        EFFECTIVE_CONFIG_ENV: config.model_dump_json(),
    }

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
        detail = f"{pdf.name}: still running after {config.child_timeout_s:g} s; " + (
            _last_stage(exc.stderr)
        )
        raise IngestError("convert_timeout", detail) from exc
    wall = time.perf_counter() - started

    if done.returncode < 0:
        raise IngestError("convert_crashed", f"signal {-done.returncode}")
    if done.returncode == _EXIT_ERROR and (reason := _reason(without_progress(done.stderr), pdf)) is not None:
        raise IngestError(reason, _tail(done.stderr))
    stored = config.artifact_root / sha256 / "convert.json"
    if done.returncode not in (0, _EXIT_ALL_FAILED) or not stored.is_file():
        raise IngestError("convert_crashed", _tail(done.stderr) or f"exit {done.returncode}")

    result = ConvertResult.model_validate_json(stored.read_text(encoding="utf-8"))
    release = docling_version()
    if (
        result.sha256 != sha256
        or result.version != CONVERT_VERSION
        or result.docling_version != release
        or result.locate_version != LOCATE_VERSION
    ):
        raise IngestError("convert_crashed", "child result document/version mismatch")
    if result.settings_plans is None:
        raise IngestError(
            "convert_crashed", "child result has no settings plans for digest verification"
        )
    digest = settings_hash(config, result.settings_plans, release, LOCATE_VERSION)
    if result.settings_hash != digest or result.device != config.device:
        raise IngestError("convert_crashed", "child settings digest mismatch")
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


def _last_stage(partial_stderr: str | bytes | None) -> str:
    """Where the killed child was, from the stderr it had written. ``TimeoutExpired`` carries
    that as bytes even when the run decodes its output, and as None when nothing was read."""
    if isinstance(partial_stderr, bytes):
        partial_stderr = partial_stderr.decode("utf-8", errors="replace")
    last = last_progress(partial_stderr or "")
    if last is None:
        return "no progress reported"
    return f"last progress: {last.message} (at {last.seconds:.1f} s)"


def _tail(stderr: str) -> str:
    stderr = without_progress(stderr)
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return " | ".join(lines[-_TAIL_LINES:])[-_TAIL_CHARS:]
