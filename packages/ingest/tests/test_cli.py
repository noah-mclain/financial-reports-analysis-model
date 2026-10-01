"""fra-ingest locate."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from support import make_blank_pdf

from fra_ingest import cli
from fra_ingest.cli import main
from fra_ingest.results import ConvertResult, LocateResult, RangeConversion, StructureResult


@pytest.mark.golden
def test_locate_writes_a_valid_result(
    golden: Callable[[str], Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "locate",
            str(golden("juhayna-2025-en-standalone.pdf")),
            "--no-ocr",
            "--artifacts",
            str(tmp_path),
            "--json",
        ]
    )
    assert code == 0
    result = LocateResult.model_validate(json.loads(capsys.readouterr().out))
    assert "image_pages_not_read:3" in result.flags
    assert (tmp_path / result.document.sha256 / "locate.json").exists()


def test_an_unreadable_file_exits_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    assert main(["locate", str(path), "--no-ocr", "--artifacts", str(tmp_path)]) == 2
    assert "unreadable_pdf" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_a_path_that_is_not_a_file_exits_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    path = tmp_path / "nothing.pdf"
    if kind == "directory":
        path.mkdir()
    assert main(["locate", str(path), "--no-ocr", "--artifacts", str(tmp_path / "a")]) == 2
    assert "unreadable_pdf" in capsys.readouterr().err


def canned(sha256: str, status: str) -> ConvertResult:
    return ConvertResult.model_validate(
        {
            "version": "1",
            "sha256": sha256,
            "locate_version": "2",
            "docling_version": "2.126.0",
            "device": "mps",
            "settings_hash": "h",
            "ranges": [
                RangeConversion.model_validate(
                    {
                        "first_page": 1,
                        "last_page": 1,
                        "ocr": "full_page",
                        "ocr_language": "en-US",
                        "status": status,
                    }
                )
            ],
            "peak_footprint_gb": 1.2,
        }
    )


@pytest.mark.parametrize(("status", "code"), [("ok", 0), ("partial", 0), ("failed", 3)])
def test_convert_exits_by_how_the_ranges_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    code: int,
) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf")
    seen: dict[str, object] = {}

    def fake_convert(
        pdf_path: Path, located: object, languages: object, config: object, **kwargs: object
    ) -> ConvertResult:
        seen.update(kwargs)
        return canned(located.document.sha256, status)  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "convert_pdf", fake_convert)
    argv = ["convert", str(pdf), "--no-ocr", "--artifacts", str(tmp_path / "a"), "--json"]
    assert main(argv) == code
    assert ConvertResult.model_validate_json(capsys.readouterr().out).ranges[0].status == status
    assert seen["use_cache"] is True
    assert "locate" in seen["timings"]  # type: ignore[operator]


def test_convert_no_cache_reconverts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf")
    seen: dict[str, object] = {}

    def fake_convert(
        pdf_path: Path, located: object, languages: object, config: object, **kwargs: object
    ) -> ConvertResult:
        seen.update(kwargs)
        return canned(located.document.sha256, "ok")  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "convert_pdf", fake_convert)
    main(["convert", str(pdf), "--no-ocr", "--no-cache", "--artifacts", str(tmp_path / "a")])
    assert seen["use_cache"] is False


def test_convert_of_an_unreadable_file_exits_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "تقرير سنوي.pdf"
    path.write_bytes(b"not a pdf")
    assert main(["convert", str(path), "--no-ocr", "--artifacts", str(tmp_path / "a")]) == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith(f"{path}: unreadable_pdf")


def test_structure_writes_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf")

    def fake_structure(
        pdf_path: Path, config: object, ocr: object, **kwargs: object
    ) -> StructureResult:
        return StructureResult(
            version="1",
            sha256="c" * 64,
            convert_version="1",
            settings_hash="h",
            flags=["statement_not_extracted:balance"],
        )

    monkeypatch.setattr(cli, "structure_pdf", fake_structure)
    assert (
        main(["structure", str(pdf), "--no-ocr", "--artifacts", str(tmp_path / "a"), "--json"]) == 0
    )
    assert StructureResult.model_validate_json(capsys.readouterr().out).flags == [
        "statement_not_extracted:balance"
    ]


def test_review_report_of_an_unknown_document_exits_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["review-report", "ee", "--artifacts", str(tmp_path)]) == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith("ee: unknown_document")


def test_review_report_prints_where_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    written = tmp_path / "ab" / "review.html"

    def fake_report(sha256: str, config: object) -> Path:
        assert sha256 == "ab"
        return written

    monkeypatch.setattr(cli, "write_review_report", fake_report)
    assert main(["review-report", "ab", "--artifacts", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == str(written)
