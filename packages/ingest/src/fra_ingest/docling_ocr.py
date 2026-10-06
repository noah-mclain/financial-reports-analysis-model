"""The pinned Docling extension boundary that preserves Tesseract failures.

Loaded only when constructing a Tesseract converter. A failed OCR region fails its page;
Docling's pipeline records the exception and marks the range failed or partially successful.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from fra_ingest.ocr import OcrEngineError


@lru_cache(maxsize=1)
def tesseract_pipeline() -> Any:
    """A local pipeline class; no factory registration or global dependency patching."""
    from docling.datamodel.pipeline_options import TesseractCliOcrOptions
    from docling.models.stages.ocr.tesseract_ocr_cli_model import TesseractOcrCliModel
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    class CheckedTesseract(TesseractOcrCliModel):
        def _run_tesseract(self, ifilename: str, osd: Any) -> Any:
            try:
                return super()._run_tesseract(ifilename, osd)
            except subprocess.CalledProcessError as exc:
                # The upstream __call__ catches CalledProcessError and drops the region.
                # A domain exception reaches the pipeline's structured page error boundary.
                raise OcrEngineError(f"tesseract OCR region failed: exit {exc.returncode}") from exc

        def _perform_osd(self, ifilename: str) -> Any:
            try:
                return super()._perform_osd(ifilename)
            except subprocess.CalledProcessError as exc:
                # Fixed-language OCR can proceed without orientation detection. Auto language
                # selection cannot: upstream would otherwise drop this region too.
                if self._is_auto:
                    raise OcrEngineError(
                        f"tesseract OSD region failed: exit {exc.returncode}"
                    ) from exc
                raise

    class CheckedPdfPipeline(StandardPdfPipeline):
        ocr_model_type = CheckedTesseract

        def _make_ocr_model(self, art_path: Path | None) -> Any:
            options = self.pipeline_options.ocr_options
            if isinstance(options, TesseractCliOcrOptions):
                return CheckedTesseract(
                    enabled=self.pipeline_options.do_ocr,
                    artifacts_path=art_path,
                    options=options,
                    accelerator_options=self.pipeline_options.accelerator_options,
                )
            return super()._make_ocr_model(art_path)

    return CheckedPdfPipeline
