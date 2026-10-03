"""``fra_api.main`` builds the served app from FRA_PROFILE, and refuses to start without it."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import fra_api
from fra_api.app import HEALTH_PATH
from fra_core.profile import PROFILE_ENV_VAR, Profile


def forget_main() -> None:
    sys.modules.pop("fra_api.main", None)
    if hasattr(fra_api, "main"):
        del fra_api.main


@contextmanager
def fresh_main_module() -> Iterator[None]:
    """Import main anew inside the block, and leave no module behind afterwards."""
    forget_main()
    try:
        yield
    finally:
        forget_main()


@pytest.fixture(autouse=True)
def fresh_main() -> Iterator[None]:
    with fresh_main_module():
        yield


def test_nothing_of_main_is_left_after_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROFILE_ENV_VAR, Profile.NATIVE.value)

    with fresh_main_module():
        importlib.import_module("fra_api.main")
        assert "fra_api.main" in sys.modules

    assert "fra_api.main" not in sys.modules
    assert not hasattr(fra_api, "main")


@pytest.mark.parametrize("profile", list(Profile))
def test_main_serves_the_profile_in_the_environment(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROFILE_ENV_VAR, profile.value)

    main = importlib.import_module("fra_api.main")

    assert TestClient(main.app).get(HEALTH_PATH).json()["profile"] == profile.value


def test_main_fails_to_import_when_the_profile_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)

    with pytest.raises(ValueError, match=PROFILE_ENV_VAR):
        importlib.import_module("fra_api.main")
