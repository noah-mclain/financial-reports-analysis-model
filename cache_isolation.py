"""Keeping tests off the real ``var/artifacts``: a fingerprint of the cache, and the structure
stage run over a copy of one document's cache. Used by the root ``conftest.py``."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from fra_ingest.config import IngestConfig, load_config
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.pages import sha256_file
from fra_ingest.results import ConvertResult, StructureResult
from fra_ingest.structure import structure_pdf

# The same settings the golden fixture copies from, so the guard watches that directory.
REAL_ARTIFACTS = load_config().artifact_root

Fingerprint = Mapping[str, tuple[int, int, str]]


def fingerprint(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every file under ``root`` as relative path to (size, mtime_ns, sha256 of json files)."""
    found: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.suffix == ".json" else ""
            found[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns, digest)
    return found


def changed_paths(before: Fingerprint, after: Fingerprint) -> list[str]:
    """Each created, deleted or changed path, labelled, in path order."""
    lines = []
    for path in sorted(before.keys() | after.keys()):
        if path not in after:
            lines.append(f"deleted {path}")
        elif path not in before:
            lines.append(f"created {path}")
        elif before[path] != after[path]:
            lines.append(f"changed {path}")
    return lines


@dataclass(frozen=True)
class GoldenStructure:
    """The structure result of one golden document and the directory its artifacts are in."""

    result: StructureResult
    out_dir: Path


def structure_isolated(pdf: Path, config: IngestConfig, scratch: Path) -> GoldenStructure:
    """Structure ``pdf`` over a copy of its cache under ``config.artifact_root``, writing only
    under ``scratch``. Skips by name when there is no stored conversion or it is not current."""
    sha = sha256_file(pdf)
    source = config.artifact_root / sha
    profile = os.environ.get("FRA_PROFILE", "native")
    setting = (
        f"FRA_PROFILE={profile}, device={config.device}, engine={config.convert_ocr}, "
        f"LOCATE_VERSION={LOCATE_VERSION}"
    )
    if not (source / "convert.json").is_file():
        pytest.skip(f"{pdf.name}: no stored conversion for {setting}; run make eval-convert")
    shutil.copytree(source, scratch / sha)

    def stale(_pdf: Path, _config: IngestConfig) -> ConvertResult:
        pytest.skip(
            f"{pdf.name}: cached conversion not current for {setting}; "
            "run make eval-convert under this profile"
        )

    isolated = config.model_copy(update={"artifact_root": scratch})
    result = structure_pdf(pdf, isolated, None, use_cache=False, convert=stale)
    return GoldenStructure(result, scratch / sha)
