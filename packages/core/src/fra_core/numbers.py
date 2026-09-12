"""Parse a number as printed in a financial statement.

Input is one table cell, English or Arabic, from a text layer or OCR. Every assumption made
along the way goes into ``flags``: reading "1.234" as a decimal instead of a thousands group
is off by 1000x.

Conventions seen in the golden set (see eval/golden/README.md):

- Arabic editions use Arabic-Indic digits: ``٢٢,٧٥٠,٣٤٢`` in digital PDFs and
  ``٨ ٨٦٤ ٣٨٣ ٢٤٤`` in scanned Egyptian filings, so both comma and space grouping occur.
- Negatives are printed in parentheses, and OCR sometimes yields a trailing hyphen instead.
- A dash alone means zero; an empty cell means the line does not apply to that period.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal

__all__ = ["ParsedNumber", "normalize_digits", "parse_number", "strip_bidi"]

# Bidirectional formatting characters. They carry no numeric meaning and would break parsing.
_BIDI_CONTROLS = frozenset(
    [
        "‎",  # left-to-right mark
        "‏",  # right-to-left mark
        "؜",  # Arabic letter mark
        "‪",  # left-to-right embedding
        "‫",  # right-to-left embedding
        "‬",  # pop directional formatting
        "‭",  # left-to-right override
        "‮",  # right-to-left override
        "⁦",  # left-to-right isolate
        "⁧",  # right-to-left isolate
        "⁨",  # first strong isolate
        "⁩",  # pop directional isolate
    ]
)

# Space characters used as thousands separators in printed statements.
_SPACE_SEPARATORS = frozenset(
    [
        " ",
        " ",  # no-break space
        " ",  # thin space
        " ",  # narrow no-break space
        " ",  # figure space
    ]
)

# U+0660..U+0669 Arabic-Indic, U+06F0..U+06F9 Extended Arabic-Indic (Persian).
_ARABIC_INDIC_ZERO = 0x0660
_EXTENDED_ARABIC_INDIC_ZERO = 0x06F0

_ARABIC_DECIMAL_SEPARATOR = "٫"
_ARABIC_THOUSANDS_SEPARATOR = "٬"
_PERCENT_SIGNS = ("٪", "%")

# A dash alone in a value column means zero.
_DASHES = frozenset("-‐‑‒–—―−")

_NOT_AVAILABLE_WORDS = frozenset({"n/a", "na", "n.a.", "غير متاح", "لا ينطبق"})
_ZERO_WORDS = frozenset({"nil", "none", "صفر", "لا شيء"})

# Superscript footnote markers. Removed before NFKC, which would map them onto digits.
_SUPERSCRIPTS = frozenset("¹²³⁰⁴⁵⁶⁷⁸⁹")
_TRAILING_FOOTNOTE = re.compile(r"\s*(?:\((?P<marker>[A-Za-zء-ي]{1,3})\)|\*+)\s*$")

_DIGITS_ONLY = re.compile(r"^\d+$")
_PLAIN_NUMBER = re.compile(r"\d*\.?\d+")


@dataclass(frozen=True)
class ParsedNumber:
    """The outcome of reading one printed cell.

    ``value`` is ``None`` when the cell holds no number, which is different from zero: an empty
    cell means the line item does not apply to that period, while a dash means it is zero.

    ``unit`` is ``"ratio"`` when the cell was a percentage, already divided by 100.
    """

    value: Decimal | None
    unit: Literal["number", "ratio"] = "number"
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return self.value is None


def strip_bidi(text: str) -> str:
    """Remove bidirectional formatting characters."""
    return "".join(c for c in text if c not in _BIDI_CONTROLS)


def normalize_digits(text: str) -> tuple[str, bool]:
    """Convert Arabic-Indic and Extended Arabic-Indic digits to ASCII.

    Returns the converted text and whether any conversion happened, so callers can record it.
    """
    converted = False
    out: list[str] = []
    for char in text:
        code = ord(char)
        if _ARABIC_INDIC_ZERO <= code <= _ARABIC_INDIC_ZERO + 9:
            out.append(chr(ord("0") + code - _ARABIC_INDIC_ZERO))
            converted = True
        elif _EXTENDED_ARABIC_INDIC_ZERO <= code <= _EXTENDED_ARABIC_INDIC_ZERO + 9:
            out.append(chr(ord("0") + code - _EXTENDED_ARABIC_INDIC_ZERO))
            converted = True
        else:
            out.append(char)
    return "".join(out), converted


def parse_number(text: str | None) -> ParsedNumber:
    """Read one printed cell into a :class:`ParsedNumber`."""
    flags: list[str] = []

    if text is None:
        return ParsedNumber(None, flags=("empty",))

    cleaned = strip_bidi(text)

    # Superscripts are stripped before NFKC normalization, which maps "¹" onto "1" and would
    # turn a footnote marker into a digit.
    without_superscripts = "".join(c for c in cleaned if c not in _SUPERSCRIPTS)
    if without_superscripts != cleaned:
        flags.append("footnote_marker")
        cleaned = without_superscripts

    # NFKC folds Arabic presentation forms onto base letters, which OCR and some text layers
    # emit, and normalizes exotic spaces.
    cleaned = unicodedata.normalize("NFKC", cleaned).strip()
    if not cleaned:
        return ParsedNumber(None, flags=tuple([*flags, "empty"]))

    lowered = cleaned.casefold()
    if lowered in _NOT_AVAILABLE_WORDS:
        return ParsedNumber(None, flags=tuple([*flags, "not_available"]))
    if lowered in _ZERO_WORDS:
        return ParsedNumber(Decimal(0), flags=tuple([*flags, "word_as_zero"]))

    # A cell holding only dashes is a printed zero.
    if all(c in _DASHES or c in _SPACE_SEPARATORS for c in cleaned):
        return ParsedNumber(Decimal(0), flags=tuple([*flags, "dash_as_zero"]))

    cleaned, converted = normalize_digits(cleaned)
    if converted:
        flags.append("arabic_indic_digits")

    cleaned, unit = _extract_percent(cleaned, flags)
    cleaned = _strip_footnote_markers(cleaned, flags)
    cleaned, negative = _extract_sign(cleaned, flags)

    digits = _to_plain_digits(cleaned, flags)
    if digits is None:
        return ParsedNumber(None, flags=tuple([*flags, "unparsed"]))

    try:
        value = Decimal(digits)
    except InvalidOperation:  # pragma: no cover - guarded by _to_plain_digits
        return ParsedNumber(None, flags=tuple([*flags, "unparsed"]))

    if negative:
        value = -value
    if unit == "ratio":
        value = value / Decimal(100)

    return ParsedNumber(value, unit=unit, flags=tuple(flags))


def _extract_percent(text: str, flags: list[str]) -> tuple[str, Literal["number", "ratio"]]:
    for sign in _PERCENT_SIGNS:
        if sign in text:
            flags.append("percent")
            return text.replace(sign, "").strip(), "ratio"
    return text, "number"


def _strip_footnote_markers(text: str, flags: list[str]) -> str:
    """Remove a trailing marker such as "(a)" or "*".

    Only letters match inside the parentheses, so "(1,234)" stays a negative number.
    """
    match = _TRAILING_FOOTNOTE.search(text)
    if match:
        flags.append("footnote_marker")
        text = text[: match.start()].strip()
    return text


def _extract_sign(text: str, flags: list[str]) -> tuple[str, bool]:
    """Return the text without its sign, and whether the value is negative."""
    negative = False

    if text.startswith("(") and text.endswith(")"):
        inner = text[1:-1].strip()
        if any(c.isdigit() for c in inner):
            negative = True
            flags.append("parentheses_negative")
            text = inner

    if text.startswith(tuple(_DASHES)):
        negative = not negative
        text = text[1:].strip()
    elif text.endswith(tuple(_DASHES)):
        # OCR of a parenthesised negative sometimes yields a trailing hyphen.
        negative = not negative
        flags.append("trailing_sign")
        text = text[:-1].strip()
    elif text.startswith("+"):
        text = text[1:].strip()

    return text, negative


def _to_plain_digits(text: str, flags: list[str]) -> str | None:
    """Reduce a grouped number to plain digits with at most one dot.

    Grouping is resolved from the text itself rather than from an assumed locale, because the
    golden set mixes comma grouping (digital Arabic) with space grouping (scanned filings).
    """
    text = "".join(c for c in text if c not in _SPACE_SEPARATORS)
    if not text:
        return None

    text = text.replace(_ARABIC_THOUSANDS_SEPARATOR, ",").replace(_ARABIC_DECIMAL_SEPARATOR, ".")

    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        # Whichever appears last is the decimal separator; the other groups thousands.
        if text.rfind(",") > text.rfind("."):
            flags.append("european_separators")
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        groups = text.split(",")
        if all(_DIGITS_ONLY.match(g) and len(g) == 3 for g in groups[1:]):
            text = text.replace(",", "")
        else:
            flags.append("comma_decimal")
            text = text.replace(",", ".")
    elif has_dot:
        whole, _, fraction = text.partition(".")
        # "1.234" and "11.234" are genuinely ambiguous: the fraction could be a thousands
        # group. Statements in scope use the dot as a decimal point, so that reading wins and
        # the flag lets review catch a grouped source.
        if len(fraction) == 3 and _DIGITS_ONLY.match(fraction) and len(whole) <= 3:
            flags.append("ambiguous_separator")

    if not _PLAIN_NUMBER.fullmatch(text):
        return None
    return text
