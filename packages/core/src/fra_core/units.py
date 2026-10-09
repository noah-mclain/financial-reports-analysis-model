"""Detect the scale and currency a statement is presented in.

A wrong scale is off by 1000x on every figure, so detection returns the text it matched and
callers should stop on conflicting signals instead of picking one.

Golden set cases this handles:

- Almarai's statement header extracts as ``X '000``. The riyal sign is a font glyph that
  never reaches the text layer, so scale is readable but currency has to come from elsewhere
  on the page.
- The Arabic edition of the same header extracts as ``بآالف X``, where the PDF has mangled
  ``بآلاف``. Both spellings are therefore accepted.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fra_core.numbers import strip_bidi

__all__ = ["ScaleSignal", "detect_currency", "detect_scale", "unit_marker_spans"]

_THOUSAND_CUES: tuple[str, ...] = (
    "'000",
    "’000",
    "in thousands",
    "thousands of",
    "thousand",
    "(000)",
    "بآلاف",
    "بآالف",
    "بالآلاف",
    "آلاف",
    "الاف",
    "ألف",
)
_MILLION_CUES: tuple[str, ...] = (
    "in millions",
    "millions of",
    "million",
    " mn",
    "€m",
    "us$m",
    "بالملايين",
    "ملايين",
    "مليون",
)
_BILLION_CUES: tuple[str, ...] = ("in billions", "billion", " bn", "بالمليارات", "مليار")

# Per-share figures are printed unscaled even when the statement is in thousands.
_PER_SHARE_CUES: tuple[str, ...] = (
    "except per share",
    "except share",
    "per share data",
    "عدا نصيب السهم",
    "فيما عدا السهم",
)

_CURRENCY_WORDS: dict[str, str] = {
    "saudi riyal": "SAR",
    "saudi riyals": "SAR",
    "riyal": "SAR",
    "ريال سعودي": "SAR",
    "ريال": "SAR",
    "sar": "SAR",
    "egyptian pound": "EGP",
    "egyptian pounds": "EGP",
    "جنيه مصري": "EGP",
    "جنيه": "EGP",
    "egp": "EGP",
    "l.e.": "EGP",
    "ج.م": "EGP",
    "us dollar": "USD",
    "us dollars": "USD",
    "u.s. dollar": "USD",
    "u.s. dollars": "USD",
    "united states dollar": "USD",
    "united states dollars": "USD",
    "دولار": "USD",
    "usd": "USD",
    "pound sterling": "GBP",
    "pounds sterling": "GBP",
    "جنيه إسترليني": "GBP",
    "جنيه استرليني": "GBP",
    "gbp": "GBP",
    "kuwaiti dinar": "KWD",
    "kuwaiti dinars": "KWD",
    "دينار كويتي": "KWD",
    "euro": "EUR",
    "eur": "EUR",
    "يورو": "EUR",
    "uae dirham": "AED",
    "dirham": "AED",
    "درهم": "AED",
    "aed": "AED",
}
_CURRENCY_NAMES_BY_LENGTH = sorted(_CURRENCY_WORDS, key=len, reverse=True)

# Two-letter abbreviations are currencies only as printed in capitals: "LE" and "SR" in a
# statement header, not "le" or "Sr." in running text.
_CAPITAL_ABBREVIATIONS: dict[str, str] = {"LE": "EGP", "SR": "SAR", "KD": "KWD"}
# Checked last: a sign says less than a code or a currency word elsewhere in the text.
_CURRENCY_SIGNS: dict[str, str] = {"£": "GBP", "$": "USD"}

_ISO_CODE = re.compile(r"(?<![A-Za-z])(SAR|EGP|USD|GBP|EUR|AED|KWD|QAR|BHD|OMR|JOD)(?![A-Za-z])")

# Whole scale wording and currency names, longest first. Plural scale words are also
# present in the existing "in ..." / "... of" cues. Short capitals remain case-sensitive.
_MARKER_WORDS = set(_CURRENCY_WORDS) | {
    wording
    for cue in (*_THOUSAND_CUES, *_MILLION_CUES, *_BILLION_CUES)
    for wording in (cue.strip(), cue.strip().removeprefix("in ").removesuffix(" of"))
}
_UNIT_MARKER = re.compile(
    r"(?<!\w)(?i:"
    + "|".join(re.escape(cue) for cue in sorted(_MARKER_WORDS, key=len, reverse=True))
    + r")(?!\w)|(?i:"
    + _ISO_CODE.pattern
    + r")|(?<![A-Za-z])(?:"
    + "|".join(_CAPITAL_ABBREVIATIONS)
    + r")(?![A-Za-z])|["
    + re.escape("".join(_CURRENCY_SIGNS))
    + "]"
)


def unit_marker_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return currency/scale spans in the supplied text, preserving adjacent residue.

    Offsets refer to the original input; callers perform any text normalization first.
    Unlike the permissive detectors, a scale substring inside another word is not a
    complete marker. A currency next to a number covers only the currency characters.
    """
    return tuple(match.span() for match in _UNIT_MARKER.finditer(text))


@dataclass(frozen=True)
class ScaleSignal:
    """A scale reading, with the text that produced it."""

    scale: int
    currency: str | None
    per_share_exempt: bool
    evidence: str

    @property
    def is_units(self) -> bool:
        return self.scale == 1


def detect_scale(text: str) -> ScaleSignal | None:
    """Read the scale from a caption, column header or page fragment.

    Returns ``None`` when the text carries no scale wording at all, which is different from
    finding that figures are in units.
    """
    if not text or not text.strip():
        return None

    cleaned = unicodedata.normalize("NFKC", strip_bidi(text))
    lowered = cleaned.casefold()

    scale, evidence = _match_scale(lowered)
    if scale is None:
        return None

    return ScaleSignal(
        scale=scale,
        currency=detect_currency(cleaned),
        per_share_exempt=any(cue in lowered for cue in _PER_SHARE_CUES),
        evidence=evidence,
    )


def detect_currency(text: str) -> str | None:
    """Find an ISO 4217 code, a currency word, a capital abbreviation or a currency sign, in
    that order."""
    if not text or not text.strip():
        return None

    cleaned = unicodedata.normalize("NFKC", strip_bidi(text))

    code = _ISO_CODE.search(cleaned.upper())
    if code:
        return code.group(1)

    lowered = cleaned.casefold()
    for name in _CURRENCY_NAMES_BY_LENGTH:
        if _mentions_currency(name, lowered):
            return _CURRENCY_WORDS[name]
    for abbreviation, currency in _CAPITAL_ABBREVIATIONS.items():
        if re.search(rf"(?<![A-Za-z]){abbreviation}(?![A-Za-z])", cleaned):
            return currency
    for sign, currency in _CURRENCY_SIGNS.items():
        if sign in cleaned:
            return currency
    return None


def _mentions_currency(name: str, text: str) -> bool:
    """Whether ``name`` appears in ``text`` as a currency reference.

    Latin names need word boundaries: "sar" also sits inside "necessary". Arabic can't use
    boundaries since the article and plural endings attach directly to the stem: ريال appears
    inside الريالات with word characters on both sides, so a boundary test would reject a
    correct match.
    """
    if name.isascii():
        return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) is not None
    return name in text


def _match_scale(lowered: str) -> tuple[int | None, str]:
    """Largest unit first: "in millions" must not be read as "in thousands"."""
    for cue in _BILLION_CUES:
        if cue in lowered:
            return 1_000_000_000, cue.strip()
    for cue in _MILLION_CUES:
        if cue in lowered:
            return 1_000_000, cue.strip()
    for cue in _THOUSAND_CUES:
        if cue in lowered:
            return 1_000, cue.strip()
    return None, ""
