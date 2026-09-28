"""Ingest settings, read from ``configs/ingest.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas import StatementType

REPO_ROOT = Path(__file__).resolve().parents[4]
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
    header_fraction: float = Field(default=0.35, gt=0.0, lt=1.0)
    ocr_dpi: int = Field(default=100, ge=36, le=300)
    ocr_languages: tuple[str, ...] = Field(default=("ar-SA", "en-US"), min_length=1)
    pad_pages: int = Field(default=1, ge=0)
    low_selectivity_share: float = Field(default=0.25, gt=0.0, le=1.0)
    artifact_root: Path = REPO_ROOT / "var" / "artifacts"

    @model_validator(mode="after")
    def _types_are_disjoint(self) -> IngestConfig:
        both = set(self.enabled_types) & set(self.optional_types)
        if both:
            msg = f"statement types both enabled and optional: {sorted(both)}"
            raise ValueError(msg)
        return self


def load_config(path: Path | None = None) -> IngestConfig:
    """Read settings, rejecting any key the model does not know."""
    source = path or DEFAULT_CONFIG_PATH
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

    if "artifact_root" in values:
        root = Path(values["artifact_root"])
        values["artifact_root"] = root if root.is_absolute() else REPO_ROOT / root
    return IngestConfig.model_validate(values)
