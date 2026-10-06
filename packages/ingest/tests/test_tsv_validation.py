"""Malformed engine output must fail instead of silently dropping words."""

import pytest

from fra_ingest.ocr import OcrEngineError, lines_from_tesseract_tsv

HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)
WORD = "5\t1\t1\t1\t1\t1\t0\t0\t20\t10\t90\tword"


@pytest.mark.parametrize(
    "tsv",
    [
        "level\ttext\n5\tword",
        HEADER.replace("conf", "left") + "\n" + WORD,
        HEADER + "\n" + WORD.rsplit("\t", 1)[0],
        HEADER + "\n" + WORD + "\textra",
        HEADER + "\n" + WORD.replace("90", "nan"),
        HEADER + "\n" + WORD.replace("90", "101"),
        HEADER + "\n" + WORD.replace("20", "-20"),
        HEADER + "\n" + WORD.replace("5\t", "bad\t", 1),
    ],
)
def test_invalid_tsv_is_actionable(tsv: str) -> None:
    with pytest.raises(OcrEngineError, match="tesseract"):
        lines_from_tesseract_tsv(tsv, (100, 100))


def test_header_only_and_nonword_rows_are_valid() -> None:
    assert lines_from_tesseract_tsv(HEADER + "\n", (100, 100)) == []
    nonword = "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t"
    result = lines_from_tesseract_tsv(HEADER + "\n" + nonword + "\n" + WORD, (100, 100))
    assert len(result) == 1 and result[0].text == "word"
