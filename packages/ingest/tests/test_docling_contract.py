"""The docling names spec 10 relies on, checked against the pinned version (04, 1.2; R25)."""

from __future__ import annotations

import importlib.metadata
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


def test_docling_is_the_pinned_version() -> None:
    assert importlib.metadata.version("docling") == "2.126.0"
    assert importlib.metadata.version("docling-core") == "2.96.0"


def test_pipeline_option_names_exist() -> None:
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMacOptions,
        OcrMode,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ImageRefMode

    fields = set(PdfPipelineOptions.model_fields)
    expected = {
        "do_ocr",
        "ocr_options",
        "do_table_structure",
        "table_structure_options",
        "generate_page_images",
        "images_scale",
        "ocr_batch_size",
        "layout_batch_size",
        "table_batch_size",
        "document_timeout",
        "accelerator_options",
    }
    assert expected <= fields, expected - fields
    assert {"lang", "mode"} <= set(OcrMacOptions.model_fields)
    assert {"mode", "do_cell_matching"} <= set(TableStructureOptions.model_fields)
    assert {"FULL_PAGE", "PDF_AWARE_LAYOUT_REGIONS"} <= {m.name for m in OcrMode}
    assert TableFormerMode.ACCURATE is not None
    assert {"MPS", "CPU", "AUTO"} <= {d.name for d in AcceleratorDevice}
    assert {"SUCCESS", "PARTIAL_SUCCESS", "FAILURE"} <= {s.name for s in ConversionStatus}
    assert ImageRefMode.PLACEHOLDER is not None
    assert "device" in AcceleratorOptions.model_fields
    assert InputFormat.PDF is not None and PdfFormatOption is not None
    assert callable(DocumentConverter.initialize_pipeline)
    parameters = inspect.signature(DocumentConverter.convert).parameters
    assert {"page_range", "raises_on_error"} <= set(parameters)


@pytest.mark.golden
def test_page_range_keeps_the_document_page_numbers(golden: Callable[[str], Path]) -> None:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    result = converter.convert(golden("almarai-2025-en-annualreport.pdf"), page_range=(156, 158))
    assert sorted(result.document.pages) == [156, 157, 158]
