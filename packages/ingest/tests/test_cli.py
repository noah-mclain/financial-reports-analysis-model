"""fra-ingest locate."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from fra_ingest.cli import main
from fra_ingest.results import LocateResult


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
