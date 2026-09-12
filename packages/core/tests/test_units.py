"""Scale and currency strings taken from the golden set documents."""

import pytest

from fra_core.units import detect_currency, detect_scale


@pytest.mark.parametrize(
    ("text", "scale"),
    [
        ("SAR '000", 1_000),
        ("X '000", 1_000),  # Almarai English: the riyal glyph does not reach the text layer
        ("بآالف X", 1_000),  # Almarai Arabic: the PDF mangles بآلاف
        ("بآلاف الريالات السعودية", 1_000),
        ("(in thousands of Egyptian pounds)", 1_000),
        ("EGP thousands", 1_000),
        ("USD millions", 1_000_000),
        ("(in millions)", 1_000_000),
        ("بالملايين", 1_000_000),
        ("in billions", 1_000_000_000),
    ],
)
def test_detects_scale(text: str, scale: int) -> None:
    signal = detect_scale(text)
    assert signal is not None
    assert signal.scale == scale


def test_million_is_not_read_as_thousand() -> None:
    """Both words contain a scale cue; the larger unit has to win."""
    signal = detect_scale("in millions of thousands of units")
    assert signal is not None
    assert signal.scale == 1_000_000


def test_absent_scale_wording_returns_none() -> None:
    """No wording is different from figures being in units."""
    assert detect_scale("Consolidated Statement of Financial Position") is None


def test_per_share_exemption_is_flagged() -> None:
    signal = detect_scale("in millions, except per share data")
    assert signal is not None
    assert signal.per_share_exempt is True


def test_scale_without_readable_currency() -> None:
    """Almarai's header gives the scale but not the currency."""
    signal = detect_scale("X '000")
    assert signal is not None
    assert signal.currency is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("SAR '000", "SAR"),
        ("in thousands of Saudi Riyals", "SAR"),
        ("بآلاف الريالات السعودية", "SAR"),
        ("(in thousands of Egyptian pounds)", "EGP"),
        ("جنيه مصري", "EGP"),
        ("LE", "EGP"),
        ("EGP", "EGP"),
        ("U.S. Dollars", "USD"),
        ("USD millions", "USD"),
    ],
)
def test_detects_currency(text: str, code: str) -> None:
    assert detect_currency(text) == code


@pytest.mark.parametrize("text", ["", "   ", "Total assets"])
def test_no_currency_found(text: str) -> None:
    assert detect_currency(text) is None


@pytest.mark.parametrize("text", ["Net sales for the year", "scale", "Leases", "settlement"])
def test_currency_abbreviations_need_word_boundaries(text: str) -> None:
    """'LE' abbreviates Egyptian pounds, but it also sits inside ordinary words."""
    assert detect_currency(text) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("بآلاف الريالات السعودية", "SAR"),  # plural with the definite article
        ("بالريال السعودي", "SAR"),  # preposition attached to the stem
        ("بالجنيه المصري", "EGP"),
        ("بآلاف الجنيهات المصرية", "EGP"),
    ],
)
def test_arabic_currency_words_carry_prefixes_and_suffixes(text: str, code: str) -> None:
    """Arabic attaches ال and plural endings to the stem, so boundary matching cannot apply."""
    assert detect_currency(text) == code
