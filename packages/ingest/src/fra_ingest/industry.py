"""Whether the issuer is a bank, an insurer or another financial company.

Line items show the kind of business, so cues are read from the balance sheet and income
statement pages when the locator found them. When those pages give no verdict, or none were
found, every page is read, but each cue counts once and at least three different cues must
appear: an ordinary report repeats one phrase on many pages (a utility's "deposits from
customers", a group's consumer finance subsidiary), while a bank or insurer uses the whole
vocabulary. The verdict is stored on the locate
result; declining is decided in week 2.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast

import yaml

from fra_core.schemas import StatementType
from fra_ingest.results import (
    IndustryKind,
    IndustrySignal,
    IndustrySubkind,
    PageText,
    StatementRange,
)
from fra_ingest.text_match import PhraseIndex, canonical, reading_variants

VERDICT_THRESHOLD = 6.0
# Distinct cues the whole-document fallback needs, each counted once.
WHOLE_DOCUMENT_MIN_CUES = 3
_EVIDENCE_LIMIT = 12
_STATEMENT_TYPES = (StatementType.BALANCE, StatementType.INCOME)


@dataclass(frozen=True)
class IndustryBook:
    exclude: PhraseIndex
    cues: PhraseIndex
    weights: dict[tuple[str, str], float]  # (group, canonical phrase) -> weight


def load_industry_book(path: Path | None = None) -> IndustryBook:
    if path is None:
        raw = resources.files("fra_ingest.data").joinpath("industry_cues.yaml").read_text("utf-8")
    else:
        raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    groups: dict[str, dict[str, float]] = data["cues"]
    return IndustryBook(
        exclude=PhraseIndex.build({"exclude": data["exclude"]}),
        cues=PhraseIndex.build({group: list(cues) for group, cues in groups.items()}),
        weights={
            (group, canonical(phrase)): float(weight)
            for group, cues in groups.items()
            for phrase, weight in cues.items()
        },
    )


def detect_industry(
    pages: list[PageText], ranges: list[StatementRange], book: IndustryBook
) -> IndustrySignal:
    readable = [page for page in pages if page.text.strip()]
    unread = sum(1 for page in pages if "ocr_unavailable" in page.flags)
    if not readable or unread > len(pages) / 2:
        # Most of the document was never read: no verdict rather than a guess of corporate.
        return IndustrySignal(kind="unknown")

    statement_pages = {
        page_no
        for r in ranges
        if r.type in _STATEMENT_TYPES
        for page_no in range(r.first_page, r.last_page + 1)
    }
    on_statements = [p for p in readable if p.page_no in statement_pages]
    if on_statements:
        signal = _verdict(on_statements, book)
        if signal.kind != "corporate":
            return signal
    return _verdict(readable, book, whole_document=True)


def _verdict(
    pages: list[PageText], book: IndustryBook, *, whole_document: bool = False
) -> IndustrySignal:
    totals: dict[str, float] = defaultdict(float)
    distinct: dict[str, set[str]] = defaultdict(set)
    evidence: list[tuple[int, str]] = []
    for page in pages:
        variants = [
            _without(book, text) for text in reading_variants(page.text, page.visual_arabic)
        ]
        for phrase, groups in book.cues.find(variants).items():
            for group in groups:
                if whole_document and phrase in distinct[group]:
                    continue
                totals[group] += book.weights[(group, phrase)]
                distinct[group].add(phrase)
            evidence.append((page.page_no, phrase))

    if not totals:
        return IndustrySignal(kind="corporate")
    group, score = max(totals.items(), key=lambda item: item[1])
    too_narrow = whole_document and len(distinct[group]) < WHOLE_DOCUMENT_MIN_CUES
    if score < VERDICT_THRESHOLD or too_narrow:
        return IndustrySignal(kind="corporate", score=score, evidence=evidence[:_EVIDENCE_LIMIT])
    kind, _, subkind = group.partition("/")
    return IndustrySignal(
        kind=cast(IndustryKind, kind),
        subkind=cast(IndustrySubkind, subkind) if subkind else None,
        score=score,
        evidence=evidence[:_EVIDENCE_LIMIT],
    )


def _without(book: IndustryBook, text: str) -> str:
    """The text with excluded phrases blanked, in canonical form."""
    normalized = f" {canonical(text)} "
    for phrase, _ in book.exclude.entries:
        # A placeholder word, so the words on either side cannot join into a cue.
        normalized = normalized.replace(f" {phrase} ", " excluded ")
    return normalized
