"""Each harness records a document the OCR engine failed on and goes on, and a run whose first
documents all fail on the engine stops after writing the report of the rows it has."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import harness.convert as convert_module
import harness.gates as gates_module
import harness.locate as locate_module
import harness.mapping as mapping_module
import harness.structure as structure_module
import pytest
import yaml
from harness.failures import ENGINE_PROBE, EngineFailure
from harness.holdout_records import LOG_HEADER
from test_convert_harness import result as convert_result

from fra_core.schemas import Document, StatementType
from fra_core.split import Part, hashed_part, issuer_key
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngineError, OcrTimeoutError
from fra_ingest.results import IndustrySignal, LocateResult, StatementRange, StructureResult

IDS = ["d1", "d2", "d3", "d4", "d5"]
DETAIL = "tesseract did not finish within 120.0 s"
RAISED = {"timeout": OcrTimeoutError, "engine": OcrEngineError}
REASON = {"timeout": "ocr_timeout", "engine": "ocr_engine"}

# Which documents fail, and how. A document not named here is read.
Failures = dict[str, Exception]


def one_failure(error: str) -> Failures:
    return {"d2": RAISED[error](DETAIL)}


def engine_down() -> Failures:
    return {i: OcrEngineError(DETAIL) for i in IDS[:ENGINE_PROBE]}


def config_in(tmp_path: Path, **extra: Any) -> IngestConfig:
    return IngestConfig(artifact_root=tmp_path / "artifacts", **extra)


def write_manifest(tmp_path: Path, **fields: Any) -> Path:
    manifest = tmp_path / "golden" / "manifest.yaml"
    manifest.parent.mkdir()
    documents = [
        {"id": i, "file": f"{i}.pdf", "language": "en", "scale": 1, "currency": "SAR", **fields}
        for i in IDS
    ]
    manifest.write_text(yaml.safe_dump({"documents": documents}), encoding="utf-8")
    return manifest


def structured(config: IngestConfig, stem: str) -> StructureResult:
    sha = stem.ljust(64, "0")
    out = config.artifact_root / sha
    out.mkdir(parents=True, exist_ok=True)
    (out / "table_checks.json").write_text("[]", encoding="utf-8")
    return StructureResult(
        version="2", sha256=sha, convert_version="1", settings_hash="", statements=[]
    )


def structure_fake(
    tmp_path: Path, config: IngestConfig, failures: Failures, seen: list[str]
) -> Callable[..., StructureResult]:
    def fake(pdf: Path, *_args: object, **_kwargs: object) -> StructureResult:
        seen.append(pdf.stem)
        if pdf.stem in failures:
            raise failures[pdf.stem]
        return structured(config, pdf.stem)

    return fake


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_failed_with(row: dict[str, Any], reason: str, detail: str = DETAIL) -> None:
    assert row["id"] == "d2"
    assert row["reason"] == reason
    assert row["detail"] == detail


# ---- structure --------------------------------------------------------------------------------


def run_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: Failures, seen: list[str]
) -> Path:
    config = config_in(tmp_path, enabled_types=(StatementType.BALANCE,))
    monkeypatch.setattr(structure_module, "MANIFEST", write_manifest(tmp_path))
    monkeypatch.setattr(structure_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(structure_module, "load_config", lambda: config)
    monkeypatch.setattr(structure_module, "make_engine", lambda _config: None)
    monkeypatch.setattr(structure_module, "PAIRS", ())
    monkeypatch.setattr(
        structure_module, "structure_pdf", structure_fake(tmp_path, config, failures, seen)
    )
    structure_module.main([])
    return tmp_path / "eval" / "structure-golden.json"


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_structure_records_an_ocr_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    seen: list[str] = []
    report = read(run_structure(tmp_path, monkeypatch, one_failure(error), seen))
    assert seen == IDS
    assert [r["id"] for r in report["documents"]] == IDS
    failed = report["documents"][1]
    assert_failed_with(failed, REASON[error])
    assert failed["error"] == f"{REASON[error]} {DETAIL}"
    assert "aborted" not in report


def test_structure_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    with pytest.raises(EngineFailure, match="ocr_engine"):
        run_structure(tmp_path, monkeypatch, engine_down(), seen)
    report = read(tmp_path / "eval" / "structure-golden.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [r["id"] for r in report["documents"]] == IDS[:ENGINE_PROBE]
    assert "ocr_engine" in report["aborted"]


# ---- gates ------------------------------------------------------------------------------------


def run_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: Failures, seen: list[str]
) -> Path:
    config = config_in(tmp_path)
    monkeypatch.setattr(gates_module, "MANIFEST", write_manifest(tmp_path))
    monkeypatch.setattr(gates_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(gates_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gates_module, "load_config", lambda: config)
    monkeypatch.setattr(gates_module, "make_engine", lambda _config: None)
    monkeypatch.setattr(
        gates_module, "structure_pdf", structure_fake(tmp_path, config, failures, seen)
    )
    gates_module.main([])
    return tmp_path / "eval" / "gates-dev.json"


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_gates_records_an_ocr_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    seen: list[str] = []
    report = read(run_gates(tmp_path, monkeypatch, one_failure(error), seen))
    assert seen == IDS
    assert report["errors"] == [f"d2: {REASON[error]}"]
    assert_failed_with(report["errored"][0], REASON[error])
    assert len(report["documents"]) == len(IDS) - 1


def test_gates_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    with pytest.raises(EngineFailure, match="ocr_engine"):
        run_gates(tmp_path, monkeypatch, engine_down(), seen)
    report = read(tmp_path / "eval" / "gates-dev.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [e["id"] for e in report["errored"]] == IDS[:ENGINE_PROBE]
    assert "ocr_engine" in report["aborted"]


# ---- mapping, golden --------------------------------------------------------------------------


def run_mapping_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: Failures, seen: list[str]
) -> Path:
    config = config_in(tmp_path)
    (tmp_path / "expected").mkdir()
    monkeypatch.setattr(mapping_module, "MANIFEST", write_manifest(tmp_path))
    monkeypatch.setattr(mapping_module, "EXPECTED_DIR", tmp_path / "expected")
    monkeypatch.setattr(mapping_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(mapping_module, "load_config", lambda: config)
    monkeypatch.setattr(mapping_module, "make_engine", lambda _config: None)
    monkeypatch.setattr(
        mapping_module, "structure_pdf", structure_fake(tmp_path, config, failures, seen)
    )
    mapping_module.run_golden()
    return tmp_path / "eval" / "mapping-golden.json"


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_mapping_golden_records_an_ocr_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    seen: list[str] = []
    report = read(run_mapping_golden(tmp_path, monkeypatch, one_failure(error), seen))
    assert seen == IDS
    assert report["failures"] == [f"d2: {REASON[error]}"]
    assert_failed_with(report["errored"][0], REASON[error])
    assert [d["id"] for d in report["documents"]] == ["d1", "d3", "d4", "d5"]


def test_mapping_golden_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    with pytest.raises(EngineFailure, match="ocr_engine"):
        run_mapping_golden(tmp_path, monkeypatch, engine_down(), seen)
    report = read(tmp_path / "eval" / "mapping-golden.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [e["id"] for e in report["errored"]] == IDS[:ENGINE_PROBE]
    assert "ocr_engine" in report["aborted"]


# ---- mapping, fit -----------------------------------------------------------------------------


def fit_records(tmp_path: Path) -> dict[str, Path]:
    issuers = [
        n for n in (f"Issuer {k}" for k in range(300)) if hashed_part(issuer_key(n)) is Part.FIT
    ][: len(IDS)]
    records = [
        {
            "id": i,
            "issuer": issuer,
            "pool": "train",
            "role": "corporate",
            "period": "annual",
            "language": "en",
        }
        for i, issuer in zip(IDS, issuers, strict=True)
    ]
    store = tmp_path / "store"
    (store / "train").mkdir(parents=True)
    for r in records:
        (store / "train" / f"{r['id']}.pdf").write_bytes(b"x")
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"documents": records}), encoding="utf-8")
    (tmp_path / "m.yaml").write_text(yaml.safe_dump({"moves": []}), encoding="utf-8")
    (tmp_path / "l.tsv").write_text(LOG_HEADER, encoding="utf-8")
    return {
        "candidates": tmp_path / "c.yaml",
        "moves": tmp_path / "m.yaml",
        "log": tmp_path / "l.tsv",
        "store": store,
    }


def run_mapping_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: Failures, seen: list[str]
) -> Path:
    config = config_in(tmp_path)
    monkeypatch.setattr(mapping_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(mapping_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mapping_module, "load_config", lambda: config)
    monkeypatch.setattr(mapping_module, "make_engine", lambda _config: None)
    monkeypatch.setattr(
        mapping_module, "structure_pdf", structure_fake(tmp_path, config, failures, seen)
    )
    mapping_module.run_fit(None, **fit_records(tmp_path))
    return tmp_path / "eval" / "mapping-fit.json"


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_mapping_fit_records_an_ocr_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    seen: list[str] = []
    report = read(run_mapping_fit(tmp_path, monkeypatch, one_failure(error), seen))
    assert seen == IDS
    assert_failed_with(report["errored"][0], REASON[error])
    assert [d["id"] for d in report["documents"]] == ["d1", "d3", "d4", "d5"]
    assert "aborted" not in report


def test_mapping_fit_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    with pytest.raises(EngineFailure, match="ocr_engine"):
        run_mapping_fit(tmp_path, monkeypatch, engine_down(), seen)
    report = read(tmp_path / "eval" / "mapping-fit.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [e["id"] for e in report["errored"]] == IDS[:ENGINE_PROBE]
    assert "ocr_engine" in report["aborted"]


# ---- convert ----------------------------------------------------------------------------------


def run_convert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: Failures, seen: list[str]
) -> Path:
    config = config_in(tmp_path)

    def fake(pdf: Path, *_args: object, **_kwargs: object) -> Any:
        seen.append(pdf.stem)
        if pdf.stem in failures:
            raise failures[pdf.stem]
        return convert_result(["ok"])

    monkeypatch.setattr(convert_module, "MANIFEST", write_manifest(tmp_path))
    monkeypatch.setattr(convert_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(convert_module, "load_config", lambda: config)
    monkeypatch.setattr(convert_module, "convert_in_child", fake)
    convert_module.main([])
    return tmp_path / "eval" / "convert-golden.json"


def child_failures(failures: Failures) -> Failures:
    """What the parent of a convert child sees: the child's OCR error as an ingest error."""
    return {
        i: IngestError("ocr_timeout" if isinstance(e, OcrTimeoutError) else "ocr_engine", str(e))
        for i, e in failures.items()
    }


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_convert_records_an_ocr_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    seen: list[str] = []
    report = read(run_convert(tmp_path, monkeypatch, child_failures(one_failure(error)), seen))
    assert seen == IDS
    assert [r["id"] for r in report["rows"]] == IDS
    failed = report["rows"][1]
    assert_failed_with(failed, REASON[error])
    assert failed["error"] == f"{REASON[error]} {DETAIL}"


def test_convert_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    with pytest.raises(EngineFailure, match="ocr_engine"):
        run_convert(tmp_path, monkeypatch, child_failures(engine_down()), seen)
    report = read(tmp_path / "eval" / "convert-golden.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [r["id"] for r in report["rows"]] == IDS[:ENGINE_PROBE]
    assert all(r["reason"] == "ocr_engine" for r in report["rows"])
    assert "ocr_engine" in report["aborted"]


def test_convert_timeout_detail_reaches_the_report_and_the_printed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    detail = "d.pdf: still running after 900 s; last progress: convert pp. 58-60 start (at 61.9 s)"
    failures: Failures = {i: IngestError("convert_timeout", detail) for i in IDS[:ENGINE_PROBE]}
    report = read(run_convert(tmp_path, monkeypatch, failures, []))
    # A timeout is a per-document failure: the same three in a row do not stop the run.
    assert [r["id"] for r in report["rows"]] == IDS
    assert "aborted" not in report
    assert report["rows"][0]["detail"] == detail
    assert f"ERROR convert_timeout {detail}" in capsys.readouterr().out


# ---- locate, golden ---------------------------------------------------------------------------


def run_locate_golden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: Failures, seen: list[str]
) -> Path:
    config = config_in(tmp_path)

    def fake(pdf: Path, *_args: object, **_kwargs: object) -> LocateResult:
        seen.append(pdf.stem)
        if pdf.stem in failures:
            raise failures[pdf.stem]
        return LocateResult(
            version="1",
            document=Document(sha256="0" * 64, filename="x.pdf", page_count=20),
            pages=[],
            ranges=[
                StatementRange(
                    type=StatementType.BALANCE, first_page=2, last_page=2, score=8, rank=1
                )
            ],
            convert_ranges=[],
            industry=IndustrySignal(kind="corporate"),
        )

    manifest = write_manifest(tmp_path, statement_pages={"financial_position": [2, 2]})
    monkeypatch.setattr(locate_module, "MANIFEST", manifest)
    monkeypatch.setattr(locate_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(locate_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(locate_module, "load_config", lambda: config)
    monkeypatch.setattr(locate_module, "locate_pdf", fake)
    locate_module.main(["golden", "--no-ocr"])
    return tmp_path / "eval" / "locate-golden.json"


@pytest.mark.parametrize("error", ["timeout", "engine"])
def test_locate_golden_records_an_ocr_failure_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    seen: list[str] = []
    report = read(run_locate_golden(tmp_path, monkeypatch, one_failure(error), seen))
    assert seen == IDS
    assert [r["id"] for r in report["documents"]] == IDS
    failed = report["documents"][1]
    assert failed["id"] == "d2"
    assert failed["error"] == REASON[error]
    assert failed["detail"] == DETAIL
    assert report["median_share"] == 0.0


def test_locate_golden_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    with pytest.raises(EngineFailure, match="ocr_engine"):
        run_locate_golden(tmp_path, monkeypatch, engine_down(), seen)
    report = read(tmp_path / "eval" / "locate-golden.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [r["id"] for r in report["documents"]] == IDS[:ENGINE_PROBE]
    assert "ocr_engine" in report["aborted"]


# ---- locate, pools ----------------------------------------------------------------------------


def test_locate_pool_aborts_on_engine_failure_after_writing_a_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = fit_records(tmp_path)
    original = locate_module.run_pool
    seen: list[str] = []

    def broken(pdf: Path, *_args: object, **_kwargs: object) -> LocateResult:
        seen.append(pdf.stem)
        raise OcrEngineError(DETAIL)

    monkeypatch.setattr(locate_module, "locate_pdf", broken)
    monkeypatch.setattr(locate_module, "load_config", lambda: config_in(tmp_path))
    monkeypatch.setattr(locate_module, "OUT", tmp_path / "eval")
    monkeypatch.setattr(locate_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        locate_module,
        "run_pool",
        lambda pool, no_ocr, limit: original(pool, no_ocr, limit, **records),
    )
    with pytest.raises(EngineFailure, match="ocr_engine"):
        locate_module.main(["train", "--no-ocr"])
    report = read(tmp_path / "eval" / "locate-train.json")
    assert seen == IDS[:ENGINE_PROBE]
    assert [r["id"] for r in report["documents"]] == IDS[:ENGINE_PROBE]
    assert all(r["error"] == "ocr_engine" and r["detail"] == DETAIL for r in report["documents"])
    assert "ocr_engine" in report["aborted"]
