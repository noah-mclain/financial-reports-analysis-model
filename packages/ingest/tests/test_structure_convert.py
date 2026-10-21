"""Which convert result structure trusts, and what it writes (spec 11, Data flow steps 1, 11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import FakeRunner, located, text_page
from test_structure import document

from fra_core.schemas import PageMode, StatementType
from fra_ingest import structure
from fra_ingest.config import IngestConfig
from fra_ingest.convert import CONVERT_VERSION, convert_pdf, settings_hash
from fra_ingest.errors import IngestError
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.pages import ocr_key
from fra_ingest.results import ConvertResult, LocateResult, PageScore, RangeConversion

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


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_structure_rebuilds_statements_and_checks_after_same_digest_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT] * 2, [(1, 2)], sha256=SHA)
    where.pages = [
        PageScore(
            page_no=n, type_scores={StatementType.BALANCE: 8}, title_types=[StatementType.BALANCE]
        )
        for n in (1, 2)
    ]
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args: where)
    monkeypatch.setattr(
        structure,
        "read_pages",
        lambda *_args, **_kw: [text_page("Statement of financial position")],
    )
    conversions: list[ConvertResult] = []

    def unsuccessful(pdf: Path, config: IngestConfig) -> ConvertResult:
        result = convert_pdf(
            pdf,
            where,
            {},
            config,
            runner_factory=lambda _config: FakeRunner({"1-2": status}),
            docling=RELEASE,
        )
        conversions.append(result)
        return result

    first = structure.structure_pdf(PDF, config, None, convert=unsuccessful)
    assert conversions[0].ranges[0].status == status
    assert first.statements == []
    out = tmp_path / SHA
    checks = out / "table_checks.json"
    statements = out / "statements.raw.json"
    assert json.loads(checks.read_text()) == []
    old_statements = statements.read_bytes()
    recovered_calls: list[Path] = []

    def recovered(pdf: Path, _config: IngestConfig) -> ConvertResult:
        # An injected converter has no dependency on convert_pdf's invalidation.
        recovered_calls.append(pdf)
        assert not statements.exists()
        assert not checks.exists()
        previous = conversions[0]
        fresh = previous.model_copy(
            update={
                "ranges": [
                    previous.ranges[0].model_copy(
                        update={"status": "ok", "docling_path": "docling/p1-2.json", "flags": []}
                    )
                ],
                "flags": [],
            }
        )
        (out / "docling/p1-2.json").write_text(document().model_dump_json(by_alias=True))
        (out / "convert.json").write_text(fresh.model_dump_json())
        return fresh

    result = structure.structure_pdf(PDF, config, None, convert=recovered)
    assert recovered_calls == [PDF]
    assert result.settings_hash == first.settings_hash
    assert len(result.statements) == 1
    assert statements.read_bytes() != old_statements
    assert json.loads(statements.read_text())["statements"]
    rebuilt_checks = json.loads(checks.read_text())
    assert rebuilt_checks
    assert {c["status"] for c in rebuilt_checks if c["kind"] == "balance_identity"} == {"pass"}

    before = (statements.read_bytes(), checks.read_bytes())
    assert structure.structure_pdf(PDF, config, None, convert=recovered) == result
    assert recovered_calls == [PDF]
    assert (statements.read_bytes(), checks.read_bytes()) == before


def test_a_conversion_given_is_read_as_it_is_and_never_converted_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT] * 2, [(1, 2)], sha256=SHA)
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args: where)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kw: [])
    partial = convert_pdf(
        PDF,
        where,
        {},
        config,
        runner_factory=lambda _config: FakeRunner({"1-2": "partial"}),
        docling=RELEASE,
    )

    def refuse(_pdf: Path, _config: IngestConfig) -> ConvertResult:
        raise AssertionError("converted a second time")

    result = structure.structure_pdf(PDF, config, None, convert=refuse, conversion=partial)
    assert "range_partially_converted:1-2" in result.flags
    other = partial.model_copy(update={"sha256": "c" * 64})
    with pytest.raises(ValueError, match="given for"):
        structure.structure_pdf(PDF, config, None, convert=refuse, conversion=other)


@pytest.mark.parametrize("failure", ["interrupt", "missing_docling"])
def test_injected_conversion_failure_removes_derived_artifacts_before_running(
    tmp_path: Path, failure: str
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    current = _result(_current_hash(config, _located()))
    unsuccessful = current.model_copy(
        update={"ranges": [r.model_copy(update={"status": "failed"}) for r in current.ranges]}
    )
    _store(config, unsuccessful, docling=False)
    out = tmp_path / SHA
    statements = out / "statements.raw.json"
    checks = out / "table_checks.json"
    statements.write_text("stale statements")
    checks.write_text("stale checks")

    def failing(_pdf: Path, _config: IngestConfig) -> ConvertResult:
        assert not statements.exists()
        assert not checks.exists()
        if failure == "interrupt":
            raise KeyboardInterrupt
        return _result(_current_hash(config, _located()))

    error = KeyboardInterrupt if failure == "interrupt" else IngestError
    with pytest.raises(error):
        structure.current_convert(PDF, config, None, _located(), failing)
    assert not statements.exists()
    assert not checks.exists()


def _recorded_structure_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[IngestConfig, ConvertResult]:
    from test_header_recovery import OBSERVED, inject_render

    observation = OBSERVED[1]
    config = IngestConfig(artifact_root=tmp_path)
    result = _result("recorded-conversion").model_copy(
        update={
            "ranges": [
                RangeConversion(
                    first_page=6,
                    last_page=6,
                    ocr="full_page",
                    ocr_language="ar-SA",
                    docling_path="docling/p6-6.json",
                    status="ok",
                )
            ]
        }
    )
    out = tmp_path / SHA
    (out / "docling").mkdir(parents=True)
    (out / "docling/p6-6.json").write_text(observation.document().model_dump_json(by_alias=True))
    where = located([PageMode.IMAGE] * 6, [(6, 6)], sha256=SHA, primary_type=StatementType.INCOME)
    where.pages = [
        PageScore(
            page_no=6, type_scores={StatementType.INCOME: 8}, title_types=[StatementType.INCOME]
        )
    ]
    page = text_page("قائمة الأرباح أو الخسائر المجمعة", page_no=6).model_copy(
        update={"mode": PageMode.IMAGE}
    )
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args: where)
    monkeypatch.setattr(structure, "current_convert", lambda *_args: result)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kw: [page])
    inject_render(monkeypatch, observation)
    return config, result


def test_recorded_scanned_headers_reach_statements_and_persist_boxes_on_cache_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_header_recovery import OBSERVED, RecordedEngine

    config, _converted = _recorded_structure_setup(tmp_path, monkeypatch)
    engine = RecordedEngine(OBSERVED[1])
    result = structure.structure_pdf(PDF, config, engine)
    assert engine.calls == [("ar-SA", "en-US")]
    assert len(result.statements) == 1
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["FY2023", "FY2024"]
    assert "header_recovered" in statement.flags
    assert [(c.text, c.col) for c in result.tables[0].recovered_headers] == [
        ("٢٠٢٣", 0),
        ("٢٠٢٤", 1),
    ]
    assert result.tables[0].recovery_context
    for item in statement.line_items:
        for cell in item.cells:
            original = OBSERVED[1].grid.cell(cell.provenance.row, cell.provenance.col)
            if original is None:
                assert cell.raw_text == ""
                assert "numbers_missing" in cell.flags and "bbox_synthesized" in cell.flags
                continue
            assert original is not None
            assert (cell.raw_text, cell.provenance.bbox) == (original.text, original.bbox)
            assert cell.provenance.page_no == 6
    stored = (tmp_path / SHA / "statements.raw.json").read_bytes()
    cached = structure.structure_pdf(PDF, config, engine)
    assert cached == result
    assert len(engine.calls) == 1
    assert (tmp_path / SHA / "statements.raw.json").read_bytes() == stored

    # An engine identity change invalidates structure even when injected conversion stays
    # byte-identical. A header-only resolution change never requires conversion again.
    engine.name = "another-configured-engine"
    structure.structure_pdf(PDF, config, engine)
    assert len(engine.calls) == 2
    config = config.model_copy(update={"header_ocr_scale": 5.0})
    structure.structure_pdf(PDF, config, engine)
    assert len(engine.calls) == 3
    config = config.model_copy(update={"ocr_languages": ("en-US", "ar-SA")})
    structure.structure_pdf(PDF, config, engine)
    assert engine.calls[-1] == ("en-US", "ar-SA")


def test_failed_header_read_is_persisted_but_not_reused_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_header_recovery import OBSERVED, RecordedEngine

    from fra_ingest.ocr import OcrEngineError

    config, _converted = _recorded_structure_setup(tmp_path, monkeypatch)
    engine = RecordedEngine(OBSERVED[1])
    real = engine.recognize

    def failure(*_args: object) -> list[object]:
        raise OcrEngineError("partial native read failed")

    monkeypatch.setattr(engine, "recognize", failure)
    first = structure.structure_pdf(PDF, config, engine)
    assert not first.statements
    assert any(
        e.startswith("header_recovery_failed:OcrEngineError") for e in first.tables[0].evidence
    )
    monkeypatch.setattr(engine, "recognize", real)
    fresh = structure.structure_pdf(PDF, config, engine)
    assert len(fresh.statements) == 1
    assert fresh.settings_hash == first.settings_hash
    assert len(engine.calls) == 1


@pytest.mark.parametrize("old_version", ["17", "24", "25", "52"])
def test_old_structure_version_and_header_resolution_do_not_reuse_old_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_version: str
) -> None:
    from test_header_recovery import OBSERVED, RecordedEngine

    config, converted = _recorded_structure_setup(tmp_path, monkeypatch)
    engine = RecordedEngine(OBSERVED[1])
    first = structure.structure_pdf(PDF, config, engine)
    path = tmp_path / SHA / "statements.raw.json"
    path.write_text(first.model_copy(update={"version": old_version}).model_dump_json())
    structure.structure_pdf(PDF, config, engine)
    assert len(engine.calls) == 2
    assert old_version != structure.STRUCTURE_VERSION
    changed = config.model_copy(update={"header_ocr_scale": 4.0})
    assert _current_hash(changed, _located()) == _current_hash(config, _located())
    assert structure._settings_hash(
        changed, converted, _located(), engine
    ) != structure._settings_hash(config, converted, _located(), engine)


def test_source_payload_changes_only_structure_and_is_canonical() -> None:
    from fra_ingest.results import StatementRange

    where = _located()
    config = IngestConfig()
    converted = _result(_current_hash(config, where))
    original = structure._settings_hash(config, converted, where)
    reordered = where.model_copy(
        update={"pages": list(reversed(where.pages)), "ranges": list(reversed(where.ranges))}
    )
    assert structure._settings_hash(config, converted, reordered) == original
    updates: list[dict[str, object]] = [
        {
            "ranges": [
                StatementRange(
                    type=StatementType.BALANCE, first_page=2, last_page=2, rank=1, score=8
                )
            ]
        },
        {"ranges": [where.ranges[0].model_copy(update={"rank": 2})]},
        {"pages": [where.pages[0].model_copy(update={"negatives": ["notes"]}), *where.pages[1:]]},
    ]
    for update in updates:
        changed = where.model_copy(update=update)
        assert structure._settings_hash(config, converted, changed) != original
        assert _current_hash(config, changed) == converted.settings_hash


def test_source_change_rebuilds_structure_without_relocating_or_converting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fra_ingest import stage

    where = located([PageMode.TEXT] * 2, [(1, 2)], sha256=SHA)
    config = IngestConfig(artifact_root=tmp_path)
    where.ocr_key = ocr_key(config, None)
    converted = _result(_current_hash(config, where)).model_copy(update={"ranges": []})
    _store(config, converted)
    pdf = tmp_path / "cached.pdf"
    pdf.write_bytes(b"saved upstream stub")
    monkeypatch.setattr(stage, "sha256_file", lambda *_: SHA)
    stored_locate = tmp_path / SHA / "locate.json"
    stored_locate.write_text(where.model_dump_json())
    locate_calls: list[Path] = []
    convert_calls: list[Path] = []

    def locate_again(path: Path, *_args: object, **_kwargs: object) -> LocateResult:
        locate_calls.append(path)
        raise AssertionError("cached locate must be reused")

    def convert_again(path: Path, _config: IngestConfig) -> ConvertResult:
        convert_calls.append(path)
        raise AssertionError("cached conversion must be reused")

    monkeypatch.setattr(stage, "locate_pdf", locate_again)
    reads: list[int] = []

    def read(*_args: object, **_kwargs: object) -> list[object]:
        reads.append(1)
        return []

    monkeypatch.setattr(structure, "read_pages", read)
    first = structure.structure_pdf(pdf, config, None, convert=convert_again)
    assert structure.structure_pdf(pdf, config, None, convert=convert_again) == first
    assert len(reads) == 1
    where.ranges = [where.ranges[0].model_copy(update={"last_page": 1})]
    stored_locate.write_text(where.model_dump_json())
    fresh = structure.structure_pdf(pdf, config, None, convert=convert_again)
    assert fresh.settings_hash != first.settings_hash and len(reads) == 2
    assert _current_hash(config, where) == converted.settings_hash
    assert locate_calls == [] and convert_calls == []


def test_no_located_ranges_records_unextracted_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = located([PageMode.TEXT] * 2, [], sha256=SHA, statement_ranges=[])
    config = IngestConfig(artifact_root=tmp_path)
    converted = _result("no-ranges").model_copy(update={"ranges": []})
    monkeypatch.setattr(structure, "load_or_locate", lambda *_: where)
    monkeypatch.setattr(structure, "current_convert", lambda *_: converted)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kwargs: [])
    # A saved upstream grid can still exist even when this locate run found no range.
    converted.ranges = [
        converted_range.model_copy(update={"docling_path": "docling/p1-2.json"})
        for converted_range in _result("no-ranges").ranges
    ]
    monkeypatch.setattr(structure, "load_docling_json", lambda *_: document())
    result = structure.structure_pdf(PDF, config, None)
    assert not result.statements
    assert all(f"statement_not_extracted:{t.value}" in result.flags for t in config.enabled_types)
    assert result.tables
    assert all(t.type is None and t.statement_id is None for t in result.tables)
    assert all("source_outside_range" in t.evidence for t in result.tables)
