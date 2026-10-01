"""Scale, currency, entity and consolidation for one statement (spec 11, Data flow step 6).

Each is read from the header first, then from the text around the table. Currency alone falls
back further, because some filings print the currency as a font glyph (Almarai's riyal sign
extracts as ``X``): to the country of incorporation ("a Saudi Joint Stock Company"), flagged
``currency_from_domicile`` (the earliest page naming one), then to the currency named most
often in the document, flagged ``currency_inferred``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from fra_core.units import detect_currency, detect_scale
from fra_ingest.label_match import squash

_CONSOLIDATED = ("consolidated", "المجمع", "الموحد")
_STANDALONE_PHRASES = (
    "separate financial statements",
    "separate statement",
    "standalone",
    "stand-alone",
    "المستقلة",
    "المنفصلة",
)
_ENTITY_LATIN = ("company", "corporation", "s.a.e", "plc", "limited", "ltd")
_ENTITY_ARABIC = "شركة"
_EXCLUDED_ENTITY_STARTS = ("statement", "notes", "the", "these", "for", "as")

_COUNTRY_CURRENCY = {
    "saudi": "SAR",
    "egyptian": "EGP",
    "emirati": "AED",
    "uae": "AED",
    "kuwaiti": "KWD",
    "qatari": "QAR",
    "bahraini": "BHD",
    "omani": "OMR",
}
# A country adjective, at most two words, then a company form: "a Saudi Joint Stock Company".
_DOMICILE_LATIN = re.compile(
    r"\b(" + "|".join(_COUNTRY_CURRENCY) + r")\s+(?:[a-z]+\s+){0,2}?"
    r"(?:closed\s+)?(?:joint\s+stock\s+company|public\s+shareholding\s+company|public\s+company)\b"
)
_SAE = re.compile(r"(?<![\w.])s\.a\.e\b")
_COUNTRY_CURRENCY_ARABIC = {
    squash(adjective): currency
    for adjective, currency in (
        ("سعودية", "SAR"),
        ("مصرية", "EGP"),
        ("إماراتية", "AED"),
        ("كويتية", "KWD"),
        ("قطرية", "QAR"),
        ("بحرينية", "BHD"),
        ("عمانية", "OMR"),
    )
}
# Space-free: "مساهمة", optionally "عامة" or "مقفلة", then the country adjective.
_DOMICILE_ARABIC = re.compile(
    squash("مساهمة")
    + "(?:"
    + "|".join((squash("عامة"), squash("مقفلة")))
    + ")?("
    + "|".join(_COUNTRY_CURRENCY_ARABIC)
    + ")"
)


def domicile_currency(text: str) -> str | None:
    """The currency of the country a company is incorporated in, from a phrase such as
    "a Saudi Joint Stock Company", "S.A.E." or "شركة مساهمة سعودية"."""
    lowered = text.casefold()
    latin = _DOMICILE_LATIN.search(lowered)
    if latin:
        return _COUNTRY_CURRENCY[latin.group(1)]
    if _SAE.search(lowered):
        return "EGP"
    arabic = _DOMICILE_ARABIC.search(squash(text))
    return _COUNTRY_CURRENCY_ARABIC[arabic.group(1)] if arabic else None


def _has_entity_marker(text: str) -> bool:
    """A Latin company form on word boundaries, or a line whose first token starts with
    "شركة" (so a joined running header such as "حوكمةالشركة" is not one)."""
    lowered = text.casefold()
    latin_pattern = r"(?<![\w.])(company|corporation|plc|ltd|limited|s\.a\.e)(?![\w])"
    if re.search(latin_pattern, lowered, re.IGNORECASE):
        return True
    words = text.split()
    return bool(words) and words[0].startswith(_ENTITY_ARABIC)


def _is_entity_shaped(text: str) -> bool:
    """At most 10 words, no excluded first word, and not a domicile line such as
    "(An Egyptian Joint Stock Company)"."""
    words = text.split()
    if not words or len(words) > 10:
        return False
    stripped = text.strip()
    if stripped.startswith("(") or re.match(r"an?\s", stripped, re.IGNORECASE):
        return False
    if _DOMICILE_LATIN.search(stripped.casefold()) or _DOMICILE_ARABIC.search(squash(stripped)):
        return False
    return words[0].casefold() not in _EXCLUDED_ENTITY_STARTS


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scale: int | None = None
    currency: str | None = None
    entity_name: str | None = None
    consolidated: bool | None = None
    signals: list[str] = Field(default_factory=list)
    conflict: bool = False
    flags: list[str] = Field(default_factory=list)


def detect_metadata(
    *,
    header_text: str,
    context_texts: Sequence[str],
    document_texts: Sequence[str],
    domicile_texts: Sequence[str] = (),
) -> Metadata:
    """``domicile_texts`` come in page order: the first one naming a country of incorporation
    decides, since the reporting company comes before its subsidiaries."""
    meta = Metadata()

    header_scale = detect_scale(header_text)
    context_scale = next((s for t in context_texts if (s := detect_scale(t)) is not None), None)
    if header_scale is not None:
        meta.scale = header_scale.scale
        meta.signals.append("scale:header")
        if context_scale is not None and context_scale.scale != header_scale.scale:
            meta.conflict = True
            meta.flags.append("scale_conflict")
    elif context_scale is not None:
        meta.scale = context_scale.scale
        meta.signals.append("scale:context")
    else:
        meta.flags.append("scale_missing")

    header_currency = detect_currency(header_text)
    context_currency = next((c for t in context_texts if (c := detect_currency(t))), None)
    if header_currency is not None:
        meta.currency = header_currency
        meta.signals.append("currency:header")
        if context_currency is not None and context_currency != header_currency:
            meta.conflict = True
            meta.flags.append("currency_conflict")
    elif context_currency is not None:
        meta.currency = context_currency
        meta.signals.append("currency:context")
    elif domicile := next((c for t in domicile_texts if (c := domicile_currency(t))), None):
        meta.currency = domicile
        meta.signals.append("currency:domicile")
        meta.flags.append("currency_from_domicile")
    else:
        counts = Counter(c for t in document_texts if (c := detect_currency(t)))
        if counts:
            meta.currency = counts.most_common(1)[0][0]
            meta.signals.append("currency:document")
            meta.flags.append("currency_inferred")
        else:
            meta.flags.append("currency_missing")

    # Check all context texts for consolidated markers first
    has_consolidated = any(
        any(squash(m) in squash(text) for m in _CONSOLIDATED) for text in context_texts
    )
    if has_consolidated:
        meta.consolidated = True

    for text in context_texts:
        # Extract entity name
        if meta.entity_name is None and _has_entity_marker(text) and _is_entity_shaped(text):
            meta.entity_name = " ".join(text.split())

        # Extract consolidation status only if not already set to True
        if meta.consolidated is None:
            lowered = text.casefold()
            # Check for standalone phrases
            if any(phrase.casefold() in lowered for phrase in _STANDALONE_PHRASES):
                meta.consolidated = False

    return meta
