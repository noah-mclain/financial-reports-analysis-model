"""Pre-repair successful artifacts must not bypass the current OCR failure contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest
from support import FakeOcr, FakeRunner, located, make_blank_pdf, text_page

from fra_core.schemas import PageMode, TextSource
from fra_ingest import cli, convert, pages, stage, structure
from fra_ingest.config import IngestConfig
from fra_ingest.ocr import OcrLine
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, LocateResult, RangePlan, StructureResult

RELEASE = "2.126.0"
SHA = "e" * 64
TITLE = OcrLine("Statement of financial position", 0.9, 0.1, 0.05, 0.8, 0.04)


def _legacy_page_settings(config: IngestConfig, engine: FakeOcr | None) -> dict[str, object]:
    return {
        "min_text_chars": config.min_text_chars,
        "header_fraction": config.header_fraction,
        "ocr_dpi": config.ocr_dpi,
        "ocr_languages": list(config.ocr_languages),
        "ocr_engine": engine.name if engine is not None else None,
        "ocr_options": (
            {"psm": config.tesseract_psm, "arabic_language": config.tesseract_arabic_language}
            if engine is not None and engine.name == "tesseract"
            else None
        ),
    }


def _legacy_convert_hash(config: IngestConfig, where: LocateResult) -> str:
    # The v1 contract, deliberately independent of today's digest implementation.
    payload = {
        "device": config.device,
        "ocr_engine": config.convert_ocr,
        "images_scale": config.images_scale,
        "ocr_scale": config.ocr_scale,
        "tesseract_psm": config.tesseract_psm,
        "tesseract_arabic_language": config.tesseract_arabic_language,
        "batch_size": config.batch_size,
        "do_cell_matching": config.do_cell_matching,
        "document_timeout_s": config.document_timeout_s,
        "docling": RELEASE,
        "locate": where.version,
        "plans": [p.model_dump(mode="json") for p in plan_ranges(where, {}, config)],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _digest(config: IngestConfig, where: LocateResult) -> str:
    return convert.settings_hash(config, plan_ranges(where, {}, config), RELEASE, where.version)


def _store_convert(config: IngestConfig, where: LocateResult) -> ConvertResult:
    healthy = convert.convert_pdf(
        Path("synthetic.pdf"),
        where,
        {},
        config,
        runner_factory=lambda _config: FakeRunner(),
        docling=RELEASE,
    )
    old = healthy.model_copy(
        update={
            "version": "1",
            "settings_hash": _legacy_convert_hash(config, where),
            "settings_plans": None,
        }
    )
    (config.artifact_root / SHA / "convert.json").write_text(old.model_dump_json())
    return old


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("with_engine", [False, True])
@pytest.mark.parametrize("renamed", [False, True])
def test_legacy_tesseract_pages_are_rejected_even_without_an_engine(
    tmp_path: Path,
    empty: bool,
    with_engine: bool,
    renamed: bool,
) -> None:
    config = IngestConfig(convert_ocr="tesseract")
    engine = FakeOcr([TITLE])
    engine.name = "tesseract"
    old_page = text_page("" if empty else TITLE.text).model_copy(
        update={
            "mode": PageMode.IMAGE,
            "source": TextSource.OCR,
            "char_count": 0,
            "ocr_language": "en-US",
        }
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    target = f"pages.v{pages.PAGES_STAGE_VERSION if renamed else '4'}.json"
    (cache / target).write_text(
        json.dumps(
            {
                "version": "4",
                "settings": _legacy_page_settings(config, engine),
                "pages": [old_page.model_dump(mode="json")],
            }
        )
    )
    result = pages.read_pages(
        make_blank_pdf(tmp_path / "scan.pdf"),
        config,
        engine if with_engine else None,
        cache_dir=cache,
    )
    if with_engine:
        assert engine.calls > 0
        assert result[0].header_text == TITLE.text
    else:
        assert result[0].source is None
        assert result[0].flags == ["ocr_unavailable"]


@pytest.mark.parametrize("with_engine", [False, True])
def test_legacy_locate_digest_is_not_reused(tmp_path: Path, with_engine: bool) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    config = IngestConfig(artifact_root=tmp_path / "artifacts", convert_ocr="tesseract")
    engine = FakeOcr([TITLE]) if with_engine else None
    if engine is not None:
        engine.name = "tesseract"
    old = located([PageMode.IMAGE], [], sha256=pages.sha256_file(pdf)).model_copy(
        update={
            "ocr_key": hashlib.sha256(
                json.dumps(
                    _legacy_page_settings(config, engine),
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            "ocr_engine": engine.name if engine is not None else None,
        }
    )
    out = config.artifact_root / old.document.sha256
    out.mkdir(parents=True)
    (out / "locate.json").write_text(old.model_dump_json())
    result = stage.load_or_locate(pdf, config, engine)
    assert result.ocr_key != old.ocr_key
    if engine is not None:
        assert engine.calls > 0


def test_current_failed_locate_retries_ocr(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    first = stage.load_or_locate(pdf, config, FakeOcr(fail=True))
    assert "ocr_failed_pages:1" in first.flags
    healthy = FakeOcr([TITLE])
    result = stage.load_or_locate(pdf, config, healthy)
    assert healthy.calls > 0
    assert "ocr_failed_pages:1" not in result.flags


def test_legacy_convert_cannot_hide_a_fresh_failure(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path, convert_ocr="tesseract")
    where = located([PageMode.IMAGE], [(1, 1)], sha256=SHA)
    _store_convert(config, where)
    runner = FakeRunner({"1-1": "raise"})
    result = convert.convert_pdf(
        Path("synthetic.pdf"),
        where,
        {},
        config,
        runner_factory=lambda _config: runner,
        docling=RELEASE,
    )
    assert len(runner.calls) == 1
    assert result.all_failed
    assert result.version != "1"


def test_cli_cannot_add_plan_evidence_to_an_old_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    old = _store_convert(config, where)
    stored = tmp_path / SHA / "convert.json"
    before = stored.read_bytes()
    monkeypatch.setattr(cli, "load_or_locate", lambda *_args, **_kwargs: where)
    monkeypatch.setattr(cli, "page_ocr_languages", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "convert_pdf", lambda *_args, **_kwargs: old)
    args = argparse.Namespace(
        pdf=Path("synthetic.pdf"), no_cache=False, json=True, transported_config=True
    )
    cli._convert(args, config, None)
    result = ConvertResult.model_validate_json(capsys.readouterr().out)
    assert result.settings_plans is None
    assert stored.read_bytes() == before


@pytest.mark.parametrize("contract", ["pages", "convert"])
def test_behavior_versions_propagate_through_all_dependent_digests(
    monkeypatch: pytest.MonkeyPatch,
    contract: str,
) -> None:
    config = IngestConfig()
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    old_ocr = pages.ocr_key(config, None)
    old_digest = _digest(config, where)
    result = ConvertResult(
        version=convert.CONVERT_VERSION,
        sha256=SHA,
        locate_version=where.version,
        docling_version=RELEASE,
        device=config.device,
        settings_hash=old_digest,
        ranges=[],
    )
    old_structure = structure._settings_hash(config, result)
    if contract == "pages":
        monkeypatch.setattr(pages, "PAGES_STAGE_VERSION", "future")
        monkeypatch.setattr(convert, "PAGES_STAGE_VERSION", "future", raising=False)
        assert pages.ocr_key(config, None) != old_ocr
    else:
        monkeypatch.setattr(convert, "CONVERT_VERSION", "future")
    new_digest = _digest(config, where)
    assert new_digest != old_digest
    assert (
        structure._settings_hash(
            config,
            result.model_copy(
                update={
                    "settings_hash": new_digest,
                }
            ),
        )
        != old_structure
    )


def test_legacy_structure_forces_current_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    old = _store_convert(config, where)
    cached = StructureResult(
        version=structure.STRUCTURE_VERSION,
        sha256=SHA,
        convert_version=old.version,
        settings_hash=structure._settings_hash(config, old),
        flags=["obsolete_success"],
    )
    out = tmp_path / SHA
    (out / "statements.raw.json").write_text(cached.model_dump_json())
    (out / "table_checks.json").write_text("[]")
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args, **_kwargs: where)
    monkeypatch.setattr(structure, "page_ocr_languages", lambda *_args: {})
    monkeypatch.setattr(structure, "docling_version", lambda: RELEASE)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kwargs: [])
    calls: list[Path] = []

    def fresh(pdf: Path, _config: IngestConfig) -> ConvertResult:
        calls.append(pdf)
        return old.model_copy(
            update={
                "version": convert.CONVERT_VERSION,
                "settings_hash": _digest(config, where),
                "ranges": [],
                "flags": [],
            }
        )

    result = structure.structure_pdf(Path("synthetic.pdf"), config, None, convert=fresh)
    assert len(calls) == 1
    assert "obsolete_success" not in result.flags
    assert result.settings_hash != cached.settings_hash


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_structure_retries_current_unsuccessful_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    old = _store_convert(config, where)
    failed = old.model_copy(
        update={
            "version": convert.CONVERT_VERSION,
            "settings_hash": _digest(config, where),
            "ranges": [r.model_copy(update={"status": status}) for r in old.ranges],
        }
    )
    (tmp_path / SHA / "convert.json").write_text(failed.model_dump_json())
    monkeypatch.setattr(structure, "page_ocr_languages", lambda *_args: {})
    monkeypatch.setattr(structure, "docling_version", lambda: RELEASE)
    calls: list[Path] = []

    def fresh(pdf: Path, _config: IngestConfig) -> ConvertResult:
        calls.append(pdf)
        return failed.model_copy(update={"ranges": []})

    structure.current_convert(Path("synthetic.pdf"), config, None, where, fresh)
    assert len(calls) == 1


def test_fresh_conversion_records_plans_and_reuses_them(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    plans: list[RangePlan] = plan_ranges(where, {}, config)
    healthy = FakeRunner()
    result = convert.convert_pdf(
        Path("synthetic.pdf"),
        where,
        {},
        config,
        runner_factory=lambda _config: healthy,
        docling=RELEASE,
    )
    assert result.settings_plans == plans
    repeated = FakeRunner({"1-1": "raise"})
    cached = convert.convert_pdf(
        Path("synthetic.pdf"),
        where,
        {},
        config,
        runner_factory=lambda _config: repeated,
        docling=RELEASE,
    )
    assert repeated.calls == []
    assert cached == result


@pytest.mark.parametrize("prior_version", ["18", "19", "20"])
def test_period_evidence_changes_only_the_structure_cache_contract(
    monkeypatch: pytest.MonkeyPatch,
    prior_version: str,
) -> None:
    assert structure.STRUCTURE_VERSION == "21"
    assert convert.CONVERT_VERSION == "2"
    assert pages.PAGES_STAGE_VERSION == "5"
    config = IngestConfig()
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    converted = ConvertResult(
        version=convert.CONVERT_VERSION,
        sha256=SHA,
        locate_version=where.version,
        docling_version=RELEASE,
        device=config.device,
        settings_hash=_digest(config, where),
        ranges=[],
    )
    current = structure._settings_hash(config, converted)
    monkeypatch.setattr(structure, "STRUCTURE_VERSION", prior_version)
    assert structure._settings_hash(config, converted) != current
    assert _digest(config, where) == converted.settings_hash


@pytest.mark.parametrize("prior_version", ["18", "19", "20"])
def test_prior_structure_is_not_reused_but_healthy_conversion_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_version: str,
) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    where = located([PageMode.TEXT], [(1, 1)], sha256=SHA)
    runner = FakeRunner()
    converted = convert.convert_pdf(
        Path("synthetic.pdf"),
        where,
        {},
        config,
        runner_factory=lambda _config: runner,
        docling=RELEASE,
    )
    with monkeypatch.context() as legacy:
        legacy.setattr(structure, "STRUCTURE_VERSION", prior_version)
        old = StructureResult(
            version=prior_version,
            sha256=SHA,
            convert_version=converted.version,
            settings_hash=structure._settings_hash(config, converted),
            flags=["obsolete_structure"],
        )
    out = tmp_path / SHA
    (out / "statements.raw.json").write_text(old.model_dump_json())
    (out / "table_checks.json").write_text("[]")
    monkeypatch.setattr(structure, "load_or_locate", lambda *_args, **_kwargs: where)
    monkeypatch.setattr(structure, "page_ocr_languages", lambda *_args: {})
    monkeypatch.setattr(structure, "docling_version", lambda: RELEASE)
    monkeypatch.setattr(structure, "read_pages", lambda *_args, **_kwargs: [])
    calls: list[Path] = []

    def cached_convert(pdf: Path, _config: IngestConfig) -> ConvertResult:
        calls.append(pdf)
        return convert.convert_pdf(
            pdf,
            where,
            {},
            config,
            runner_factory=lambda _config: FakeRunner({"1-1": "raise"}),
            docling=RELEASE,
        )

    result = structure.structure_pdf(Path("synthetic.pdf"), config, None, convert=cached_convert)
    assert calls == []
    assert result.version == "21"
    assert result.settings_hash != old.settings_hash
    assert "obsolete_structure" not in result.flags
    assert ConvertResult.model_validate_json((out / "convert.json").read_text()) == converted
