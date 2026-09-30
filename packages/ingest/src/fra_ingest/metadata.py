"""Scale, currency, entity and consolidation for one statement (spec 11, Data flow step 6).

Each is read from the header first, then from the text around the table. Currency alone falls
back to the currency named most often in the document, flagged ``currency_inferred``, because
some filings print the currency as a font glyph (Almarai's riyal sign extracts as ``X``).
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


def _has_entity_marker(text: str) -> bool:
    """Check if text contains an entity marker with appropriate boundaries."""
    lowered = text.casefold()
    # Check Latin markers with word boundaries
    latin_pattern = r"(?<![\w.])(company|corporation|plc|ltd|limited|s\.a\.e)(?![\w])"
    if re.search(latin_pattern, lowered, re.IGNORECASE):
        return True
    # Check Arabic marker as substring
    return _ENTITY_ARABIC in text


def _is_entity_shaped(text: str) -> bool:
    """Check if text resembles an entity name: at most 10 words, no excluded first word."""
    words = text.split()
    if len(words) > 10:
        return False
    first_word = words[0].casefold() if words else ""
    return first_word not in _EXCLUDED_ENTITY_STARTS


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
    *, header_text: str, context_texts: Sequence[str], document_texts: Sequence[str]
) -> Metadata:
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
