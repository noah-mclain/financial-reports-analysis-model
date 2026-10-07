"""Run the eval harnesses and hold what they report to a record of known outcomes.

    eval_outcomes.py RECORD [--run NAME ...] [--make CMD] [--reports DIR]

RECORD is a TOML file with one ``[runs.<name>]`` table per run: the Makefile ``target`` and its
``args`` (make variable assignments), the ``report`` the harness writes under the reports
directory, the ``reasons_key`` of the list of failure reasons in that report, the ``expected``
reasons and the ``exit`` code the harness returns. A harness may fail by design on a known
document; the record says which, so the run passes while the failures are the known ones and
fails when one appears or disappears.

Each run deletes its report, runs ``make <target> <args>`` and fails when the exit code is not the
recorded one, when no report was written (a crash), when the report is marked ``aborted``, or when
its normalized reasons differ from the recorded ones: "new failure" for an extra reason, "fixed,
update the record" for a missing one. GNU make folds every recipe failure into its own exit status
2 and prints the recipe's status in ``*** [...] Error N``, so that is read from the error output.

Exit 0 when every run matches, 1 when one does not, 2 when the record or the arguments cannot be
used. Standard library only, like the other CI scripts.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORTS = ROOT / "var" / "eval"
RUN_FIELDS = {"target", "args", "report", "reasons_key", "expected", "exit"}
# What a number is measured in when it varies from run to run: peak memory in GB, time in
# seconds. A bare number (a page, a count) is part of the reason and stays.
MEASURED_NUMBER = re.compile(r"\d+(?:\.\d+)?(?= (?:GB|s)\b)")
# The checkout's own path, which differs between machines; the repository-relative part stays.
CHECKOUT_PREFIX = f"{ROOT}/"
# GNU make's report of a failed recipe: the recipe's own status, or the signal that ended it.
MAKE_ERROR = re.compile(r"^make(?:\[\d+\])?: \*\*\* \[[^\]]*\] (?:Error (\d+)|(\D.*))$")
EXIT_NAMES = {
    124: "timeout",
    137: "SIGKILL: out of memory or a hard timeout",
    143: "SIGTERM: a timeout or a cancelled job",
}
# Lines of the make error output kept to find its closing message.
STDERR_TAIL = 20


class RecordError(Exception):
    """The record or the arguments cannot be used; nothing was run."""


@dataclass(frozen=True)
class Run:
    name: str
    target: str
    args: tuple[str, ...]
    report: str
    reasons_key: str
    expected: tuple[str, ...]
    exit: int


def normalize(reason: str) -> str:
    """The reason with the numbers that vary between runs replaced by N and the checkout's path
    taken off the paths inside it."""
    return MEASURED_NUMBER.sub("N", reason.replace(CHECKOUT_PREFIX, ""))


def load_record(path: Path) -> dict[str, Run]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RecordError(f"cannot read {path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise RecordError(f"{path} is not valid TOML: {error}") from error
    tables = data.get("runs")
    if not isinstance(tables, dict) or not tables:
        raise RecordError(f"{path} has no [runs.<name>] table")
    return {name: _parse_run(path, name, table) for name, table in tables.items()}


def _parse_run(path: Path, name: str, table: Any) -> Run:
    where = f"{path} [runs.{name}]"
    if not isinstance(table, dict):
        raise RecordError(f"{where} must be a table")
    unknown = sorted(set(table) - RUN_FIELDS)
    if unknown:
        raise RecordError(f"{where}: unknown key {', '.join(unknown)}")
    missing = sorted(RUN_FIELDS - set(table))
    if missing:
        raise RecordError(f"{where}: missing {', '.join(missing)}")
    for key in ("target", "report", "reasons_key"):
        if not isinstance(table[key], str) or not table[key]:
            raise RecordError(f"{where}: {key} must be a non-empty string")
    for key in ("args", "expected"):
        value = table[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RecordError(f"{where}: {key} must be a list of strings")
    if isinstance(table["exit"], bool) or not isinstance(table["exit"], int):
        raise RecordError(f"{where}: exit must be an integer")
    report = Path(table["report"])
    if report.is_absolute() or ".." in report.parts:
        raise RecordError(f"{where}: report must be a path under the reports directory")
    return Run(
        name=name,
        target=table["target"],
        args=tuple(table["args"]),
        report=table["report"],
        reasons_key=table["reasons_key"],
        expected=tuple(table["expected"]),
        exit=table["exit"],
    )


def describe_exit(status: int | str) -> str:
    """An exit status for a message: a signal or a timeout is named."""
    if isinstance(status, str):
        return f"stopped by {status}"
    if status < 0:
        name = signal.Signals(-status).name
        return f"killed by signal {-status} ({name})"
    if status in EXIT_NAMES:
        return f"exit {status} ({EXIT_NAMES[status]})"
    return f"exit {status}"


def harness_status(returncode: int, stderr_tail: Sequence[str]) -> int | str:
    """The harness's own exit status: make reports 2 for any failed recipe and prints the
    recipe's status, or the name of the signal that ended it, on its closing error line."""
    if returncode != 2:
        return returncode
    for line in reversed(stderr_tail):
        found = MAKE_ERROR.match(line.strip())
        if found:
            return int(found.group(1)) if found.group(1) else found.group(2)
    return returncode


def run_make(command: Sequence[str], run: Run) -> tuple[int | str, float]:
    """Run the target with its output on the console; the status and the seconds it took."""
    started = time.monotonic()
    process = subprocess.Popen(
        [*command, run.target, *run.args],
        cwd=ROOT,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    tail: deque[str] = deque(maxlen=STDERR_TAIL)

    def forward() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            tail.append(line)

    reader = threading.Thread(target=forward)
    reader.start()
    returncode = process.wait()
    reader.join()
    return harness_status(returncode, tail), time.monotonic() - started


def compare_reasons(run: Run, found: Sequence[str]) -> list[str]:
    expected = Counter(normalize(reason) for reason in run.expected)
    seen = Counter(normalize(reason) for reason in found)
    problems = [f"new failure: {reason}" for reason in sorted((seen - expected).elements())]
    problems += [
        f"fixed, update the record: {reason}" for reason in sorted((expected - seen).elements())
    ]
    return problems


def check_run(command: Sequence[str], run: Run, reports: Path) -> tuple[list[str], str]:
    """The problems of one run (none when it matches the record) and its summary detail."""
    report_path = reports / run.report
    report_path.unlink(missing_ok=True)
    status, seconds = run_make(command, run)
    detail = f"{describe_exit(status)}, {seconds:.0f} s"
    if not report_path.is_file():
        return [f"crashed: no report written at {report_path} ({describe_exit(status)})"], detail
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [f"{report_path} is not valid JSON: {error}"], detail
    problems: list[str] = []
    if status != run.exit:
        problems.append(f"{describe_exit(status)}, expected {run.exit}")
    if "aborted" in report:
        return [*problems, f"aborted: {report['aborted']}"], detail
    reasons = report.get(run.reasons_key)
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        problems.append(f"report has no key {run.reasons_key!r} holding a list of reasons")
        return problems, detail
    problems += compare_reasons(run, reasons)
    return problems, f"{detail}, {len(reasons)} reason(s)"


def select(runs: dict[str, Run], names: Sequence[str]) -> list[Run]:
    unknown = [name for name in names if name not in runs]
    if unknown:
        raise RecordError(f"unknown run {', '.join(unknown)}; the record has {', '.join(runs)}")
    return [run for name, run in runs.items() if not names or name in names]


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("record", type=Path)
    parser.add_argument("--run", action="append", default=[], metavar="NAME")
    parser.add_argument("--make", default="make", help="the command that runs a target")
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exit_request:
        return 2 if exit_request.code else 0
    try:
        chosen = select(load_record(arguments.record), arguments.run)
    except RecordError as error:
        print(f"eval_outcomes: {error}", file=sys.stderr)
        return 2
    command = shlex.split(arguments.make)
    failed = 0
    for run in chosen:
        problems, detail = check_run(command, run, arguments.reports)
        if problems:
            failed += 1
            print(f"{run.target}: FAILED ({detail})")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"{run.target}: ok ({detail})")
    print(f"{len(chosen) - failed} of {len(chosen)} run(s) match the record")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
