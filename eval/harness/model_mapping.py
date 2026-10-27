"""Evaluation composition root for the same optional mapping stage as queued jobs."""

from __future__ import annotations

import os
from pathlib import Path

from fra_core.normalization import load_normalization_settings
from fra_core.paths import REPO_ROOT
from fra_ingest.config import IngestConfig
from fra_ingest.ocr import OcrEngine
from fra_ingest.results import StructureResult
from fra_ingest.structure import structure_pdf
from fra_worker.model_mapping import map_structure


def evaluation_structure(
    pdf: Path, config: IngestConfig, ocr: OcrEngine | None, *, use_cache: bool = True
) -> StructureResult:
    result = structure_pdf(pdf, config, ocr, use_cache=use_cache)
    settings = load_normalization_settings(os.environ, REPO_ROOT)
    return map_structure(result, config.artifact_root, settings) if settings.enabled else result


def evaluation_checks(path: Path) -> Path:
    settings = load_normalization_settings(os.environ, REPO_ROOT)
    return path.parent / "normalization" / "checks.json" if settings.enabled else path
