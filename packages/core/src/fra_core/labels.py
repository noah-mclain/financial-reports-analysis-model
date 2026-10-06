"""Normalize a printed line-item label so that two spellings of the same thing compare equal.

Statements do not spell labels consistently. The same item appears as "Right-of-use assets"
and "Right of use assets"; Arabic filings vary the hamza (``إجمالي`` against ``اجمالي``), add
or drop diacritics, and OCR inserts tatweel. Normalization is deliberately conservative: it
removes decoration, never words, so that "total assets" and "total current assets" stay
distinct.
"""

from __future__ import annotations

import re
import unicodedata

from fra_core.numbers import strip_bidi

__all__ = ["normalize_label"]

# Arabic diacritics (U+064B-U+0652), superscript alef, and tatweel, which OCR adds freely.
_DECORATION = frozenset("ًٌٍَُِّْٰـ")

# Alef spellings collapse to the bare form: filings alternate freely between them.
_ALEF_FORMS = frozenset("أإآٱ")

_LETTER_FOLDING = {
    "ة": "ه",  # ta marbuta, written either way at the end of a word
    "ى": "ي",  # alef maqsura
    "ئ": "ي",
    "ؤ": "و",
    "ی": "ي",  # Persian yeh: NFKC maps the joined presentation forms of yeh to it
    "ک": "ك",  # Persian kaf: likewise for kaf
}

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_label(text: str) -> str:
    """Return a comparable form of ``text``.

    Compare normalized forms on both sides; never store the result in place of the label as
    printed, which stays on the line item for display and for provenance.
    """
    if not text:
        return ""

    folded = unicodedata.normalize("NFKC", strip_bidi(text))
    folded = "".join(c for c in folded if c not in _DECORATION)
    folded = "".join("ا" if c in _ALEF_FORMS else _LETTER_FOLDING.get(c, c) for c in folded)
    folded = folded.casefold()
    folded = _PUNCTUATION.sub(" ", folded)
    return _WHITESPACE.sub(" ", folded).strip()
