"""The comparator that runs the eval harnesses and holds their outcomes to a record.

Every run goes through a fake ``--make``: a small script that writes the report and exits as the
plan for its target says, so no harness, model or document is involved.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "eval_outcomes.py"
RECORD = ROOT / "eval" / "golden" / "outcomes" / "docker-linux.toml"

FAKE_MAKE = """\
import json, os, signal, sys
from pathlib import Path

plan = json.loads(Path(os.environ["FAKE_PLAN"]).read_text())
target, *arguments = sys.argv[1:]
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps([target, *arguments]) + "\\n")
step = plan[target]
if step.get("report") is not None:
    path = Path(os.environ["FAKE_REPORTS"]) / step["report_name"]
    path.write_text(json.dumps(step["report"]))
if step.get("stderr"):
    print(step["stderr"], file=sys.stderr)
if step.get("signal"):
    os.kill(os.getpid(), step["signal"])
sys.exit(step.get("exit", 0))
"""


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_outcomes_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


outcomes = load_module()


def toml_run(name: str, **fields: Any) -> str:
    run = {
        "target": f"eval-{name}",
        "args": [],
        "report": f"{name}.json",
        "reasons_key": "reasons",
        "expected": [],
        "exit": 0,
        **fields,
    }
    lines = [f"[runs.{name}]"]
    lines += [f"{key} = {json.dumps(value)}" for key, value in run.items()]
    return "\n".join(lines) + "\n"


class Harness:
    """A record, a plan for the fake make, and the directories they use."""

    def __init__(self, tmp_path: Path) -> None:
        self.reports = tmp_path / "reports"
        self.reports.mkdir()
        self.record = tmp_path / "record.toml"
        self.plan_path = tmp_path / "plan.json"
        self.log = tmp_path / "log"
        self.fake = tmp_path / "fake_make.py"
        self.fake.write_text(FAKE_MAKE, encoding="utf-8")
        self.plan: dict[str, Any] = {}

    def step(self, name: str, report: Any = None, **fields: Any) -> None:
        self.plan[f"eval-{name}"] = {"report_name": f"{name}.json", "report": report, **fields}

    def execute(self, *extra: str) -> subprocess.CompletedProcess[str]:
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.record),
                "--make",
                f"{sys.executable} {self.fake}",
                "--reports",
                str(self.reports),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "FAKE_PLAN": str(self.plan_path),
                "FAKE_LOG": str(self.log),
                "FAKE_REPORTS": str(self.reports),
            },
        )

    def invocations(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_a_run_that_matches_its_record_passes(harness: Harness) -> None:
    harness.record.write_text(
        toml_run("alpha", expected=["doc-a: ocr_engine"], exit=1) + toml_run("beta"),
        encoding="utf-8",
    )
    harness.step("alpha", {"reasons": ["doc-a: ocr_engine"]}, exit=1)
    harness.step("beta", {"reasons": []}, exit=0)

    done = harness.execute()

    assert done.returncode == 0, done.stdout + done.stderr
    assert "eval-alpha: ok" in done.stdout
    assert "eval-beta: ok" in done.stdout


def test_the_runs_go_in_record_order_with_their_make_variables(harness: Harness) -> None:
    harness.record.write_text(
        toml_run("beta", args=["TARGET=golden", "LIMIT=2"]) + toml_run("alpha"),
        encoding="utf-8",
    )
    harness.step("alpha", {"reasons": []})
    harness.step("beta", {"reasons": []})

    harness.execute()

    assert harness.invocations() == [["eval-beta", "TARGET=golden", "LIMIT=2"], ["eval-alpha"]]


def test_run_limits_the_comparison_to_the_named_runs(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha") + toml_run("beta"), encoding="utf-8")
    harness.step("alpha", {"reasons": []})
    harness.step("beta", {"reasons": []})

    done = harness.execute("--run", "beta")

    assert done.returncode == 0, done.stderr
    assert [call[0] for call in harness.invocations()] == ["eval-beta"]


def test_an_unknown_run_name_is_refused_before_anything_runs(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha"), encoding="utf-8")
    harness.step("alpha", {"reasons": []})

    done = harness.execute("--run", "gamma")

    assert done.returncode == 2
    assert "gamma" in done.stderr
    assert "alpha" in done.stderr
    assert harness.invocations() == []


def test_a_crash_that_wrote_no_report_fails_even_when_an_old_report_is_there(
    harness: Harness,
) -> None:
    harness.record.write_text(toml_run("alpha"), encoding="utf-8")
    (harness.reports / "alpha.json").write_text(json.dumps({"reasons": []}), encoding="utf-8")
    harness.step("alpha", None, exit=1)

    done = harness.execute()

    assert done.returncode == 1
    assert "eval-alpha: FAILED" in done.stdout
    assert "no report" in done.stdout


def test_an_aborted_report_fails_and_says_why(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha", exit=1), encoding="utf-8")
    harness.step(
        "alpha", {"reasons": [], "aborted": "the first 3 documents failed with ocr_engine"}, exit=1
    )

    done = harness.execute()

    assert done.returncode == 1
    assert "aborted: the first 3 documents failed with ocr_engine" in done.stdout


def test_a_reason_the_record_does_not_hold_is_a_new_failure(harness: Harness) -> None:
    harness.record.write_text(
        toml_run("alpha", expected=["doc-a: ocr_engine"], exit=1), encoding="utf-8"
    )
    harness.step("alpha", {"reasons": ["doc-a: ocr_engine", "doc-b: ocr_timeout"]}, exit=1)

    done = harness.execute()

    assert done.returncode == 1
    assert "new failure: doc-b: ocr_timeout" in done.stdout
    assert "fixed" not in done.stdout


def test_a_recorded_reason_that_is_gone_says_to_update_the_record(harness: Harness) -> None:
    harness.record.write_text(
        toml_run("alpha", expected=["doc-a: ocr_engine", "doc-b: ocr_timeout"], exit=1),
        encoding="utf-8",
    )
    harness.step("alpha", {"reasons": ["doc-a: ocr_engine"]}, exit=1)

    done = harness.execute()

    assert done.returncode == 1
    assert "fixed, update the record: doc-b: ocr_timeout" in done.stdout


def test_a_reason_repeated_more_often_than_recorded_is_a_new_failure(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha", expected=["same"], exit=1), encoding="utf-8")
    harness.step("alpha", {"reasons": ["same", "same"]}, exit=1)

    done = harness.execute()

    assert "new failure: same" in done.stdout


def test_an_exit_code_other_than_the_recorded_one_fails(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha", expected=["x"], exit=1), encoding="utf-8")
    harness.step("alpha", {"reasons": ["x"]}, exit=0)

    done = harness.execute()

    assert done.returncode == 1
    assert "exit 0, expected 1" in done.stdout


def test_make_folds_a_recipe_failure_into_2_and_the_harness_code_is_read_from_its_message(
    harness: Harness,
) -> None:
    harness.record.write_text(toml_run("alpha", expected=["x"], exit=1), encoding="utf-8")
    harness.step(
        "alpha",
        {"reasons": ["x"]},
        exit=2,
        stderr="make: *** [Makefile:70: eval-alpha] Error 1",
    )

    done = harness.execute()

    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("stderr", "named"),
    [
        ("make: *** [Makefile:70: eval-alpha] Error 124", "timeout"),
        ("make: *** [Makefile:70: eval-alpha] Error 137", "SIGKILL"),
        ("make: *** [Makefile:70: eval-alpha] Error 143", "SIGTERM"),
        ("make: *** [Makefile:70: eval-alpha] Killed", "Killed"),
        ("make: *** [Makefile:70: eval-alpha] Terminated", "Terminated"),
    ],
)
def test_a_timeout_or_signal_seen_through_make_is_named(
    harness: Harness, stderr: str, named: str
) -> None:
    harness.record.write_text(toml_run("alpha"), encoding="utf-8")
    harness.step("alpha", None, exit=2, stderr=stderr)

    done = harness.execute()

    assert done.returncode == 1
    assert named in done.stdout
    assert "no report" in done.stdout


def test_a_harness_killed_by_a_signal_is_named(harness: Harness) -> None:
    import signal

    harness.record.write_text(toml_run("alpha"), encoding="utf-8")
    harness.step("alpha", None, signal=signal.SIGKILL)

    done = harness.execute()

    assert done.returncode == 1
    assert "killed by signal 9 (SIGKILL)" in done.stdout


def test_a_signal_exit_is_named_even_when_a_report_was_left(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha"), encoding="utf-8")
    harness.step("alpha", {"reasons": []}, exit=137)

    done = harness.execute()

    assert done.returncode == 1
    assert "exit 137 (SIGKILL" in done.stdout


def test_numbers_that_vary_between_runs_do_not_count_as_a_difference(harness: Harness) -> None:
    harness.record.write_text(
        toml_run("alpha", expected=["doc-a: peak N GB over N GB"], exit=1), encoding="utf-8"
    )
    harness.step("alpha", {"reasons": ["doc-a: peak 3.62 GB over 3.5 GB"]}, exit=1)

    done = harness.execute()

    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize(
    ("reason", "normalized"),
    [
        ("doc-a: peak 3.62 GB over 3.5 GB", "doc-a: peak N GB over N GB"),
        ("doc-a: peak 12 GB over 3.5 GB", "doc-a: peak N GB over N GB"),
        ("doc-a: peak unknown over 3.5 GB", "doc-a: peak unknown over N GB"),
        ("doc-a: ocr_timeout page 4 after 61.5 s", "doc-a: ocr_timeout page 4 after N s"),
        ("doc-a: ranges ok, failed", "doc-a: ranges ok, failed"),
        ("2023-annual-report: ocr_engine", "2023-annual-report: ocr_engine"),
    ],
)
def test_the_normalization_replaces_only_measured_quantities(reason: str, normalized: str) -> None:
    assert outcomes.normalize(reason) == normalized
    assert outcomes.normalize(normalized) == normalized


def test_a_path_inside_the_checkout_is_compared_relative_to_it() -> None:
    reason = f"doc-a: ocr_timeout {outcomes.ROOT}/eval/golden/documents/doc-a.pdf: after 120.0 s"
    assert outcomes.normalize(reason) == (
        "doc-a: ocr_timeout eval/golden/documents/doc-a.pdf: after N s"
    )
    elsewhere = "doc-a: ocr_timeout /srv/other/eval/golden/documents/doc-a.pdf"
    assert outcomes.normalize(elsewhere) == elsewhere


def test_a_report_without_the_reasons_key_fails_by_naming_the_key(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha", reasons_key="failures"), encoding="utf-8")
    harness.step("alpha", {"reasons": []})

    done = harness.execute()

    assert done.returncode == 1
    assert "no key 'failures'" in done.stdout


def test_every_failing_run_is_reported_not_only_the_first(harness: Harness) -> None:
    harness.record.write_text(toml_run("alpha") + toml_run("beta"), encoding="utf-8")
    harness.step("alpha", {"reasons": ["a"]})
    harness.step("beta", {"reasons": ["b"]})

    done = harness.execute()

    assert "new failure: a" in done.stdout
    assert "new failure: b" in done.stdout


def good() -> str:
    return toml_run("alpha")


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("this is not toml [", "not valid TOML"),
        ("", "no [runs"),
        ("[runs]\n", "no [runs"),
        (good().replace('target = "eval-alpha"\n', ""), "target"),
        (good().replace("args = []", 'args = "TARGET=golden"'), "args"),
        (good().replace("args = []", "args = [1]"), "args"),
        (good().replace('report = "alpha.json"', 'report = "/tmp/alpha.json"'), "report"),
        (good().replace('report = "alpha.json"', 'report = "../alpha.json"'), "report"),
        (good().replace('reasons_key = "reasons"\n', ""), "reasons_key"),
        (good().replace("expected = []", 'expected = "x"'), "expected"),
        (good().replace("expected = []", "expected = [1]"), "expected"),
        (good().replace("exit = 0", 'exit = "1"'), "exit"),
        (good() + 'colour = "red"\n', "colour"),
    ],
)
def test_a_malformed_record_is_refused_before_anything_runs(
    harness: Harness, text: str, complaint: str
) -> None:
    harness.record.write_text(text, encoding="utf-8")
    harness.step("alpha", {"reasons": []})

    done = harness.execute()

    assert done.returncode == 2
    assert complaint in done.stderr
    assert harness.invocations() == []


def test_a_missing_record_is_refused(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "none.toml")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 2
    assert "none.toml" in done.stderr


def test_the_shipped_record_loads_and_names_makefile_targets() -> None:
    runs = outcomes.load_record(RECORD)
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    phony = set(re.search(r"^\.PHONY:(.*)$", makefile, re.MULTILINE).group(1).split())  # type: ignore[union-attr]
    assert runs
    for run in runs.values():
        assert run.target in phony, f"{run.name}: {run.target} is not a Makefile target"


def test_the_runs_the_shipped_record_names_never_touch_a_protected_pool() -> None:
    runs = outcomes.load_record(RECORD)
    forbidden = ("dry-run", "model_test", "blind", "holdout", "train", "dev", "corpus")
    for run in runs.values():
        words = [run.target, *run.args]
        assert not [w for w in words if any(f in w for f in forbidden)], run.name
