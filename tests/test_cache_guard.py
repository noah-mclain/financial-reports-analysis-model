"""The real var/artifacts cache is never written by the test run."""

from __future__ import annotations

import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from cache_isolation import changed_paths, fingerprint, structure_isolated

from fra_ingest.config import IngestConfig
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.pages import sha256_file


def blank_pdf(path: Path) -> Path:
    document = pdfium.PdfDocument.new()
    document.new_page(595, 842)
    document.save(path)
    document.close()
    return path


def test_guard_reports_a_file_written_under_var_artifacts(tmp_path: Path) -> None:
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "keep.json").write_text("{}")
    (tmp_path / "doc" / "gone.json").write_text("{}")
    (tmp_path / "doc" / "edit.json").write_text("{}")
    before = fingerprint(tmp_path)
    assert changed_paths(before, fingerprint(tmp_path)) == []

    (tmp_path / "doc" / "gone.json").unlink()
    (tmp_path / "doc" / "edit.json").write_text('{"a": 1}')
    (tmp_path / "doc" / "new.json").write_text("{}")

    assert changed_paths(before, fingerprint(tmp_path)) == [
        "changed doc/edit.json",
        "deleted doc/gone.json",
        "created doc/new.json",
    ]


def test_guard_sees_a_rewrite_of_identical_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}")
    before = fingerprint(tmp_path)
    (tmp_path / "a.json").write_text("{}")
    assert changed_paths(before, fingerprint(tmp_path)) == ["changed a.json"]


@pytest.fixture
def real_cache(tmp_path: Path) -> tuple[Path, IngestConfig, Path]:
    """A document and a stand-in for var/artifacts whose conversion was made for another
    device, as the Docker caches look to a native run."""
    pdf = blank_pdf(tmp_path / "doc.pdf")
    root = tmp_path / "var-artifacts"
    document = root / sha256_file(pdf)
    document.mkdir(parents=True)
    (document / "convert.json").write_text(json.dumps({"device": "mps"}))
    (document / "statements.raw.json").write_text("{}")
    return pdf, IngestConfig(artifact_root=root, device="cpu"), tmp_path / "scratch"


def test_golden_fixture_leaves_the_real_cache_byte_identical(
    real_cache: tuple[Path, IngestConfig, Path],
) -> None:
    pdf, config, scratch = real_cache
    before = fingerprint(config.artifact_root)
    scratch.mkdir()
    with pytest.raises(pytest.skip.Exception):
        structure_isolated(pdf, config, scratch)
    assert changed_paths(before, fingerprint(config.artifact_root)) == []
    assert (config.artifact_root / sha256_file(pdf) / "convert.json").is_file()


def test_golden_fixture_skips_by_name_when_the_conversion_is_stale(
    real_cache: tuple[Path, IngestConfig, Path],
) -> None:
    pdf, config, scratch = real_cache
    scratch.mkdir()
    with pytest.raises(pytest.skip.Exception) as skipped:
        structure_isolated(pdf, config, scratch)
    message = str(skipped.value)
    assert message.startswith("doc.pdf: cached conversion not current for ")
    assert "device=cpu" in message
    assert "engine=ocrmac" in message
    assert f"LOCATE_VERSION={LOCATE_VERSION}" in message
    assert "make eval-convert" in message


def test_golden_fixture_skips_by_name_when_there_is_no_conversion(tmp_path: Path) -> None:
    pdf = blank_pdf(tmp_path / "doc.pdf")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = IngestConfig(artifact_root=tmp_path / "empty")
    with pytest.raises(pytest.skip.Exception, match=r"doc\.pdf: no stored conversion for "):
        structure_isolated(pdf, config, scratch)
