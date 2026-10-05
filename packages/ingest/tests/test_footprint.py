"""The child's lifetime peak memory (spec 10, Peak memory; R26)."""

from __future__ import annotations

import os
import resource
import sys

import pytest
from support import run_python

import fra_ingest.footprint as footprint
from fra_ingest.footprint import peak_footprint_gb, peak_footprint_reading

MEASURE = "from fra_ingest.footprint import peak_footprint_gb\n{body}\nprint(peak_footprint_gb())"


def isolated_peak(body: str) -> float:
    # Linux can retain pytest's RSS at fork/exec as the child's startup high-water mark.
    # Exec a small stdlib launcher before spawning the process whose peak we measure.
    code = MEASURE.format(body=body)
    launcher = (
        f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {code!r}], check=True)"
    )
    return float(run_python(launcher).stdout)


def test_the_peak_is_a_plausible_figure() -> None:
    assert 0.005 < peak_footprint_gb() < 64


def test_the_peak_counts_memory_the_process_touched() -> None:
    idle = isolated_peak("")
    busy = isolated_peak('block = b"\\x01" * (500 * 1024**2)')
    assert busy - idle > 0.4


def test_the_measured_child_does_not_start_from_the_pytest_process() -> None:
    assert isolated_peak(f"import os\nassert os.getppid() != {os.getpid()}") > 0


@pytest.mark.parametrize(("peak_kib", "expected_gib"), [(500 * 1024, 0.48828125), (1024**2, 1)])
def test_linux_rss_is_converted_from_kib_to_gib(
    monkeypatch: pytest.MonkeyPatch, peak_kib: int, expected_gib: float
) -> None:
    def getrusage(who: int) -> resource.struct_rusage:
        assert who == resource.RUSAGE_SELF
        return resource.struct_rusage((0, 0, peak_kib) + (0,) * 13)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrusage", getrusage)
    assert peak_footprint_reading() == (expected_gib, False)
    assert peak_footprint_gb() == expected_gib


@pytest.mark.parametrize("used_fallback", [False, True])
def test_peak_footprint_gb_returns_the_reading_regardless_of_fallback(
    monkeypatch: pytest.MonkeyPatch, used_fallback: bool
) -> None:
    expected_gb = 0.125
    monkeypatch.setattr(footprint, "peak_footprint_reading", lambda: (expected_gb, used_fallback))
    assert peak_footprint_gb() == expected_gb


def test_the_live_reading_is_positive_and_reports_a_boolean_fallback_flag() -> None:
    gb, used_fallback = peak_footprint_reading()
    assert 0.005 < gb < 64
    assert isinstance(used_fallback, bool)
