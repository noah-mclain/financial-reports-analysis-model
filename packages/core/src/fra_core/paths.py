"""Where ``configs/`` and ``var/`` live, found once for every package and app."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ROOT_ENV_VAR = "FRA_ROOT"
_SOURCE_ROOT = Path(__file__).resolve().parents[4]


def find_repo_root(environ: Mapping[str, str], source_root: Path, cwd: Path) -> Path:
    """Where configs/ and var/ live: ``FRA_ROOT`` when set (an installed copy, as in the
    Docker image), else the source checkout when it holds the config, else the working
    directory."""
    if environ.get(ROOT_ENV_VAR):
        return Path(environ[ROOT_ENV_VAR])
    if (source_root / "configs" / "ingest.toml").is_file():
        return source_root
    return cwd


REPO_ROOT = find_repo_root(os.environ, _SOURCE_ROOT, Path.cwd())
CONFIG_DIR = REPO_ROOT / "configs"
ANALYTICS_POLICY = CONFIG_DIR / "analytics.toml"
JOB_SETTINGS = CONFIG_DIR / "jobs.toml"
