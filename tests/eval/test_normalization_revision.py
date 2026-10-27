"""Synthetic paired dev runs: cached baseline never conceals an enabled model failure."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from harness import extraction, gates, mapping, model_mapping, structure
from test_extraction_harness import extracted_of, file_of, item, row, statement_of

from fra_ingest.config import IngestConfig
from fra_ingest.ocr import OcrEngineError
from fra_ingest.results import IndustryDecision, IndustrySignal, StructureResult
from fra_model.runtime import ModelRuntimeError


@pytest.fixture
def dev_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    expected_dir = tmp_path / "expected"
    expected_dir.mkdir()
    entries = []
    config = IngestConfig(artifact_root=tmp_path / "artifacts", convert_ocr="none")
    baseline = StructureResult(
        version="55",
        sha256="a" * 64,
        convert_version="1",
        settings_hash="synthetic",
        statements=[extracted_of([item(1, "Unfamiliar caption", "5")])],
    )
    artifact = config.artifact_root / baseline.sha256
    artifact.mkdir(parents=True)
    (artifact / "table_checks.json").write_text("[]")
    for n in range(7):
        doc_id = f"doc{n}"
        entries.append(
            {
                "id": doc_id,
                "file": f"{doc_id}.pdf",
                "language": "en" if n < 6 else "ar",
                "scale": 1000,
                "currency": "SAR",
            }
        )
        if n < 6:
            (tmp_path / f"{doc_id}.pdf").write_bytes(b"synthetic")
            expected = file_of(statement_of([row("Cash", "5")]), "draft").model_copy(
                update={"id": doc_id}
            )
            (expected_dir / f"{doc_id}.json").write_text(expected.model_dump_json())
        else:
            # English selection must happen before opening either this example or its PDF.
            (expected_dir / f"{doc_id}.json").write_text("must not open Arabic example")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump({"documents": entries}))
    for module in (extraction, gates, mapping, structure):
        monkeypatch.setattr(module, "MANIFEST", manifest)
        monkeypatch.setattr(module, "OUT", tmp_path / "eval")
        monkeypatch.setattr(module, "load_config", lambda **kwargs: config)
        if hasattr(module, "EXPECTED_DIR"):
            monkeypatch.setattr(module, "EXPECTED_DIR", expected_dir)
        if hasattr(module, "make_engine"):
            monkeypatch.setattr(module, "make_engine", lambda _: None)
    monkeypatch.delenv("FRA_OCR_ENGINE", raising=False)
    monkeypatch.setenv("FRA_PROFILE", "native")
    monkeypatch.setenv("FRA_LLM_PORT", "8123")
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "1")
    cached = Mock(return_value=baseline)
    monkeypatch.setattr(model_mapping, "structure_pdf", cached)
    runtime = Mock()
    runtime.normalize.return_value = '{"canonical_id":null}'
    monkeypatch.setattr("fra_worker.model_mapping.LlamaServerRuntime", Mock(return_value=runtime))
    return cached, runtime


def test_extraction_off_then_on_calls_runtime_even_on_cache_hits(
    dev_run: tuple[Mock, Mock], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached, runtime = dev_run
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "0")
    extraction.main(["--english-only"])
    assert cached.call_count == 6 and runtime.normalize.call_count == 0
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "1")
    extraction.main(["--english-only"])
    extraction.main(["--english-only"])
    assert cached.call_count == 18 and runtime.normalize.call_count == 12
    saved = json.loads((tmp_path / "eval/extraction-golden.json").read_text())
    assert saved["errored"] == []
    assert len(saved["statements"]) == 6


@pytest.mark.parametrize("harness", ["extraction", "mapping", "gates", "structure"])
def test_six_document_run_records_each_model_failure_and_returns_nonzero(
    dev_run: tuple[Mock, Mock], tmp_path: Path, harness: str
) -> None:
    cached, runtime = dev_run
    detail = "normalization server unreachable"
    runtime.normalize.side_effect = ModelRuntimeError(detail)
    module = {"extraction": extraction, "mapping": mapping, "gates": gates, "structure": structure}[
        harness
    ]
    assert module.main(["--english-only"]) == 1
    assert cached.call_count == runtime.normalize.call_count == 6
    name = "gates-dev" if harness == "gates" else f"{harness}-golden"
    saved = json.loads((tmp_path / f"eval/{name}.json").read_text())
    failed = saved["documents"] if harness == "structure" else saved["errored"]
    assert len(failed) == 6
    assert all(r["reason"] == "model_runtime" and r["detail"] == detail for r in failed)
    assert "aborted" not in saved
    if harness == "extraction":
        assert saved["statements"] == []
        assert saved["strata"]["total"]["documents"] == 0
    elif harness != "structure":
        assert saved["documents"] == []


@pytest.mark.parametrize("harness", ["extraction", "mapping", "gates", "structure"])
def test_invalid_model_reply_is_not_a_document_failure(
    dev_run: tuple[Mock, Mock], tmp_path: Path, harness: str
) -> None:
    cached, runtime = dev_run
    runtime.normalize.return_value = '{"canonical_id":"total_equity_and_liabilities"}'
    module = {"extraction": extraction, "mapping": mapping, "gates": gates, "structure": structure}[
        harness
    ]
    exit_code = module.main(["--english-only"])
    if harness != "gates":
        assert exit_code == 0
    else:
        assert exit_code == 1  # The fixture does not meet the independent gate thresholds.
    assert cached.call_count == runtime.normalize.call_count == 6
    name = "gates-dev" if harness == "gates" else f"{harness}-golden"
    saved = json.loads((tmp_path / f"eval/{name}.json").read_text())
    if harness == "structure":
        assert saved["documents"] and all("reason" not in row for row in saved["documents"])
    else:
        assert saved["errored"] == []


def test_enabled_decline_needs_no_normalization_or_old_baseline_checks(
    dev_run: tuple[Mock, Mock], tmp_path: Path
) -> None:
    cached, runtime = dev_run
    baseline = cached.return_value
    baseline.industry = IndustryDecision(
        outcome="declined", code="bank", reason="bank", signal=IndustrySignal(kind="bank")
    )
    baseline.statements = []
    (tmp_path / "artifacts" / baseline.sha256 / "table_checks.json").unlink()
    # Decline evidence remains incomplete; it must not be reported as a successful gate run.
    assert gates.main(["--english-only"]) == 1
    assert not runtime.validate.called
    saved = json.loads((tmp_path / "eval/gates-dev.json").read_text())
    assert len(saved["documents"]) == 6
    assert saved["errored"] == []
    assert all(d["locate_evidence"]["status"] == "incomplete" for d in saved["documents"])


@pytest.mark.parametrize("harness", ["extraction", "mapping", "gates", "structure"])
def test_partial_run_never_scores_failed_mapping_from_stale_normalization_artifacts(
    dev_run: tuple[Mock, Mock], tmp_path: Path, harness: str
) -> None:
    cached, runtime = dev_run
    # All synthetic documents share the cached source identity: the failed calls occur
    # after successful mapping has already written artifacts at that identity.
    runtime.normalize.side_effect = [
        '{"canonical_id":null}',
        ModelRuntimeError("invalid schema"),
        ModelRuntimeError("unreachable"),
        '{"canonical_id":null}',
        ModelRuntimeError("invalid schema"),
        '{"canonical_id":null}',
    ]
    module = {"extraction": extraction, "mapping": mapping, "gates": gates, "structure": structure}[
        harness
    ]
    assert module.main(["--english-only"]) == 1
    assert cached.call_count == runtime.normalize.call_count == 6
    name = "gates-dev" if harness == "gates" else f"{harness}-golden"
    saved = json.loads((tmp_path / f"eval/{name}.json").read_text())
    assert "aborted" not in saved
    if harness == "structure":
        failed = [d for d in saved["documents"] if "error" in d]
        succeeded = [d["id"] for d in saved["documents"] if "error" not in d]
    else:
        failed = saved["errored"]
        succeeded = [
            d["id"] for d in saved["statements" if harness == "extraction" else "documents"]
        ]
    assert [d["id"] for d in failed] == ["doc1", "doc2", "doc4"]
    assert succeeded == ["doc0", "doc3", "doc5"]
    if harness == "extraction":
        assert saved["planned_documents"] == 6
        assert saved["strata"]["total"]["documents"] == 3
        assert saved["strata"]["total"]["failed_documents"] == 3
        assert saved["strata"]["total"]["exit_code"] == 1


def test_extraction_retains_ocr_abort_threshold_and_writes_partial_report(
    dev_run: tuple[Mock, Mock], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.failures import ENGINE_PROBE, EngineFailure

    cached, runtime = dev_run
    monkeypatch.setenv("FRA_NORMALIZATION_ENABLED", "0")
    cached.side_effect = OcrEngineError("OCR engine failed")
    with pytest.raises(EngineFailure, match="first 3 documents"):
        extraction.main(["--english-only"])
    assert cached.call_count == ENGINE_PROBE == 3
    assert not runtime.normalize.called
    saved = json.loads((tmp_path / "eval/extraction-golden.json").read_text())
    assert len(saved["errored"]) == 3
    assert "ocr_engine" in saved["aborted"]
