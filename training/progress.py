"""Small stderr progress reporter for long running preparation stages."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TextIO


class Progress:
    """Report a stage as an in-place terminal bar or timestamped log lines."""

    def __init__(
        self,
        stage: str,
        total: int | None = None,
        unit: str = "rows",
        stream: TextIO | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.stage = stage
        self.total = total
        self.unit = unit
        self.stream = sys.stderr if stream is None else stream
        self._clock = clock
        self._started = self._now()
        self._last_render = self._started
        self._count = 0
        self._clock_countdown = 256
        self._tty = self.stream.isatty()
        self._finished = False
        if self._tty:
            self._render(self._started)
        else:
            self._write(f"{_timestamp(self._started)} {stage} started")

    def advance(self, n: int = 1, *, check_clock: bool = False) -> None:
        """Add work; slow operations can check time each call without bypassing throttling."""
        if self._finished:
            return
        self._count += n
        if n < 256 and not check_clock:
            self._clock_countdown -= 1
            if self._clock_countdown > 0:
                return
        self._clock_countdown = 256
        now = self._now()
        interval = 0.25 if self._tty else 30.0
        if (now - self._last_render).total_seconds() >= interval:
            self._render(now)
            self._last_render = now

    def finish(self, *, failed: bool = False) -> None:
        """Write the final count and elapsed time exactly once; ``failed`` ends it as such."""
        if self._finished:
            return
        now = self._now()
        if self._tty:
            self._render(now, newline=True)
        else:
            detail = self._details(now)
            outcome = "failed" if failed else "finished"
            self._write(f"{_timestamp(now)} {self.stage} {outcome}: {detail}")
        self._finished = True

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.finish(failed=exc_type is not None)

    def _now(self) -> datetime:
        return self._clock()

    def _write(self, line: str) -> None:
        self.stream.write(line + "\n")
        self.stream.flush()

    def _details(self, now: datetime) -> str:
        elapsed_seconds = max((now - self._started).total_seconds(), 0.0)
        rate = _rate(self._count, elapsed_seconds)
        if self.total is None:
            return f"{self._count:,} {self.unit}  {rate}  elapsed {_elapsed(elapsed_seconds)}"
        percent = 100.0 if self.total == 0 else min(self._count / self.total * 100, 100.0)
        eta = "00:00:00"
        if self.total > self._count and self._count > 0:
            eta = _elapsed((self.total - self._count) / (self._count / max(elapsed_seconds, 0.001)))
        return (
            f"{self._count:,}/{self.total:,} {self.unit} ({percent:.1f}%)  {rate}  "
            f"elapsed {_elapsed(elapsed_seconds)}  eta {eta}"
        )

    def _render(self, now: datetime, *, newline: bool = False) -> None:
        detail = self._details(now)
        if self._tty:
            if self.total is None:
                suffix = detail
                bar = ""
            else:
                ratio = 1.0 if self.total == 0 else min(self._count / self.total, 1.0)
                width = 20
                filled = round(width * ratio)
                percent = 100.0 if self.total == 0 else min(ratio * 100, 100.0)
                bar = f"[{('#' * filled) + ('-' * (width - filled))}]  {percent:.0f}%  "
                suffix = detail
            line = f"{self.stage}  {bar}{suffix}"
            width = shutil.get_terminal_size((80, 24)).columns
            line = line[: max(width, 1)]
            self.stream.write("\r" + line + "\x1b[K" + ("\n" if newline else ""))
            self.stream.flush()
        else:
            self._write(f"{_timestamp(now)} {self.stage}  {detail}")


def _elapsed(seconds: float) -> str:
    value = max(int(seconds), 0)
    hours, remainder = divmod(value, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _rate(count: int, seconds: float) -> str:
    rate = count / max(seconds, 0.001)
    return f"{rate / 1000:.1f}k/s" if rate >= 1000 else f"{rate:.1f}/s"
