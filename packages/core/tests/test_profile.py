"""The run profile is a closed set read from FRA_PROFILE, with no default."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fra_core.profile import PROFILE_ENV_VAR, Profile, read_profile

CORE_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize(
    ("value", "expected"), [("native", Profile.NATIVE), ("docker", Profile.DOCKER)]
)
def test_known_profiles_are_read(value: str, expected: Profile) -> None:
    assert read_profile({PROFILE_ENV_VAR: value}) is expected


def test_unset_profile_fails_and_names_the_variable() -> None:
    with pytest.raises(ValueError, match=PROFILE_ENV_VAR):
        read_profile({})


@pytest.mark.parametrize("value", ["", "Docker", " native", "kubernetes"])
def test_anything_else_fails_and_shows_the_value(value: str) -> None:
    with pytest.raises(ValueError, match=PROFILE_ENV_VAR) as raised:
        read_profile({PROFILE_ENV_VAR: value})
    assert repr(value) in str(raised.value)
    assert "native" in str(raised.value)
    assert "docker" in str(raised.value)


def run_module(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """``python -m fra_core.profile``, the command the worker service runs."""
    return subprocess.run(
        [sys.executable, "-m", "fra_core.profile"],
        env={"PYTHONPATH": str(CORE_SRC), **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_running_the_module_prints_the_profile() -> None:
    result = run_module({PROFILE_ENV_VAR: "docker"})

    assert (result.returncode, result.stdout) == (0, "docker\n")


def test_running_the_module_without_a_profile_fails_and_names_the_variable() -> None:
    result = run_module({})

    assert result.returncode != 0
    assert PROFILE_ENV_VAR in result.stderr
