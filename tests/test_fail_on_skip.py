"""``FRA_FAIL_ON_SKIP=1`` turns a skipped test into a failed run (conftest.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import pytest

def test_passes():
    pass

@pytest.mark.skip(reason="no font")
def test_skipped():
    pass

@pytest.mark.xfail(reason="known")
def test_expected_failure():
    assert False
"""


def run_probe(tmp_path: Path, env_value: str | None, body: str = PROBE) -> tuple[int, str]:
    """Run a probe test file outside the repository with this repository's conftest loaded as a
    plugin, so the run sees the hook and nothing else of the suite."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(body)
    env = {k: v for k, v in os.environ.items() if k != "FRA_FAIL_ON_SKIP"}
    if env_value is not None:
        env["FRA_FAIL_ON_SKIP"] = env_value
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "-p",
            "conftest",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            str(probe),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def test_a_skip_fails_the_run_and_is_listed(tmp_path: Path) -> None:
    code, output = run_probe(tmp_path, "1")
    assert code == 1, output
    assert "1 test(s) skipped while FRA_FAIL_ON_SKIP=1" in output
    assert "test_probe.py::test_skipped" in output
    assert "test_probe.py::test_passes" not in output.split("FAILED:")[-1]
    assert "test_expected_failure" not in output.split("FAILED:")[-1]


@pytest.mark.parametrize("value", [None, "0", ""])
def test_a_skip_passes_the_run_unless_the_variable_is_one(
    tmp_path: Path, value: str | None
) -> None:
    code, output = run_probe(tmp_path, value)
    assert code == 0, output
    assert "FAILED:" not in output


def test_a_run_without_skips_passes_with_the_variable_set(tmp_path: Path) -> None:
    code, output = run_probe(tmp_path, "1", "def test_passes():\n    pass\n")
    assert code == 0, output


def test_a_module_skipped_while_collected_fails_the_run(tmp_path: Path) -> None:
    body = (
        "import pytest\n"
        'pytest.importorskip("a_module_nobody_installed")\n'
        "def test_never_collected():\n    pass\n"
    )
    code, output = run_probe(tmp_path, "1", body)
    assert code == 1, output
    assert "1 test(s) skipped while FRA_FAIL_ON_SKIP=1" in output
    assert "test_probe.py" in output.split("FAILED:")[-1]
