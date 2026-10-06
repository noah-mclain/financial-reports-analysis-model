"""Running convert in a child process per document (ADR 0007, spec 10)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fra_ingest.child import convert_in_child
from fra_ingest.config import IngestConfig
from fra_ingest.convert import CONVERT_VERSION, settings_hash
from fra_ingest.errors import IngestError
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.pages import sha256_file
from fra_ingest.results import ConvertResult, RangeConversion, RangePlan, RangeStatus


def fake(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def setup(tmp_path: Path, name: str = "doc.pdf") -> tuple[Path, IngestConfig]:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.4 stand-in")
    return pdf, IngestConfig(artifact_root=tmp_path / "artifacts", child_timeout_s=20)


def write_result(config: IngestConfig, pdf: Path, status: RangeStatus) -> ConvertResult:
    sha = sha256_file(pdf)
    plans = [RangePlan(first_page=4, last_page=6, ocr="pdf_aware", ocr_language="en-US")]
    result = ConvertResult(
        version=CONVERT_VERSION,
        sha256=sha,
        locate_version=LOCATE_VERSION,
        docling_version="2.126.0",
        device=config.device,
        settings_hash=settings_hash(config, plans, "2.126.0", LOCATE_VERSION),
        settings_plans=plans,
        ranges=[
            RangeConversion(
                first_page=4, last_page=6, ocr="pdf_aware", ocr_language="en-US", status=status
            )
        ],
        timings={"convert": 1.0},
    )
    out = config.artifact_root / sha
    out.mkdir(parents=True, exist_ok=True)
    (out / "convert.json").write_text(result.model_dump_json(), encoding="utf-8")
    return result


def test_a_child_that_writes_a_result_returns_it_with_its_wall_time(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    written = write_result(config, pdf, "ok")
    result = convert_in_child(pdf, config, command=fake("import sys; sys.exit(0)"))
    assert result.ranges == written.ranges
    assert result.timings["convert"] == 1.0
    assert result.timings["child_wall"] > 0


def test_the_child_gets_the_pdf_artifacts_config_and_extra_args(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    write_result(config, pdf, "ok")
    record = tmp_path / "argv.txt"
    script = f"import sys; open({str(record)!r}, 'w').write('\\n'.join(sys.argv[1:]))"
    convert_in_child(
        pdf,
        config,
        config_path=tmp_path / "ingest.toml",
        extra_args=["--no-cache"],
        command=fake(script),
    )
    assert record.read_text().splitlines() == [
        "convert",
        str(pdf),
        "--artifacts",
        str(config.artifact_root),
        "--config",
        str(tmp_path / "ingest.toml"),
        "--no-cache",
    ]


def test_the_child_is_told_the_engine_the_parent_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf, config = setup(tmp_path)
    write_result(config, pdf, "ok")
    record = tmp_path / "engine.txt"
    script = f"import os; open({str(record)!r}, 'w').write(os.environ['FRA_OCR_ENGINE'])"
    monkeypatch.setenv("FRA_OCR_ENGINE", "ocrmac")
    config = config.model_copy(update={"convert_ocr": "tesseract"})
    (config.artifact_root / sha256_file(pdf) / "convert.json").unlink()
    # Reuse the directory created for the first config.
    write_result(config, pdf, "ok")
    convert_in_child(pdf, config, command=fake(script))
    assert record.read_text() == "tesseract"


def test_every_range_failing_raises_convert_failed(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    write_result(config, pdf, "failed")
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake("import sys; sys.exit(3)"))
    assert caught.value.reason == "convert_failed"
    assert "4-6" in caught.value.detail


def test_a_child_past_its_timeout_is_killed(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    config = config.model_copy(update={"child_timeout_s": 0.5})
    started = time.perf_counter()
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake("import time; time.sleep(30)"))
    assert caught.value.reason == "convert_timeout"
    assert time.perf_counter() - started < 10


def test_a_child_killed_by_a_signal_is_a_crash(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    script = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake(script))
    assert caught.value.reason == "convert_crashed"
    assert caught.value.detail == "signal 9"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("import sys; sys.exit(0)", "exit 0"),
        ("raise ValueError('bad table')", "ValueError: bad table"),
    ],
)
def test_a_child_that_leaves_no_result_is_a_crash(
    tmp_path: Path, script: str, expected: str
) -> None:
    pdf, config = setup(tmp_path)
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake(script))
    assert caught.value.reason == "convert_crashed"
    assert expected in caught.value.detail


@pytest.mark.parametrize("name", ["doc.pdf", "تقرير سنوي 2025.pdf"])
def test_the_childs_own_reason_is_kept(tmp_path: Path, name: str) -> None:
    pdf, config = setup(tmp_path, name)
    script = (
        "import sys; print('warming up', file=sys.stderr); "
        "print(f'{sys.argv[2]}: encrypted_pdf needs a password', file=sys.stderr); sys.exit(2)"
    )
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake(script))
    assert caught.value.reason == "encrypted_pdf"


def test_a_missing_pdf_is_refused_before_starting_a_child(tmp_path: Path) -> None:
    _, config = setup(tmp_path)
    with pytest.raises(IngestError) as caught:
        convert_in_child(tmp_path / "missing.pdf", config, command=fake("raise SystemExit(9)"))
    assert caught.value.reason == "unreadable_pdf"


@pytest.mark.slow
@pytest.mark.golden
def test_almarai_converts_in_a_real_child(golden: Callable[[str], Path], tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    result = convert_in_child(golden("almarai-2025-en-annualreport.pdf"), config)
    assert result.ranges
    assert all(r.status == "ok" for r in result.ranges)
    assert result.peak_footprint_gb is not None and result.peak_footprint_gb > 0.3
    assert all((tmp_path / result.sha256 / p).is_file() for p in result.page_images.values())


def test_nondefault_effective_configuration_reaches_child(tmp_path: Path) -> None:
    import json

    pdf, config = setup(tmp_path)
    config = config.model_copy(
        update={
            "device": "cpu",
            "ocr_scale": 4.5,
            "tesseract_psm": 6,
            "tesseract_arabic_language": "ara",
            "convert_ocr": "tesseract",
        }
    )
    write_result(config, pdf, "ok")
    record = tmp_path / "effective.json"
    script = (
        f"import os; open({str(record)!r}, 'w').write(os.environ['FRA_INGEST_EFFECTIVE_CONFIG'])"
    )
    convert_in_child(pdf, config, command=fake(script))
    assert IngestConfig.model_validate(json.loads(record.read_text())) == config


def test_child_settings_digest_mismatch_is_refused(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    write_result(config, pdf, "ok")
    config = config.model_copy(update={"ocr_scale": 4.5})
    with pytest.raises(IngestError, match=r"settings.*mismatch"):
        convert_in_child(pdf, config, command=fake("pass"))


@pytest.mark.parametrize("version_field", ["docling_version", "locate_version"])
def test_stale_child_stage_versions_are_refused(tmp_path: Path, version_field: str) -> None:
    pdf, config = setup(tmp_path)
    result = write_result(config, pdf, "ok")
    result = result.model_copy(update={version_field: "obsolete"})
    assert result.settings_plans is not None
    result = result.model_copy(
        update={
            "settings_hash": settings_hash(
                config, result.settings_plans, result.docling_version, result.locate_version
            )
        }
    )
    (config.artifact_root / sha256_file(pdf) / "convert.json").write_text(result.model_dump_json())
    with pytest.raises(IngestError, match="version mismatch"):
        convert_in_child(pdf, config, command=fake("pass"))
