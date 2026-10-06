"""docling behind one call per range.

The only module that imports docling, and only inside functions, so importing it does not load
PyTorch (spec 10, Components). Everything it returns is in our own types.
"""

from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from PIL import Image

from fra_ingest.config import IngestConfig
from fra_ingest.docling_ocr import tesseract_pipeline
from fra_ingest.ocr import TESSERACT_COMMAND, engine_language
from fra_ingest.results import RangePlan

RunStatus = Literal["ok", "partial", "failed"]


@dataclass
class RangeOutput:
    """What one docling run over one range gave."""

    status: RunStatus
    page_numbers: list[int]
    page_images: dict[int, Image.Image | None]
    tables: int
    errors: list[str]
    write_json: Callable[[Path], None]


class RangeRunner(Protocol):
    models_seconds: float

    def __call__(self, pdf: Path, plan: RangePlan) -> RangeOutput: ...


def docling_version() -> str:
    return importlib.metadata.version("docling")


def pipeline_options(config: IngestConfig, plan: RangePlan) -> Any:
    """The options of 04, 1.2, with the OCR mode and one language taken from the plan, and the
    OCR engine from ``config.convert_ocr``: the one that also reads our own pages."""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.pipeline_options import (
        OcrMacOptions,
        OcrMode,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
        TesseractCliOcrOptions,
    )

    if plan.ocr == "skipped":
        msg = f"range {plan.label} is skipped and has no pipeline"
        raise ValueError(msg)
    devices = {
        "mps": AcceleratorDevice.MPS,
        "cpu": AcceleratorDevice.CPU,
        "auto": AcceleratorDevice.AUTO,
    }
    options = PdfPipelineOptions(
        do_ocr=plan.ocr_language is not None,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=config.do_cell_matching,
        ),
        generate_page_images=True,
        images_scale=config.images_scale,
        ocr_batch_size=config.batch_size,
        layout_batch_size=config.batch_size,
        table_batch_size=config.batch_size,
        document_timeout=config.document_timeout_s,
        accelerator_options=AcceleratorOptions(device=devices[config.device]),
    )
    if plan.ocr_language is not None:
        mode = OcrMode.FULL_PAGE if plan.ocr == "full_page" else OcrMode.PDF_AWARE_LAYOUT_REGIONS
        engine = config.convert_ocr
        if engine == "none":
            msg = f"range {plan.label} names OCR language {plan.ocr_language} but no engine is set"
            raise ValueError(msg)
        common: dict[str, Any] = {
            "lang": [engine_language(config, plan.ocr_language)],
            "mode": mode,
            "scale": config.ocr_scale,
        }
        if engine == "ocrmac":
            options.ocr_options = OcrMacOptions(**common)
        else:
            options.ocr_options = TesseractCliOcrOptions(
                tesseract_cmd=TESSERACT_COMMAND, psm=config.tesseract_psm, **common
            )
    return options


class DoclingRunner:
    """Converts ranges, keeping one converter per (OCR mode, language) so a document loads
    docling's models once in the usual case."""

    def __init__(self, config: IngestConfig) -> None:
        self.config = config
        self.models_seconds = 0.0
        self._converters: dict[tuple[str, str | None], Any] = {}

    def _converter(self, plan: RangePlan) -> Any:
        key = (plan.ocr, plan.ocr_language)
        if key not in self._converters:
            started = time.perf_counter()
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = pipeline_options(self.config, plan)
            format_args: dict[str, Any] = {"pipeline_options": options}
            if self.config.convert_ocr == "tesseract":
                format_args["pipeline_cls"] = tesseract_pipeline()
            converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(**format_args)}
            )
            converter.initialize_pipeline(InputFormat.PDF)
            self._converters[key] = converter
            self.models_seconds += time.perf_counter() - started
        return self._converters[key]

    def __call__(self, pdf: Path, plan: RangePlan) -> RangeOutput:
        from docling.datamodel.base_models import ConversionStatus
        from docling_core.types.doc import ImageRefMode

        converter = self._converter(plan)
        result = converter.convert(
            pdf, page_range=(plan.first_page, plan.last_page), raises_on_error=False
        )
        document = result.document
        statuses: dict[Any, RunStatus] = {
            ConversionStatus.SUCCESS: "ok",
            ConversionStatus.PARTIAL_SUCCESS: "partial",
        }
        images: dict[int, Image.Image | None] = {}
        for page_no, page in document.pages.items():
            images[int(page_no)] = page.image.pil_image if page.image is not None else None

        def write_json(path: Path) -> None:
            # Page images are written as PNGs beside it, not embedded.
            document.save_as_json(path, image_mode=ImageRefMode.PLACEHOLDER)

        return RangeOutput(
            status=statuses.get(result.status, "failed"),
            page_numbers=sorted(images),
            page_images=images,
            tables=len(document.tables),
            errors=[str(error.error_message) for error in result.errors],
            write_json=write_json,
        )
