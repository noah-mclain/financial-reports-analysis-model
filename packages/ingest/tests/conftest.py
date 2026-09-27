"""Fixtures for the ingest tests. Plain helpers live in support.py."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support import GOLDEN_DIR


@pytest.fixture
def golden() -> Callable[[str], Path]:
    def resolve(name: str) -> Path:
        path = GOLDEN_DIR / name
        if not path.exists():
            pytest.skip(f"golden document {name} is not present")
        return path

    return resolve
