"""The progress lines the convert child writes and its parent reads (ADR 0007, spec 10)."""

from __future__ import annotations

import io

from fra_ingest.progress import (
    ProgressLine,
    StderrProgress,
    last_progress,
    parse_progress_line,
    without_progress,
)


def test_a_line_is_written_with_the_seconds_since_the_reporter_started() -> None:
    clock = iter([100.0, 161.94])
    stream = io.StringIO()
    report = StderrProgress(stream, clock=lambda: next(clock))
    report("convert pp. 58-60 start")
    assert stream.getvalue() == "progress: 61.9s convert pp. 58-60 start\n"


def test_a_written_line_parses_back() -> None:
    assert parse_progress_line("progress: 53.5s locate done") == ProgressLine(
        seconds=53.5, message="locate done"
    )


def test_other_lines_are_not_progress() -> None:
    assert parse_progress_line("doc.pdf: ocr_timeout tesseract did not finish") is None
    assert parse_progress_line("progress: soon") is None
    assert parse_progress_line("") is None


def test_the_last_progress_line_wins_and_noise_is_ignored() -> None:
    text = (
        "warning: slow\nprogress: 1.0s locate start\nprogress: 2.5s locate done\n"
        "some traceback\n"
    )
    assert last_progress(text) == ProgressLine(seconds=2.5, message="locate done")
    assert last_progress("nothing here\n") is None
    assert last_progress("") is None


def test_progress_lines_are_filtered_out_of_other_text() -> None:
    text = "progress: 1.0s locate start\nboom\nprogress: 2.0s locate done\n"
    assert without_progress(text) == "boom\n"
