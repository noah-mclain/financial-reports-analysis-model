"""Progress rendering for terminals and ordinary log streams."""

from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from progress import Progress


class FakeStream(StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 20, 31, 7, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_tty_updates_in_place_and_finish_ends_the_line() -> None:
    stream, clock = FakeStream(tty=True), Clock()
    progress = Progress("stage", total=10, unit="rows", stream=stream, clock=clock)
    progress.advance(4)
    clock.tick(1)
    progress.finish()
    output = stream.getvalue()
    assert "stage" in output and "40%" in output and "4/10 rows" in output
    assert "elapsed 00:00:01" in output
    assert all(line.count("elapsed") <= 1 for line in output.splitlines())
    assert "\r" in output and output.endswith("\n")


def test_log_output_has_start_throttle_and_finish_lines() -> None:
    stream, clock = FakeStream(tty=False), Clock()
    progress = Progress("stage", total=100, unit="rows", stream=stream, clock=clock)
    for _ in range(256):
        progress.advance()
    clock.tick(29)
    for _ in range(256):
        progress.advance()
    assert len(stream.getvalue().splitlines()) == 1
    clock.tick(1)
    for _ in range(256):
        progress.advance()
    progress.finish()
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("2026-10-18T20:31:37Z stage")
    assert "768/100 rows (100.0%)" in lines[1]
    assert "finish" in lines[2]
    assert "\r" not in stream.getvalue()


def test_slow_rows_can_check_time_without_waiting_for_256_rows() -> None:
    stream, clock = FakeStream(tty=False), Clock()
    progress = Progress("inference", total=2, stream=stream, clock=clock)
    clock.tick(31)
    progress.advance(check_clock=True)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    assert "1/2 rows (50.0%)" in lines[1]
    assert "eta" in lines[1]
    clock.tick(1)
    progress.advance(check_clock=True)
    assert len(stream.getvalue().splitlines()) == 2


def test_unknown_zero_and_over_total() -> None:
    stream, clock = FakeStream(tty=False), Clock()
    progress = Progress("unknown", unit="rows", stream=stream, clock=clock)
    progress.advance(12)
    progress.finish()
    assert "12 rows" in stream.getvalue()
    assert "%" not in stream.getvalue() and "eta" not in stream.getvalue()

    zero = FakeStream(tty=False)
    Progress("empty", total=0, unit="rows", stream=zero, clock=clock).finish()
    assert "0/0 rows (100.0%)" in zero.getvalue()

    over = FakeStream(tty=False)
    progress = Progress("over", total=2, unit="rows", stream=over, clock=clock)
    progress.advance(5)
    progress.finish()
    assert "5/2 rows (100.0%)" in over.getvalue()


def test_tty_exception_context_ends_bar_before_error() -> None:
    stream, clock = FakeStream(tty=True), Clock()
    try:
        with Progress("stage", total=1, unit="rows", stream=stream, clock=clock):
            raise ValueError("broken")
    except ValueError:
        stream.write("error: broken\n")
    assert stream.getvalue().endswith("error: broken\n")
    assert "\nerror: broken\n" in stream.getvalue()


def test_advance_checks_clock_at_most_every_256_calls() -> None:
    stream, clock = FakeStream(tty=False), Clock()
    calls = 0

    def counted_clock() -> datetime:
        nonlocal calls
        calls += 1
        return clock()

    progress = Progress("stage", total=1000, stream=stream, clock=counted_clock)
    calls_after_start = calls
    for _ in range(255):
        progress.advance()
    assert calls == calls_after_start
    progress.advance()
    assert calls == calls_after_start + 1
    progress.finish()


def test_a_failed_stage_is_logged_as_failed_not_finished() -> None:
    stream, clock = FakeStream(tty=False), Clock()
    with pytest.raises(RuntimeError), Progress("stage", stream=stream, clock=clock):
        raise RuntimeError("boom")
    lines = stream.getvalue().splitlines()
    assert "failed" in lines[-1] and "finished" not in lines[-1]


def test_tty_line_clears_leftovers_from_a_longer_render() -> None:
    stream, clock = FakeStream(tty=True), Clock()
    Progress("stage", total=10, stream=stream, clock=clock).advance(1)
    assert "\x1b[K" in stream.getvalue()
