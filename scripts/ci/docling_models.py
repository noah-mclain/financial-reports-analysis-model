"""The docling model weights the pipeline loads from the Hugging Face cache, for CI.

``key`` prints a cache key naming the installed docling and docling-ibm-models versions.
``warm`` downloads exactly the repositories the conversion pipeline reads (the layout detector,
named by the pipeline options of ``fra_ingest.converter``, and the table structure model) into
the Hugging Face cache, the same call docling makes. Run with network access; everything after
it runs with HF_HUB_OFFLINE=1, so a model missing from the cache is an error.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version
from typing import Any

from fra_ingest.config import IngestConfig
from fra_ingest.converter import pipeline_options
from fra_ingest.results import RangePlan


def cache_key() -> str:
    return f"docling-hf-{version('docling')}-{version('docling-ibm-models')}"


def layout_model_spec() -> Any:
    """The layout model the project's own pipeline options select, so there is one source."""
    plan = RangePlan(first_page=1, last_page=1, ocr="pdf_aware", ocr_language=None)
    return pipeline_options(IngestConfig(convert_ocr="none"), plan).layout_options.model_spec


def warm() -> None:
    from docling.models.stages.table_structure.table_structure_model import TableStructureModel
    from docling.models.utils.hf_model_download import download_hf_model

    layout = layout_model_spec()
    layout_path = download_hf_model(layout.repo_id, revision=layout.revision)
    # The table model's own helper with no local_dir, which fills the Hugging Face cache.
    table_path = TableStructureModel.download_models()
    print(f"layout {layout_path}\ntable {table_path}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("key", "warm"))
    command = parser.parse_args(argv).command
    if command == "key":
        print(cache_key())
    else:
        warm()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
