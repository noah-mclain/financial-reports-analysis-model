"""Hermetic training tests on hosts without the Apple-only mlx wheel (the Linux CI image).

The trainers record `importlib.metadata.version` for the MLX packages. Where mlx is not
installed the tests that stub the heavy worker still need that lookup to succeed, so the
modules' `version` is replaced by a test double. Hosts that do have mlx are untouched.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

import pytest

STUB_VERSIONS = {"mlx": "0.32.2", "mlx-lm": "0.31.3"}
TRAINER_MODULES = ("normalization_train", "normalization_stability")


def mlx_installed() -> bool:
    try:
        version("mlx")
    except PackageNotFoundError:
        return False
    return True


@pytest.fixture(autouse=True)
def stub_mlx_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    if mlx_installed():
        return
    for name in TRAINER_MODULES:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "version"):
            monkeypatch.setattr(
                module,
                "version",
                lambda package, real=module.version: STUB_VERSIONS.get(package) or real(package),
            )
