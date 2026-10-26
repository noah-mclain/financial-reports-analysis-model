"""Renderer selection and legacy conversion caches, without loading docling or PDFs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError
from support import FakeRunner, located

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig, OcrEngineName, load_config
from fra_ingest.convert import cached_convert, convert_pdf, settings_hash
from fra_ingest.converter import DoclingRunner
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, RangePlan

TXT, IMG = PageMode.TEXT, PageMode.IMAGE
RELEASE = "2.126.0"
# The digests the convert stage produced before a range could name a renderer, under
# LOCATE_VERSION 7: a default plan must keep producing exactly these.
LEGACY_HASHES = {
    TXT: "3e24f57f531d059d6b9a4b91f3052d54171c9c46d55bd90a6954b6947be201c2",
    IMG: "4502731c9337b9d869f5f9f2113436755db5d49012aa122f26ebdd64999a55df",
}


@pytest.mark.parametrize("engine", ["ocrmac", "tesseract"])
def test_only_listed_full_page_ranges_select_pdfium(engine: OcrEngineName) -> None:
    config = IngestConfig(convert_ocr=engine, pdfium_full_page_languages=("en-US",))
    doc = located([IMG, IMG, TXT], [(1, 1), (2, 2), (3, 3)])
    plans = plan_ranges(doc, {1: "en-US", 2: "ar-SA"}, config)
    assert [(p.ocr, p.renderer) for p in plans] == [
        ("full_page", "pdfium"),
        ("full_page", "default"),
        ("pdf_aware", "default"),
    ]
    assert plans[2].ocr_language == "en-US"  # Layout-region OCR also keeps the default.


@pytest.mark.parametrize("engine", ["ocrmac", "tesseract", "none"])
def test_empty_setting_preserves_existing_plans(engine: OcrEngineName) -> None:
    config = IngestConfig(convert_ocr=engine)
    plans = plan_ranges(located([IMG, TXT], [(1, 1), (2, 2)]), {1: "en-US"}, config)
    assert plans == [
        RangePlan(
            first_page=1,
            last_page=1,
            ocr="skipped" if engine == "none" else "full_page",
            ocr_language=None if engine == "none" else "en-US",
            image_pages=(1,),
        ),
        RangePlan(
            first_page=2,
            last_page=2,
            ocr="pdf_aware",
            ocr_language=None if engine == "none" else "en-US",
        ),
    ]
    assert all(p.renderer == "default" for p in plans)


@pytest.mark.parametrize("engine", ["none"])
def test_disabled_engine_keeps_default_renderer(engine: OcrEngineName) -> None:
    config = IngestConfig(convert_ocr=engine, pdfium_full_page_languages=("en-US",))
    plans = plan_ranges(located([IMG, TXT], [(1, 1), (2, 2)]), {1: "en-US"}, config)
    assert all(p.renderer == "default" for p in plans)


def test_docker_profile_selects_listed_full_page_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRA_PROFILE", "docker")
    monkeypatch.delenv("FRA_OCR_ENGINE", raising=False)
    path = tmp_path / "ingest.toml"
    path.write_text('[convert]\npdfium_full_page_languages = ["en-US"]\n')
    config = load_config(path)
    assert config.convert_ocr == "tesseract"
    assert plan_ranges(located([IMG], [(1, 1)]), {1: "en-US"}, config)[0].renderer == "pdfium"


@pytest.mark.parametrize("value", ['"en-US"', "42", "[42]", '["unknown"]'])
def test_invalid_renderer_languages_fail_at_configuration_time(tmp_path: Path, value: str) -> None:
    path = tmp_path / "ingest.toml"
    path.write_text(f"[convert]\npdfium_full_page_languages = {value}\n")
    with pytest.raises(ValueError, match="pdfium_full_page_languages"):
        load_config(path)


def test_renderer_language_must_be_configured_for_ocr() -> None:
    with pytest.raises(ValueError, match="pdfium_full_page_languages"):
        IngestConfig(ocr_languages=("ar-SA",), pdfium_full_page_languages=("en-US",))


def test_renderer_language_must_be_readable_even_when_in_ocr_languages() -> None:
    with pytest.raises(ValueError, match="pdfium_full_page_languages"):
        IngestConfig(ocr_languages=("unknown",), pdfium_full_page_languages=("unknown",))


def test_unknown_renderer_fails_when_a_plan_is_validated() -> None:
    with pytest.raises(ValidationError, match="renderer"):
        RangePlan.model_validate(
            dict(first_page=1, last_page=1, ocr="full_page", ocr_language="en-US", renderer="bad")
        )


def test_shipped_setting_and_config_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from fra_ingest.config import EFFECTIVE_CONFIG_ENV, transported_config

    config = load_config()
    assert config.pdfium_full_page_languages == ("en-US",)
    monkeypatch.setenv(EFFECTIVE_CONFIG_ENV, config.model_dump_json())
    assert transported_config() == config


@pytest.mark.parametrize("mode", [TXT, IMG])
def test_hash_preserves_legacy_default_and_changes_only_affected_ranges(mode: PageMode) -> None:
    doc = located([mode], [(1, 1)])
    old = IngestConfig()
    selected = IngestConfig(pdfium_full_page_languages=("en-US",))
    old_hash = settings_hash(old, plan_ranges(doc, {1: "en-US"}, old), RELEASE, doc.version)
    assert old_hash == LEGACY_HASHES[mode]
    new_hash = settings_hash(
        selected, plan_ranges(doc, {1: "en-US"}, selected), RELEASE, doc.version
    )
    assert (new_hash != old_hash) is (mode is IMG)
    arabic = {1: "ar-SA"}
    assert settings_hash(old, plan_ranges(doc, arabic, old), RELEASE, doc.version) == settings_hash(
        selected, plan_ranges(doc, arabic, selected), RELEASE, doc.version
    )


@pytest.mark.parametrize("mode", [TXT, IMG])
@pytest.mark.parametrize("engine", ["ocrmac", "tesseract"])
def test_legacy_cache_reused_unless_the_renderer_changes(
    tmp_path: Path, mode: PageMode, engine: OcrEngineName
) -> None:
    doc = located([mode], [(1, 1)])
    old = IngestConfig(artifact_root=tmp_path, convert_ocr=engine)
    first = convert_pdf(
        Path("unread.pdf"),
        doc,
        {1: "en-US"},
        old,
        runner_factory=lambda _: FakeRunner(),
        docling=RELEASE,
    )
    if engine == "ocrmac":
        assert first.settings_hash == LEGACY_HASHES[mode]
    path = tmp_path / doc.document.sha256 / "convert.json"
    payload = json.loads(path.read_text())
    for plan in payload["settings_plans"]:
        plan.pop("renderer", None)
    path.write_text(json.dumps(payload))
    restored = cached_convert(path.parent, first.settings_hash)
    assert restored is not None
    assert restored.settings_plans is not None
    assert restored.settings_plans[0].renderer == "default"

    selected = IngestConfig(
        artifact_root=tmp_path, convert_ocr=engine, pdfium_full_page_languages=("en-US",)
    )
    runner = FakeRunner()
    second = convert_pdf(
        Path("unread.pdf"),
        doc,
        {1: "en-US"},
        selected,
        runner_factory=lambda _: runner,
        docling=RELEASE,
    )
    assert bool(runner.calls) is (mode is IMG)
    assert second.settings_plans is not None
    assert second.settings_plans[0].renderer == ("pdfium" if mode is IMG else "default")
    assert ConvertResult.model_validate_json(path.read_text()) == second


@pytest.mark.parametrize("engine", ["ocrmac", "tesseract"])
def test_converter_factory_receives_backend_and_keys_by_renderer(
    monkeypatch: pytest.MonkeyPatch,
    engine: OcrEngineName,
) -> None:
    from fra_ingest import converter

    class DefaultBackend:
        pass

    class PdfiumBackend:
        pass

    received: list[type[object]] = []

    class FormatOption:
        def __init__(
            self,
            *,
            pipeline_options: object,
            backend: type[object] = DefaultBackend,
            pipeline_cls: object = None,
        ) -> None:
            assert (pipeline_cls is not None) is (engine == "tesseract")
            received.append(backend)

    class Factory:
        def __init__(self, *, format_options: dict[str, FormatOption]) -> None:
            assert "pdf" in format_options

        def initialize_pipeline(self, format: str) -> None:
            assert format == "pdf"

    modules: dict[str, dict[str, object]] = {
        "docling.datamodel.base_models": {"InputFormat": type("InputFormat", (), {"PDF": "pdf"})},
        "docling.document_converter": {
            "DocumentConverter": Factory,
            "PdfFormatOption": FormatOption,
        },
        "docling.backend.pypdfium2_backend": {"PyPdfiumDocumentBackend": PdfiumBackend},
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        for key, value in attributes.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(converter, "pipeline_options", lambda config, plan: object())
    monkeypatch.setattr(converter, "tesseract_pipeline", lambda: object())
    config = IngestConfig(convert_ocr=engine, pdfium_full_page_languages=("en-US",))
    plans = plan_ranges(located([IMG, IMG], [(1, 1), (2, 2)]), {1: "en-US", 2: "ar-SA"}, config)
    runner = DoclingRunner(config)
    english, arabic = [runner._converter(p) for p in plans]
    assert english is not arabic
    assert received == [PdfiumBackend, DefaultBackend]
    assert runner._converter(plans[0]) is english
    default_english = plans[0].model_copy(update={"renderer": "default"})
    assert runner._converter(default_english) is not english
    assert received == [PdfiumBackend, DefaultBackend, DefaultBackend]


@pytest.mark.parametrize(
    "update,plan",
    [
        ({"convert_ocr": "none"}, {"ocr": "full_page", "ocr_language": "en-US"}),
        ({"convert_ocr": "tesseract"}, {"ocr": "full_page", "ocr_language": "ar-SA"}),
        ({"convert_ocr": "tesseract"}, {"ocr": "pdf_aware", "ocr_language": "en-US"}),
        ({}, {"ocr": "full_page", "ocr_language": "ar-SA"}),
        ({}, {"ocr": "pdf_aware", "ocr_language": "en-US"}),
        ({"pdfium_full_page_languages": ()}, {"ocr": "full_page", "ocr_language": "en-US"}),
    ],
)
def test_runner_rejects_a_pdfium_plan_the_rule_does_not_allow(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, object], plan: dict[str, str]
) -> None:
    # plan_ranges never writes such a plan; one cached under other settings must not
    # reach docling with an engine and backend that were never measured together.
    monkeypatch.setitem(sys.modules, "docling.document_converter", None)
    config = IngestConfig(pdfium_full_page_languages=("en-US",)).model_copy(update=update)
    stale = RangePlan.model_validate(
        {"first_page": 1, "last_page": 1, "renderer": "pdfium", **plan}
    )
    with pytest.raises(ValueError, match="pdfium renderer is not configured"):
        DoclingRunner(config)._converter(stale)
