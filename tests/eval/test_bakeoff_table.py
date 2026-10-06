"""The bake-off table: what it refuses, and how one row reads."""

import json
from pathlib import Path
from typing import Any

import pytest
from harness.bakeoff_table import BEGIN, END, main, table
from harness.extraction import wilson_interval

PLATFORM = "macOS-27.2-arm64-arm-64bit"


def scanned_row(language: str, right: int = 98, cells: int = 128) -> dict[str, Any]:
    low, high = wilson_interval(right, cells)
    return {
        "status": "checked",
        "language": language,
        "cells": cells,
        "right": right,
        "accuracy": right / cells,
        "wilson_low": low,
        "wilson_high": high,
        "value_only_accuracy": 0.7734375,
        "statements": 3,
        "statements_found": 2,
        "read_seconds_per_call": 0.2229,
        "convert_seconds_per_page": None,
    }


def make_report(**changes: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "engine": "ocrmac",
        "label": "x",
        "report": [f"platform: {PLATFORM}"],
        "scanned": [scanned_row("ar"), scanned_row("en", 120, 126)],
    }
    return {**report, **changes}


def write_inputs(
    tmp_path: Path, report: dict[str, Any], text: str | None = None
) -> tuple[Path, Path]:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    document = tmp_path / "doc.md"
    document.write_text(text if text is not None else f"{BEGIN}\nold\n{END}\n", encoding="utf-8")
    return path, document


def test_a_missing_report_names_its_path(tmp_path: Path) -> None:
    _, document = write_inputs(tmp_path, make_report())
    with pytest.raises(FileNotFoundError, match=r"nowhere\.json"):
        main([], runs=[("run", tmp_path / "nowhere.json")], document=document)


def test_a_report_without_its_platform_line_is_refused(tmp_path: Path) -> None:
    path, document = write_inputs(tmp_path, make_report(report=["OCR engine: ocrmac"]))
    with pytest.raises(ValueError, match="no line starting 'platform: '"):
        main([], runs=[("run", path)], document=document)


def test_a_report_without_a_language_row_is_refused(tmp_path: Path) -> None:
    path, document = write_inputs(tmp_path, make_report(scanned=[scanned_row("ar")]))
    with pytest.raises(ValueError, match="ar and en"):
        main([], runs=[("run", path)], document=document)


@pytest.mark.parametrize("write", [False, True])
def test_a_document_without_the_markers_is_refused_with_or_without_write(
    tmp_path: Path, write: bool
) -> None:
    path, document = write_inputs(tmp_path, make_report(), text="no block here\n")
    with pytest.raises(ValueError, match="bakeoff"):
        main(["--write"] if write else [], runs=[("run", path)], document=document)
    assert document.read_text(encoding="utf-8") == "no block here\n"


def test_write_replaces_only_the_block(tmp_path: Path) -> None:
    path, document = write_inputs(
        tmp_path, make_report(), text=f"before\n{BEGIN}\nSTALE\n{END}\nafter\n"
    )
    assert main(["--write"], runs=[("run", path)], document=document) == 0
    text = document.read_text(encoding="utf-8")
    assert text.startswith(f"before\n{BEGIN}\n\n| Engine and setting")
    assert text.endswith(f"\n\n{END}\nafter\n")
    assert "STALE" not in text


def test_one_row_reads_as_its_report_says() -> None:
    ar, _ = table([("Vision", make_report())])[2:]
    assert ar == (
        "| Vision | ar | 98 of 128 | 76.6% | 68.5% to 83.1% | 77.3% "
        f"| 2 of 3 | 0.22 | n/a | `{PLATFORM}` |"
    )


def test_the_platform_comes_from_the_report_not_from_a_guess() -> None:
    linux = make_report(report=["platform: Linux-6.10.11-linuxkit-aarch64-with-glibc2.41"])
    ar, _ = table([("Run", linux)])[2:]
    assert ar.endswith("| `Linux-6.10.11-linuxkit-aarch64-with-glibc2.41` |")
