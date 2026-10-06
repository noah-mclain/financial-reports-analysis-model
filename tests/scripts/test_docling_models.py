from __future__ import annotations

import importlib.util
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "docling_models.py"


def test_the_cache_key_names_both_installed_versions_and_the_layout_model() -> None:
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    layout = PdfPipelineOptions().layout_options.model_spec
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "key"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == (
        f"docling-hf-{version('docling')}-{version('docling-ibm-models')}"
        f"-{layout.repo_id.replace('/', '_')}@{layout.revision}"
    )


def test_an_unknown_command_is_refused() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "fetch"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 2
    assert "invalid choice" in done.stderr


def test_the_layout_model_is_the_one_the_conversion_pipeline_selects() -> None:
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    spec = importlib.util.spec_from_file_location("docling_models_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.layout_model_spec() == PdfPipelineOptions().layout_options.model_spec
    assert module.layout_model_spec().repo_id == "docling-project/docling-layout-heron"
