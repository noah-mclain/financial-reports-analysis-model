"""Cases drawn from the golden set: see eval/golden/README.md for where each convention appears."""

from decimal import Decimal

import pytest

from fra_core.numbers import ParsedNumber, normalize_digits, parse_number, strip_bidi


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Plain and grouped, both separator styles found in the golden set.
        ("1,234", Decimal(1234)),
        ("22,750,342", Decimal(22750342)),
        ("8 864 383 244", Decimal(8864383244)),  # scanned Egyptian filing
        ("2 974 240 332", Decimal(2974240332)),
        ("1 234", Decimal(1234)),  # non-breaking space
        ("1 234", Decimal(1234)),  # narrow no-break space
        ("492514", Decimal(492514)),
        ("1,612,427.50", Decimal("1612427.50")),
        # Negatives.
        ("(1,234)", Decimal(-1234)),
        ("-1,234", Decimal(-1234)),
        ("−1,234", Decimal(-1234)),  # unicode minus
        ("1,234-", Decimal(-1234)),  # OCR of a parenthesised negative
        ("(1 234)", Decimal(-1234)),
        # Printed zero versus absent value.
        ("-", Decimal(0)),
        ("–", Decimal(0)),
        ("—", Decimal(0)),
        ("nil", Decimal(0)),
        # Arabic-Indic digits, comma grouping (Almarai digital Arabic).
        ("٢٢,٧٥٠,٣٤٢", Decimal(22750342)),
        # Arabic-Indic digits, space grouping (scanned Egyptian filing).
        (
            "٨ ٨٦٤ ٣٨٣ ٢٤٤",
            Decimal(8864383244),
        ),
        # Arabic separators.
        ("١٬٢٣٤", Decimal(1234)),  # thousands separator U+066C
        ("١٫٥", Decimal("1.5")),  # decimal separator U+066B
    ],
)
def test_parses_value(text: str, expected: Decimal) -> None:
    assert parse_number(text).value == expected


@pytest.mark.parametrize("text", ["", "   ", None])
def test_absent_value_is_none_not_zero(text: str | None) -> None:
    """An empty cell means the line does not apply to the period. A dash means zero."""
    result = parse_number(text)
    assert result.value is None
    assert "empty" in result.flags


@pytest.mark.parametrize("text", ["n/a", "N/A", "na"])
def test_not_available(text: str) -> None:
    result = parse_number(text)
    assert result.value is None
    assert "not_available" in result.flags


def test_percentage_becomes_a_ratio() -> None:
    result = parse_number("12.5%")
    assert result.value == Decimal("0.125")
    assert result.unit == "ratio"


def test_negative_percentage() -> None:
    result = parse_number("(12.5)%")
    assert result.value == Decimal("-0.125")
    assert result.unit == "ratio"


@pytest.mark.parametrize("text", ["1,234¹", "1,234 (a)", "1,234*"])
def test_footnote_markers_are_stripped(text: str) -> None:
    result = parse_number(text)
    assert result.value == Decimal(1234)
    assert "footnote_marker" in result.flags


def test_flags_record_arabic_digits() -> None:
    result = parse_number("١٢٣")
    assert result.value == Decimal(123)
    assert "arabic_indic_digits" in result.flags


def test_european_separators_are_read_and_flagged() -> None:
    """Both separators present makes the convention unambiguous; the flag surfaces it anyway."""
    result = parse_number("1.234,56")
    assert result.value == Decimal("1234.56")
    assert "european_separators" in result.flags


def test_lone_dot_group_is_flagged_as_ambiguous() -> None:
    """'1.234' could be either convention. The in-scope reading wins, with a flag for review."""
    result = parse_number("1.234")
    assert result.value == Decimal("1.234")
    assert "ambiguous_separator" in result.flags


def test_two_decimal_places_is_not_ambiguous() -> None:
    """A two-digit fraction cannot be a thousands group, so nothing needs flagging.

    Note that "11.234" is as ambiguous as "1.234": both could be grouped thousands.
    """
    result = parse_number("1234.56")
    assert result.value == Decimal("1234.56")
    assert "ambiguous_separator" not in result.flags


@pytest.mark.parametrize("text", ["abc", "—x—", "Note 5 (a)"])
def test_non_numeric_returns_none(text: str) -> None:
    assert parse_number(text).value is None


def test_bidi_controls_are_removed() -> None:
    assert strip_bidi("‫١٢٣‬") == "١٢٣"


def test_bidi_wrapped_number_parses() -> None:
    """Digital Arabic PDFs wrap every run in bidi controls."""
    assert parse_number("‫‪٢٢,٧٥٠‬‬").value == Decimal(22750)


def test_normalize_digits_reports_conversion() -> None:
    text, converted = normalize_digits("۱۲۳")  # Persian digits
    assert (text, converted) == ("123", True)
    assert normalize_digits("123") == ("123", False)


def test_parsed_number_is_hashable_and_frozen() -> None:
    """Cells are cached by value during extraction, so the result must be usable as a key."""
    a = ParsedNumber(Decimal(1), flags=("x",))
    assert {a: 1}[a] == 1
