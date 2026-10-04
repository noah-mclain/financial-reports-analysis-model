"""Tesseract behind the OcrEngine protocol. Parsing is tested on canned engine output and a
stand-in executable; the real engine runs only in the slow tests."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from fra_ingest import ocr
from fra_ingest.config import IngestConfig
from fra_ingest.ocr import (
    OcrEngineError,
    OcrLine,
    OcrTimeoutError,
    OcrUnavailableError,
    TesseractOcr,
    VisionOcr,
    engine_language,
    lines_from_tesseract_tsv,
    make_engine,
)

HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)
# A 1000 x 500 page: a body line, a header line below it in the file, and one Arabic line whose
# words come in reading order (right to left) with a right-to-left mark after the last.
TSV = "\n".join(
    [
        HEADER,
        "1\t1\t0\t0\t0\t0\t0\t0\t1000\t500\t-1\t",
        "4\t1\t1\t1\t2\t0\t100\t250\t300\t50\t-1\t",
        "5\t1\t1\t1\t2\t1\t100\t250\t100\t50\t90.0\tTotal",
        "5\t1\t1\t1\t2\t2\t220\t255\t180\t40\t80.0\tassets",
        "4\t1\t1\t1\t1\t0\t500\t50\t300\t25\t-1\t",
        "5\t1\t1\t1\t1\t1\t700\t50\t100\t25\t70.0\tوزر",
        "5\t1\t1\t1\t1\t2\t500\t50\t150\t25\t90.0\tزور‏",
        "5\t1\t1\t1\t3\t1\t10\t400\t50\t20\t-1\t",
        "5\t1\t1\t1\t3\t2\t10\t400\t50\t20\t95.0\t  ",
    ]
)


def test_tesseract_words_become_lines_in_reading_order_as_page_fractions() -> None:
    lines = lines_from_tesseract_tsv(TSV, (1000, 500))
    assert [line.text for line in lines] == ["وزر زور", "Total assets"]
    arabic, english = lines
    assert (arabic.left, arabic.top, arabic.width, arabic.height) == pytest.approx(
        (0.5, 0.1, 0.3, 0.05)
    )
    assert english.left == pytest.approx(0.1)
    assert english.top == pytest.approx(0.5)
    assert english.bottom == pytest.approx(0.6)
    assert english.width == pytest.approx(0.3)


def test_tesseract_line_confidence_is_the_mean_word_confidence_as_a_fraction() -> None:
    arabic, english = lines_from_tesseract_tsv(TSV, (1000, 500))
    assert arabic.confidence == pytest.approx(0.8)
    assert english.confidence == pytest.approx(0.85)


def test_tesseract_output_without_words_is_no_lines() -> None:
    assert lines_from_tesseract_tsv(HEADER + "\n", (10, 10)) == []


def test_tesseract_output_that_is_not_tsv_is_refused() -> None:
    with pytest.raises(OcrEngineError, match="tesseract"):
        lines_from_tesseract_tsv("not a table", (10, 10))


@pytest.mark.parametrize(
    ("engine", "arabic", "language", "code"),
    [
        ("tesseract", "ara", "ar-SA", "ara"),
        ("tesseract", "ara+eng", "ar-SA", "ara+eng"),
        ("tesseract", "ara+eng", "en-US", "eng"),
        ("ocrmac", "ara", "ar-SA", "ar-SA"),
    ],
)
def test_our_language_codes_map_to_each_engines_own(
    engine: str, arabic: str, language: str, code: str
) -> None:
    config = IngestConfig.model_validate(
        {"convert_ocr": engine, "tesseract_arabic_language": arabic}
    )
    assert engine_language(config, language) == code


def test_an_unmapped_language_names_itself() -> None:
    with pytest.raises(ValueError, match="fr-FR"):
        engine_language(IngestConfig(convert_ocr="tesseract"), "fr-FR")


TESSERACT = IngestConfig(convert_ocr="tesseract")
ENGLISH_ONLY = TESSERACT.model_copy(update={"ocr_languages": ("en-US",)})


def test_tesseract_without_its_binary_is_unavailable() -> None:
    with pytest.raises(OcrUnavailableError, match="no-such-tesseract"):
        TesseractOcr(TESSERACT, command="no-such-tesseract")


def fake_tesseract(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    languages: Sequence[str] = ("ara", "eng"),
    sleep_s: int = 0,
) -> str:
    """A stand-in executable: it lists ``languages`` and prints canned TSV for a read, after
    ``sleep_s`` seconds. The arguments of a read are written to ``args.txt``."""
    script = tmp_path / "tesseract"
    tsv = tmp_path / "out.tsv"
    tsv.write_text(TSV, encoding="utf-8")
    listing = "\\n".join(languages)
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--list-langs" ]; then printf "List of available languages ({len(languages)}):'
        f'\\n{listing}\\n"; exit 0; fi\n'
        f'echo "$*" > "{tmp_path}/args.txt"\nsleep {sleep_s}\ncat "{tsv}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def test_tesseract_runs_the_cli_and_parses_its_tsv(tmp_path: Path) -> None:
    engine = TesseractOcr(TESSERACT, command=fake_tesseract(tmp_path))
    lines = engine.recognize(Image.new("RGB", (1000, 500), "white"), ("en-US",))
    assert [line.text for line in lines] == ["وزر زور", "Total assets"]


def test_tesseract_gets_the_language_page_mode_and_resolution_of_the_settings(
    tmp_path: Path,
) -> None:
    config = IngestConfig(
        convert_ocr="tesseract", tesseract_psm=6, tesseract_arabic_language="ara+eng", ocr_dpi=150
    )
    engine = TesseractOcr(config, command=fake_tesseract(tmp_path))
    engine.recognize(Image.new("RGB", (10, 10), "white"), ("ar-SA",))
    args = (tmp_path / "args.txt").read_text(encoding="utf-8").split()
    assert args == ["stdin", "stdout", "-l", "ara+eng", "--psm", "6", "--dpi", "150", "tsv"]


def test_a_tesseract_that_runs_too_long_fails_loudly(tmp_path: Path) -> None:
    engine = TesseractOcr(TESSERACT, command=fake_tesseract(tmp_path, sleep_s=2), timeout_s=0.5)
    with pytest.raises(OcrTimeoutError, match=r"0\.5 s"):
        engine.recognize(Image.new("RGB", (10, 10), "white"), ("en-US",))


def test_tesseract_without_the_language_data_is_unavailable(tmp_path: Path) -> None:
    engine = TesseractOcr(ENGLISH_ONLY, command=fake_tesseract(tmp_path, languages=("eng",)))
    with pytest.raises(OcrUnavailableError, match="ara"):
        engine.recognize(Image.new("RGB", (10, 10), "white"), ("ar-SA",))


def test_a_failing_tesseract_run_is_an_error_not_an_empty_page(tmp_path: Path) -> None:
    engine = TesseractOcr(TESSERACT, command=fake_tesseract(tmp_path, exit_code=1))
    with pytest.raises(OcrEngineError, match="exit 1"):
        engine.recognize(Image.new("RGB", (10, 10), "white"), ("en-US",))


def test_vision_without_ocrmac_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    with pytest.raises(OcrUnavailableError, match="ocrmac"):
        VisionOcr()


def test_no_engine_is_chosen_by_the_none_setting() -> None:
    assert make_engine(IngestConfig(convert_ocr="none")) is None


def test_the_engine_follows_the_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    with pytest.raises(OcrUnavailableError, match="ocrmac"):
        make_engine(IngestConfig(convert_ocr="ocrmac"))
    monkeypatch.setattr(ocr, "TESSERACT_COMMAND", fake_tesseract(tmp_path))
    engine = make_engine(TESSERACT)
    assert engine is not None
    assert engine.name == "tesseract"


def test_a_tesseract_without_arabic_data_is_refused_when_the_engine_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "TESSERACT_COMMAND", fake_tesseract(tmp_path, languages=("eng",)))
    with pytest.raises(OcrUnavailableError, match="ara"):
        make_engine(TESSERACT)


def test_a_language_the_engine_cannot_map_is_refused_when_the_engine_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr, "TESSERACT_COMMAND", fake_tesseract(tmp_path))
    config = TESSERACT.model_copy(update={"ocr_languages": ("fr-FR",)})
    with pytest.raises(ValueError, match="fr-FR"):
        make_engine(config)


# ---- slow: the installed engine reads a rendered image ----

FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
ARABIC_WORDS = ("دور", "ورد", "دار", "زور", "وزر")


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    pytest.skip("no font with Arabic letters found")


def _english_image() -> Image.Image:
    image = Image.new("RGB", (900, 220), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), "Total assets 1,234,567", font=_font(40), fill="black")
    draw.text((20, 110), "Profit for the year 89,012", font=_font(40), fill="black")
    return image


def _arabic_image() -> Image.Image:
    """Words made only of letters that never join the next, so each reads the same shaped or
    not (Pillow here has no shaping), drawn right to left by reversing each word."""
    image = Image.new("RGB", (900, 120), "white")
    ImageDraw.Draw(image).text(
        (20, 20), " ".join(word[::-1] for word in ARABIC_WORDS), font=_font(40), fill="black"
    )
    return image


def _read(lines: Sequence[OcrLine]) -> str:
    return " ".join(line.text for line in lines)


@pytest.mark.slow
def test_tesseract_reads_english_in_reading_order() -> None:
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    lines = TesseractOcr(TESSERACT).recognize(_english_image(), ("en-US",))
    assert lines[0].text.startswith("Total assets")
    assert "1,234,567" in _read(lines)
    assert "89,012" in _read(lines)
    assert lines[0].top < lines[1].top
    assert all(0 <= line.left < 1 and 0 < line.bottom <= 1 for line in lines)


@pytest.mark.slow
def test_tesseract_reads_arabic() -> None:
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    lines = TesseractOcr(TESSERACT).recognize(_arabic_image(), ("ar-SA",))
    assert sum(word in _read(lines) for word in ARABIC_WORDS) >= 4
