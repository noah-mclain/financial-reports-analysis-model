"""Reusing locate.json for the convert stage (spec 10, Data flow step 1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from support import make_blank_pdf

from fra_ingest import stage
from fra_ingest.config import IngestConfig
from fra_ingest.pages import sha256_file
from fra_ingest.results import LocateResult


def config(tmp_path: Path) -> IngestConfig:
    return IngestConfig(artifact_root=tmp_path / "artifacts")


def test_a_current_locate_json_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=2)
    first = stage.load_or_locate(pdf, config(tmp_path), None)

    def refuse(*_args: object, **_kwargs: object) -> LocateResult:
        raise AssertionError("locate ran again")

    monkeypatch.setattr(stage, "locate_pdf", refuse)
    assert stage.load_or_locate(pdf, config(tmp_path), None) == first


@pytest.mark.parametrize("content", ["{not json", '{"version": "0"}'])
def test_a_corrupt_or_old_locate_json_is_recomputed(tmp_path: Path, content: str) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=2)
    out = config(tmp_path).artifact_root / sha256_file(pdf)
    out.mkdir(parents=True)
    (out / "locate.json").write_text(content, encoding="utf-8")
    result = stage.load_or_locate(pdf, config(tmp_path), None)
    assert result.document.page_count == 2
    assert LocateResult.model_validate_json((out / "locate.json").read_text()) == result


def test_page_languages_come_from_the_page_cache(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=2)
    stage.load_or_locate(pdf, config(tmp_path), None)
    assert stage.page_ocr_languages(pdf, config(tmp_path), None) == {1: None, 2: None}
