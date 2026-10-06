"""Exercise the pinned OCR extension and pipeline error path with synthetic regions."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from fra_ingest import converter
from fra_ingest.docling_ocr import tesseract_pipeline
from fra_ingest.ocr import OcrEngineError


def test_a_failed_region_among_successful_regions_cannot_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pandas as pd  # type: ignore[import-untyped]  # Pinned Docling dataframe boundary.
    from docling.models.stages.ocr.tesseract_ocr_cli_model import TesseractOcrCliModel

    # Before the fix, the ordinary model catches the failure and yields a successful page.
    pipeline_type = getattr(converter, "tesseract_pipeline", lambda: None)()
    model_type = TesseractOcrCliModel if pipeline_type is None else pipeline_type.ocr_model_type
    model: Any = object.__new__(model_type)
    model.enabled = True
    model.scale = 1
    model._is_auto = False
    rect = SimpleNamespace(area=lambda: 100)
    monkeypatch.setattr(model, "get_ocr_rects", lambda page: [rect, rect])
    monkeypatch.setattr(model, "post_process_cells", lambda *args: None)
    monkeypatch.setattr(
        model,
        "_perform_osd",
        lambda filename: pd.DataFrame({"key": ["Orientation in degrees"], "value": ["0"]}),
    )
    calls: list[str] = []

    def read(self: object, filename: str, osd: object) -> Any:
        calls.append(filename)
        if len(calls) == 2:
            raise subprocess.CalledProcessError(7, ["tesseract"])
        return pd.DataFrame(columns=["text", "conf", "left", "top", "width", "height"])

    monkeypatch.setattr(TesseractOcrCliModel, "_run_tesseract", read)
    backend = SimpleNamespace(
        is_valid=lambda: True, get_page_image=lambda **kwargs: Image.new("RGB", (10, 10))
    )
    page: Any = SimpleNamespace(_backend=backend, page_no=1)
    result: Any = SimpleNamespace(input=SimpleNamespace(file=Path("synthetic.pdf")), timings={})
    with pytest.raises(OcrEngineError, match=r"tesseract.*exit 7"):
        list(model(result, [page]))
    assert len(calls) == 2


@pytest.mark.parametrize("success_pages,expected_status", [(0, "failed"), (1, "partial")])
def test_pipeline_records_ocr_failure_in_range_output(
    success_pages: int, expected_status: str
) -> None:
    from docling.pipeline.standard_pdf_pipeline import (
        ProcessingResult,
        ThreadedItem,
        ThreadedPipelineStage,
    )

    from fra_ingest.config import IngestConfig
    from fra_ingest.results import RangePlan

    def failed(result: object, pages: object) -> object:
        raise OcrEngineError("tesseract OCR region failed: exit 7")

    result: Any = SimpleNamespace(
        pages=[SimpleNamespace(page_no=1)], errors=[], document=SimpleNamespace(pages={}, tables=[])
    )
    stage = ThreadedPipelineStage(
        name="ocr", model=failed, batch_size=1, batch_timeout=0.1, queue_max_size=1
    )
    item = ThreadedItem(payload=result.pages[0], run_id=1, page_no=1, conv_res=result)
    [processed] = stage._process_batch([item])
    assert processed.is_failed
    assert processed.failure is not None and processed.error is not None
    assert processed.failure.error_message == "tesseract OCR region failed: exit 7"
    pipeline: Any = object.__new__(tesseract_pipeline())
    pipeline.keep_images = True
    pipeline.keep_backend = True
    pipeline.pipeline_options = SimpleNamespace(generate_parsed_pages=True)
    successful: Any = SimpleNamespace(page_no=2, _backend=None)
    if success_pages:
        result.pages.append(successful)
        result.document.pages[2] = SimpleNamespace(image=None)
    proc = ProcessingResult(
        pages=[successful] if success_pages else [],
        failed_pages=[(1, processed.error, processed.failure)],
        total_expected=1 + success_pages,
    )
    pipeline._integrate_results(result, proc)
    plan = RangePlan(
        first_page=1, last_page=1 + success_pages, ocr="full_page", ocr_language="en-US"
    )
    runner = converter.DoclingRunner(IngestConfig(convert_ocr="tesseract", device="cpu"))
    runner._converters[(plan.ocr, plan.ocr_language)] = SimpleNamespace(
        convert=lambda *args, **kwargs: result
    )
    output = runner(Path("synthetic.pdf"), plan)
    assert output.status == expected_status
    assert output.errors == ["tesseract OCR region failed: exit 7"]
