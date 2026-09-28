"""The child's lifetime peak memory (spec 10, Peak memory; R26)."""

from __future__ import annotations

from support import run_python

from fra_ingest.footprint import peak_footprint_gb

MEASURE = "from fra_ingest.footprint import peak_footprint_gb\n{body}\nprint(peak_footprint_gb())"


def test_the_peak_is_a_plausible_figure() -> None:
    assert 0.005 < peak_footprint_gb() < 64


def test_the_peak_counts_memory_the_process_touched() -> None:
    idle = float(run_python(MEASURE.format(body="")).stdout)
    busy = float(run_python(MEASURE.format(body='block = b"\\x01" * (500 * 1024**2)')).stdout)
    assert busy - idle > 0.4
