"""The development documents of the train pool, read from the records: never the holdout."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from harness.development import development_set
from harness.holdout_records import LOG_HEADER

from fra_core.split import Part, SplitError, hashed_part, issuer_key


def issuer_in(part: Part) -> str:
    return next(
        n for n in (f"Issuer {k}" for k in range(300)) if hashed_part(issuer_key(n)) is part
    )


def write(tmp_path: Path, documents: list[dict[str, Any]]) -> tuple[Path, Path, Path]:
    candidates = tmp_path / "candidates.yaml"
    candidates.write_text(yaml.safe_dump({"documents": documents}), encoding="utf-8")
    moves = tmp_path / "moves.yaml"
    moves.write_text(yaml.safe_dump({"moves": []}), encoding="utf-8")
    log = tmp_path / "log.tsv"
    log.write_text(LOG_HEADER, encoding="utf-8")
    return candidates, moves, log


def documents() -> list[dict[str, Any]]:
    return [
        {"id": "fit-1", "issuer": issuer_in(Part.FIT), "pool": "train"},
        {"id": "val-1", "issuer": issuer_in(Part.VALIDATION), "pool": "train"},
        {"id": "hold-1", "issuer": issuer_in(Part.HOLDOUT), "pool": "train"},
        {"id": "mt-1", "issuer": "Some Model Test Issuer", "pool": "model_test"},
        {"id": "dev-1", "issuer": "Almarai Company", "pool": "dev"},
    ]


def test_fit_and_validation_come_back_and_the_holdout_and_other_pools_never_do(
    tmp_path: Path,
) -> None:
    paths = write(tmp_path, documents())
    got = development_set({Part.FIT, Part.VALIDATION}, *paths)
    assert [d["id"] for d in got] == ["fit-1", "val-1"]


def test_the_holdout_cannot_be_asked_for(tmp_path: Path) -> None:
    paths = write(tmp_path, documents())
    with pytest.raises(SplitError, match="holdout_for_scoring"):
        development_set({Part.HOLDOUT}, *paths)
