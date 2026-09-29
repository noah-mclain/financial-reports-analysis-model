"""The docling adapter (spec 10, Components)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from support import run_python

from fra_ingest.config import IngestConfig
from fra_ingest.converter import DoclingRunner, docling_version, pipeline_options
from fra_ingest.results import RangePlan

ALMARAI_EN = "almarai-2025-en-annualreport.pdf"


def test_importing_the_converter_loads_neither_docling_nor_torch() -> None:
    code = (
        "import sys, fra_ingest.converter\nprint('docling' in sys.modules, 'torch' in sys.modules)"
    )
    assert run_python(code).stdout.split() == ["False", "False"]


def test_the_docling_version_is_the_pinned_one() -> None:
    assert docling_version() == "2.126.0"


@pytest.mark.slow
def test_options_follow_the_plan_and_the_settings() -> None:
    config = IngestConfig(device="cpu", images_scale=1.5, batch_size=3)
    scanned = RangePlan(first_page=5, last_page=5, ocr="full_page", ocr_language="ar-SA")
    options = pipeline_options(config, scanned)
    assert options.do_ocr is True
    assert options.ocr_options.lang == ["ar-SA"]
    assert options.ocr_options.mode.name == "FULL_PAGE"
    assert options.generate_page_images is True
    assert options.images_scale == 1.5
    assert (options.ocr_batch_size, options.layout_batch_size, options.table_batch_size) == (
        3,
        3,
        3,
    )
    assert options.accelerator_options.device.name == "CPU"
    assert options.table_structure_options.mode.name == "ACCURATE"

    digital = RangePlan(first_page=1, last_page=2, ocr="pdf_aware", ocr_language="en-US")
    assert pipeline_options(config, digital).ocr_options.mode.name == "PDF_AWARE_LAYOUT_REGIONS"

    no_engine = RangePlan(first_page=1, last_page=2, ocr="pdf_aware", ocr_language=None)
    assert pipeline_options(config, no_engine).do_ocr is False


@pytest.mark.slow
@pytest.mark.golden
def test_almarai_balance_sheet_converts_with_its_page_numbers_and_images(
    golden: Callable[[str], Path], tmp_path: Path
) -> None:
    pdf = golden(ALMARAI_EN)
    runner = DoclingRunner(IngestConfig())
    plan = RangePlan(first_page=156, last_page=158, ocr="pdf_aware", ocr_language="en-US")
    output = runner(pdf, plan)

    assert output.status == "ok"
    assert output.page_numbers == [156, 157, 158]
    assert output.tables >= 1
    image = output.page_images[156]
    assert image is not None
    document = pdfium.PdfDocument(pdf)
    width_pt = document[155].get_size()[0]
    document.close()
    assert abs(image.width - 2 * width_pt) <= 2
    output.write_json(tmp_path / "doc.json")
    assert (tmp_path / "doc.json").stat().st_size > 1000
    assert runner.models_seconds > 0


@pytest.mark.slow
@pytest.mark.golden
def test_a_scanned_arabic_statement_converts_with_full_page_ocr(
    golden: Callable[[str], Path],
) -> None:
    runner = DoclingRunner(IngestConfig())
    plan = RangePlan(
        first_page=5, last_page=5, ocr="full_page", ocr_language="ar-SA", image_pages=(5,)
    )
    output = runner(golden("juhayna-2025-ar-consolidated.pdf"), plan)
    assert output.status == "ok"
    assert output.page_numbers == [5]
    assert output.tables >= 1
