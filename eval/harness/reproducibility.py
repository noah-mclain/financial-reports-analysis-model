"""Identity and effective settings for a report, without selecting extra documents."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any

from fra_analytics.metrics.registry import FORMULA_VERSION
from fra_analytics.policy import load_policy
from fra_core.taxonomy import load_taxonomy
from fra_core.taxonomy import loader as taxonomy_loader
from fra_ingest.config import REPO_ROOT, IngestConfig
from fra_ingest.convert import CONVERT_VERSION
from fra_ingest.converter import docling_version
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.pages import PAGES_STAGE_VERSION, sha256_file
from fra_ingest.structure import STRUCTURE_VERSION
from harness.paths import ANALYTICS_POLICY


def record_hash(value: object) -> str:
    """Hash supplied run inputs in a stable representation; never fetch extra examples."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _source_inventory(root: Path) -> dict[str, str]:
    """Hash the Python sources and canonical taxonomy that can change report results."""
    taxonomy_package = Path(str(resources.files(taxonomy_loader.__package__)))
    source_directories = [
        root / "packages/ingest/src",
        root / "packages/analytics/src",
        root / "eval/harness",
        taxonomy_package.parent,
    ]
    sources: dict[str, str] = {}
    for directory in source_directories:
        if not directory.is_dir():
            msg = f"source directory is missing: {directory}"
            raise FileNotFoundError(msg)
        python_files = sorted(directory.rglob("*.py"))
        if not python_files:
            msg = f"source directory contains no Python files: {directory}"
            raise ValueError(msg)
        for path in python_files:
            relative = path.relative_to(root)
            sources[str(relative)] = sha256_file(path)

    taxonomy_path = Path(str(taxonomy_package.joinpath(taxonomy_loader._RESOURCE)))
    if not taxonomy_path.is_file():
        msg = f"canonical taxonomy source is missing: {taxonomy_path}"
        raise FileNotFoundError(msg)
    sources[str(taxonomy_path.relative_to(root))] = sha256_file(taxonomy_path)
    return dict(sorted(sources.items()))


def report_evidence(
    config: IngestConfig,
    *,
    documents: Mapping[str, str],
    expected: Mapping[str, str],
    inputs: Mapping[str, str],
) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args], capture_output=True, check=True
        ).stdout

    # A revision alone cannot identify an uncommitted candidate. Hash its relevant sources,
    # including new modules, so two dirty candidates at the same HEAD remain distinguishable.
    sources = _source_inventory(REPO_ROOT)
    return {
        "ingest_config": config.model_dump(mode="json"),
        "analytics_policy": asdict(load_policy(ANALYTICS_POLICY)),
        "source_revision": git("rev-parse", "HEAD").decode().strip(),
        "source_dirty": bool(git("status", "--porcelain")),
        "source_sha256": record_hash(sources),
        "versions": {
            "pages": PAGES_STAGE_VERSION,
            "locate": LOCATE_VERSION,
            "convert": CONVERT_VERSION,
            "structure": STRUCTURE_VERSION,
            "formula": FORMULA_VERSION,
            "taxonomy": load_taxonomy().version,
            "docling": docling_version(),
        },
        "documents": dict(documents),
        "expected": dict(expected),
        "inputs": {"analytics_policy": sha256_file(ANALYTICS_POLICY), **inputs},
        "expected_note": (
            "Expected hashes identify inputs; they do not establish independent verification."
        ),
    }
