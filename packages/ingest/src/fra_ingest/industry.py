"""Whether the issuer is a bank, an insurer or another financial company.

Line items show the kind of business, so cues are read from the balance sheet and income
statement pages when the locator found them. When those pages give no verdict, or none were
found, every page is read, but each cue counts once and at least three different cues must
appear: an ordinary report repeats one phrase on many pages (a utility's "deposits from
customers", a group's consumer finance subsidiary), while a bank or insurer uses the whole
vocabulary. The verdict is stored on the locate
result; ``industry_decision`` turns it into a decline or a hold for review.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from fra_core.schemas import StatementType
from fra_ingest.results import (
    IndustryDecision,
    IndustryHoldCode,
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
# other_financial is held for review, never declined: its sub-kinds read as corporate, so a
# decline there would be a guess the verdict cannot back.
# A corporate verdict whose score is within this of VERDICT_THRESHOLD is a near miss.
UNCERTAIN_MARGIN = 2.0
_EVIDENCE_LIMIT = 12
_STATEMENT_TYPES = (StatementType.BALANCE, StatementType.INCOME)


@dataclass(frozen=True)
class IndustryBook:
    exclude: PhraseIndex
    cues: PhraseIndex
    weights: dict[tuple[str, str], float]  # (group, canonical phrase) -> weight
    order: tuple[str, ...]  # groups as listed in the cue file; ties go to the first


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
        order=tuple(groups),
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
    evidence: list[tuple[int, str, str]] = []  # (page, group, phrase)
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
                evidence.append((page.page_no, group, phrase))

    if not totals:
        return IndustrySignal(kind="corporate")
    group, score = max(totals.items(), key=lambda item: (item[1], -book.order.index(item[0])))
    shown = [(page_no, phrase) for page_no, g, phrase in evidence if g == group]
    cues = len(distinct[group])
    too_narrow = whole_document and cues < WHOLE_DOCUMENT_MIN_CUES
    if score < VERDICT_THRESHOLD or too_narrow:
        return IndustrySignal(
            kind="corporate", score=score, distinct_cues=cues, evidence=shown[:_EVIDENCE_LIMIT]
        )
    kind, _, subkind = group.partition("/")
    return IndustrySignal(
        kind=cast(IndustryKind, kind),
        subkind=cast(IndustrySubkind, subkind) if subkind else None,
        score=score,
        distinct_cues=cues,
        evidence=shown[:_EVIDENCE_LIMIT],
    )


def _without(book: IndustryBook, text: str) -> str:
    """The text with excluded phrases blanked, in canonical form."""
    normalized = f" {canonical(text)} "
    for phrase, _ in book.exclude.entries:
        # A placeholder word, so the words on either side cannot join into a cue.
        normalized = normalized.replace(f" {phrase} ", " excluded ")
    return normalized


def industry_decision(signal: IndustrySignal) -> IndustryDecision | None:
    """Decline a bank or an insurer the verdict is sure of; hold for review what it cannot settle:

    - ``weak_verdict``: a bank or insurer scoring under VERDICT_THRESHOLD + UNCERTAIN_MARGIN,
      or resting on fewer than WHOLE_DOCUMENT_MIN_CUES different cue phrases;
    - ``no_verdict``: kind ``unknown``, the document was not read;
    - ``other_financial``: a financial company that is neither bank nor insurer;
    - ``too_narrow``: a corporate verdict scoring at least VERDICT_THRESHOLD, which only
      the whole-document fallback's WHOLE_DOCUMENT_MIN_CUES rule can produce;
    - ``near_threshold``: a corporate verdict scoring within UNCERTAIN_MARGIN of
      VERDICT_THRESHOLD.

    Any other corporate verdict is neither: None.
    """
    outcome: Literal["declined", "needs_review"]
    code: Literal["bank", "insurer"] | IndustryHoldCode
    if signal.kind == "bank" or signal.kind == "insurer":
        sure = (
            signal.score >= VERDICT_THRESHOLD + UNCERTAIN_MARGIN
            and signal.distinct_cues >= WHOLE_DOCUMENT_MIN_CUES
        )
        outcome, code = ("declined", signal.kind) if sure else ("needs_review", "weak_verdict")
    elif signal.kind == "unknown":
        outcome, code = "needs_review", "no_verdict"
    elif signal.kind == "other_financial":
        outcome, code = "needs_review", "other_financial"
    elif signal.score >= VERDICT_THRESHOLD:
        outcome, code = "needs_review", "too_narrow"
    elif signal.score >= VERDICT_THRESHOLD - UNCERTAIN_MARGIN:
        outcome, code = "needs_review", "near_threshold"
    else:
        return None
    label = signal.kind + (f"/{signal.subkind}" if signal.subkind else "")
    evidence = ", ".join(f"page {page} '{phrase}'" for page, phrase in signal.evidence) or "none"
    prefix = "declined" if outcome == "declined" else "industry_uncertain"
    return IndustryDecision(
        outcome=outcome,
        code=code,
        signal=signal,
        reason=f"{prefix}:{code}: {label}, score {signal.score:.1f}, "
        f"{signal.distinct_cues} distinct cues "
        f"(verdict threshold {VERDICT_THRESHOLD:.1f}, margin {UNCERTAIN_MARGIN:.1f}); "
        f"evidence: {evidence}",
    )
