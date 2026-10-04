"""Ingest settings, read from ``configs/ingest.toml``."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas import StatementType

_SOURCE_ROOT = Path(__file__).resolve().parents[4]

# Names an OCR engine goes by in the settings; ``none`` leaves image pages unread.
OcrEngineName = Literal["ocrmac", "tesseract", "none"]
# Set to one of those names to override ``convert.ocr_engine`` for one run (the bake-off).
OCR_ENGINE_ENV = "FRA_OCR_ENGINE"


def find_repo_root(environ: Mapping[str, str], source_root: Path, cwd: Path) -> Path:
    """Where configs/ and var/ live: ``FRA_ROOT`` when set (an installed copy, as in the
    Docker image), else the source checkout when it holds the config, else the working
    directory."""
    if environ.get("FRA_ROOT"):
        return Path(environ["FRA_ROOT"])
    if (source_root / "configs" / "ingest.toml").is_file():
        return source_root
    return cwd


REPO_ROOT = find_repo_root(os.environ, _SOURCE_ROOT, Path.cwd())
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "ingest.toml"

# (section, key) in the TOML file, to the field it sets.
_TOML_FIELDS: dict[tuple[str, str], str] = {
    ("statements", "enabled"): "enabled_types",
    ("statements", "optional"): "optional_types",
    ("text", "min_text_chars"): "min_text_chars",
    ("text", "header_fraction"): "header_fraction",
    ("ocr", "dpi"): "ocr_dpi",
    ("ocr", "languages"): "ocr_languages",
    ("locate", "pad_pages"): "pad_pages",
    ("locate", "low_selectivity_share"): "low_selectivity_share",
    ("structure", "min_confidence"): "min_confidence",
    ("convert", "device"): "device",
    ("convert", "ocr_engine"): "convert_ocr",
    ("convert", "images_scale"): "images_scale",
    ("convert", "ocr_scale"): "ocr_scale",
    ("convert", "batch_size"): "batch_size",
    ("convert", "do_cell_matching"): "do_cell_matching",
    ("convert", "document_timeout_s"): "document_timeout_s",
    ("convert", "child_timeout_s"): "child_timeout_s",
    ("convert", "memory_budget_gb"): "memory_budget_gb",
    ("tesseract", "psm"): "tesseract_psm",
    ("tesseract", "arabic_language"): "tesseract_arabic_language",
    ("artifacts", "root"): "artifact_root",
}


class IngestConfig(BaseModel):
    """Every statement type is detected; only ``enabled_types`` are converted and gate recall."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled_types: tuple[StatementType, ...] = (
        StatementType.BALANCE,
        StatementType.INCOME,
        StatementType.COMPREHENSIVE_INCOME,
    )
    optional_types: tuple[StatementType, ...] = (StatementType.CASH_FLOW, StatementType.EQUITY)
    min_text_chars: int = Field(default=50, ge=0)
    min_confidence: float = Field(default=0.5, gt=0.0, le=1.0)
    header_fraction: float = Field(default=0.35, gt=0.0, lt=1.0)
    ocr_dpi: int = Field(default=100, ge=36, le=300)
    ocr_languages: tuple[str, ...] = Field(default=("ar-SA", "en-US"), min_length=1)
    pad_pages: int = Field(default=1, ge=0)
    low_selectivity_share: float = Field(default=0.25, gt=0.0, le=1.0)
    device: Literal["mps", "cpu", "auto"] = "mps"
    convert_ocr: OcrEngineName = "ocrmac"
    images_scale: float = Field(default=2.0, gt=0.0, le=4.0)
    ocr_scale: float = Field(default=3.0, gt=0.0, le=6.0)
    # Tesseract only. Page segmentation mode (3 is Tesseract's own default) and the language
    # string for an Arabic page: Arabic alone, or Arabic with English for the Latin in it. The
    # default is "ara+eng": Arabic alone read no English page at all (docs/blueprint/14).
    tesseract_psm: int = Field(default=3, ge=0, le=13)
    tesseract_arabic_language: Literal["ara", "ara+eng"] = "ara+eng"
    batch_size: int = Field(default=2, ge=1)
    do_cell_matching: bool = True
    document_timeout_s: float = Field(default=600.0, gt=0.0)
    child_timeout_s: float = Field(default=900.0, gt=0.0)
    memory_budget_gb: float = Field(default=3.5, gt=0.0)
    artifact_root: Path = REPO_ROOT / "var" / "artifacts"

    @model_validator(mode="after")
    def _types_are_disjoint(self) -> IngestConfig:
        both = set(self.enabled_types) & set(self.optional_types)
        if both:
            msg = f"statement types both enabled and optional: {sorted(both)}"
            raise ValueError(msg)
        return self


def load_config(path: Path | None = None, *, ocr_engine: str | None = None) -> IngestConfig:
    """Read settings, rejecting any key the model does not know.

    ``ocr_engine``, else ``FRA_OCR_ENGINE``, replaces ``convert.ocr_engine``, so one engine can
    be tried without editing the file.
    """
    named = os.environ.get("FRA_INGEST_CONFIG")
    source = path or (Path(named) if named else DEFAULT_CONFIG_PATH)
    with source.open("rb") as handle:
        data = tomllib.load(handle)

    values: dict[str, Any] = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            msg = f"{source}: [{section}] must be a table"
            raise ValueError(msg)
        for key, value in table.items():
            field_name = _TOML_FIELDS.get((section, key))
            if field_name is None:
                msg = f"{source}: unknown setting {section}.{key}"
                raise ValueError(msg)
            values[field_name] = value

    chosen = ocr_engine or os.environ.get(OCR_ENGINE_ENV)
    if chosen:
        values["convert_ocr"] = chosen
    if "artifact_root" in values:
        root = Path(values["artifact_root"])
        values["artifact_root"] = root if root.is_absolute() else REPO_ROOT / root
    return IngestConfig.model_validate(values)
