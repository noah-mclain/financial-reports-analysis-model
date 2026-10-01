"""Which convert result structure trusts, and what it writes (spec 11, Data flow steps 1, 11)."""

from __future__ import annotations

from pathlib import Path

import pytest
from support import located

from fra_core.schemas import PageMode
from fra_ingest import structure
from fra_ingest.config import IngestConfig
from fra_ingest.convert import CONVERT_VERSION, settings_hash
from fra_ingest.errors import IngestError
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, LocateResult, RangeConversion

SHA = "d" * 64
PDF = Path("doc.pdf")
RELEASE = "2.126.0"


@pytest.fixture(autouse=True)
def _no_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structure, "page_ocr_languages", lambda *_args: {})
    monkeypatch.setattr(structure, "docling_version", lambda: RELEASE)


def _located() -> LocateResult:
    return located([PageMode.TEXT] * 3, [(2, 3)], sha256=SHA)


def _current_hash(config: IngestConfig, where: LocateResult) -> str:
    return settings_hash(config, plan_ranges(where, {}, config), RELEASE, where.version)


def _result(digest: str) -> ConvertResult:
    return ConvertResult(
        version=CONVERT_VERSION,
        sha256=SHA,
        locate_version="2",
        docling_version=RELEASE,
        device="mps",
        settings_hash=digest,
        ranges=[
            RangeConversion(
                first_page=2,
                last_page=3,
                ocr="pdf_aware",
                ocr_language=None,
                docling_path="docling/p2-3.json",
                status="ok",
            )
        ],
    )


def _store(config: IngestConfig, result: ConvertResult, *, docling: bool = True) -> None:
    out = config.artifact_root / SHA
    (out / "docling").mkdir(parents=True, exist_ok=True)
    (out / "convert.json").write_text(result.model_dump_json(), encoding="utf-8")
    if docling:
        (out / "docling" / "p2-3.json").write_text("{}", encoding="utf-8")


class _Convert:
    def __init__(self, config: IngestConfig, *, writes: bool = True) -> None:
        self.calls = 0
        self.config = config
        self.writes = writes

    def __call__(self, _pdf: Path, config: IngestConfig) -> ConvertResult:
        self.calls += 1
        fresh = _result(_current_hash(config, _located()))
        _store(config, fresh, docling=self.writes)
        return fresh


def test_a_current_stored_convert_is_used(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    _store(config, _result(_current_hash(config, _located())))
    convert = _Convert(config)
    structure.current_convert(PDF, config, None, _located(), convert)
    assert convert.calls == 0


def test_a_stored_convert_with_other_settings_is_redone(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    _store(config, _result("stale"))
    convert = _Convert(config)
    result = structure.current_convert(PDF, config, None, _located(), convert)
    assert convert.calls == 1
    assert result.settings_hash == _current_hash(config, _located())


def test_a_stored_convert_missing_a_docling_file_is_redone(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    _store(config, _result(_current_hash(config, _located())), docling=False)
    convert = _Convert(config)
    structure.current_convert(PDF, config, None, _located(), convert)
    assert convert.calls == 1


def test_a_docling_file_still_missing_after_convert_is_a_convert_failure(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    convert = _Convert(config, writes=False)
    with pytest.raises(IngestError) as caught:
        structure.current_convert(PDF, config, None, _located(), convert)
    assert caught.value.reason == "convert_failed"
    assert "docling/p2-3.json" in caught.value.detail


def test_table_checks_are_written_before_the_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    empty = _result("h").model_copy(update={"ranges": []})
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args: _located())
    monkeypatch.setattr(structure, "current_convert", lambda *_args: empty)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kw: [])
    written: list[str] = []
    real = structure._write

    def record(path: Path, text: str) -> None:
        written.append(path.name)
        real(path, text)

    monkeypatch.setattr(structure, "_write", record)
    structure.structure_pdf(PDF, config, None, use_cache=False)
    assert written == ["table_checks.json", "statements.raw.json"]


def test_cached_statements_without_their_checks_are_structured_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    empty = _result("h").model_copy(update={"ranges": []})
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args: _located())
    monkeypatch.setattr(structure, "current_convert", lambda *_args: empty)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kw: [])
    first = structure.structure_pdf(PDF, config, None)
    checks = tmp_path / first.sha256 / "table_checks.json"
    checks.unlink()
    structure.structure_pdf(PDF, config, None)
    assert checks.is_file()
