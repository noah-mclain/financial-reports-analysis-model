"""Progress lines: how the convert child tells its parent where it is (ADR 0007, spec 10).

The child writes one ``progress: <seconds>s <message>`` line to stderr at each stage boundary,
flushed, where ``<seconds>`` counts from the start of the convert command. When the parent kills a
child that outlived ``child_timeout_s`` it reads the last such line from the stderr the child
had written, so the timeout names the stage the child was in. This module is the one place
that knows the line format; stages report through a ``Progress`` callable and never print.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

PROGRESS_PREFIX = "progress: "
_LINE = re.compile(rf"^{re.escape(PROGRESS_PREFIX)}(\d+(?:\.\d+)?)s (.+)$")

Progress = Callable[[str], None]


@dataclass(frozen=True)
class ProgressLine:
    seconds: float
    message: str


class StderrProgress:
    """Writes progress lines to ``stream`` (stderr by default), timed from construction."""

    def __init__(
        self, stream: TextIO | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._clock = clock
        self._started = clock()

    def __call__(self, message: str) -> None:
        seconds = self._clock() - self._started
        # One write per line, so another thread's output cannot land between text and newline.
        self._stream.write(f"{PROGRESS_PREFIX}{seconds:.1f}s {message}\n")
        self._stream.flush()


def parse_progress_line(line: str) -> ProgressLine | None:
    match = _LINE.match(line.strip())
    return ProgressLine(float(match[1]), match[2]) if match else None


def last_progress(stderr: str) -> ProgressLine | None:
    for line in reversed(stderr.splitlines()):
        if (parsed := parse_progress_line(line)) is not None:
            return parsed
    return None


def without_progress(stderr: str) -> str:
    return "".join(
        line for line in stderr.splitlines(keepends=True) if parse_progress_line(line) is None
    )
