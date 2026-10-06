"""Every Makefile target that runs an eval harness runs it the way the harness imports work.

The harness modules import each other as ``harness.*``, which resolves only with ``eval`` on the
import path. pytest puts it there for the tests, so a target that runs a harness by file path
fails only when someone types ``make``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
RUNS_A_HARNESS = re.compile(r"eval/harness/|-m harness\.")
AS_MODULE = re.compile(r"^\tPYTHONPATH=eval \$\(UV\) run python -m harness\.(\w+)(?: |$)")


def harness_recipes() -> list[str]:
    return [
        line.rstrip("\n")
        for line in MAKEFILE.read_text(encoding="utf-8").splitlines()
        if RUNS_A_HARNESS.search(line)
    ]


def test_every_harness_target_runs_the_module_with_eval_on_the_path() -> None:
    recipes = harness_recipes()
    assert recipes, "no Makefile target runs an eval harness"
    wrong = [line.strip() for line in recipes if not AS_MODULE.match(line)]
    assert not wrong, f"run these as PYTHONPATH=eval ... python -m harness.<name>: {wrong}"


@pytest.mark.parametrize(
    "module", sorted({m.group(1) for line in harness_recipes() if (m := AS_MODULE.match(line))})
)
def test_each_harness_a_target_runs_starts_with_only_the_path_make_gives_it(module: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {"PYTHONPATH": "eval"}
    done = subprocess.run(
        [sys.executable, "-m", f"harness.{module}", "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
