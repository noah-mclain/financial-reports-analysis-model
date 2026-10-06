from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "docling_models.py"


def test_the_cache_key_names_both_installed_versions() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "key"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == (
        f"docling-hf-{version('docling')}-{version('docling-ibm-models')}"
    )


def test_an_unknown_command_is_refused() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "fetch"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 2
    assert "invalid choice" in done.stderr
