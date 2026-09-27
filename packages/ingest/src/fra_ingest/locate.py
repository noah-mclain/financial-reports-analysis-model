"""Find the pages that hold each statement.

Pure functions over page text. A page scores for a type from its title in the header, line
item cues when the title is missing, numeric density, and structural cues every statement
page carries (period header, note column, scale line). Header wording of auditor's reports,
contents pages and notes subtracts.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from fra_core.numbers import normalize_digits
from fra_core.schemas import Document, StatementType
from fra_core.units import detect_scale
from fra_ingest.config import IngestConfig
from fra_ingest.industry import IndustryBook, detect_industry, load_industry_book
from fra_ingest.pages import document_language, profiles
from fra_ingest.results import (
    IndustrySignal,
    LocateResult,
    PageScore,
    PageText,
    StatementRange,
)
from fra_ingest.text_match import PhraseIndex, reading_variants

# Scoring weights. A page is a candidate for a type when it names the type (title or line
# item cue) and its score reaches CANDIDATE_THRESHOLD. See docs/blueprint/09-ingest-locate.md.
TITLE_WEIGHT = 3.0
CUE_WEIGHT = 1.5
NUMERIC_WEIGHT = 2.0
NUMERIC_FULL_AT = 30
STRUCTURE_WEIGHT = 1.0
NEGATIVE_WEIGHT = 4.0
CANDIDATE_THRESHOLD = 4.5
CONTINUATION_MIN_NUMBERS = 15

LOCATE_VERSION = "1"

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_NUMBER = re.compile(r"\d{1,3}(?:[,٬. ]\d{3})+|\d{3,}")


@dataclass(frozen=True)
class TitleBook:
    titles: PhraseIndex
    body_cues: PhraseIndex
    continuation: PhraseIndex
    note_headers: PhraseIndex
    period_words: PhraseIndex
    negatives: PhraseIndex


def load_title_book(path: Path | None = None) -> TitleBook:
    if path is None:
        raw = (
            resources.files("fra_ingest.data").joinpath("statement_titles.yaml").read_text("utf-8")
        )
    else:
        raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    return TitleBook(
        titles=PhraseIndex.build(data["titles"]),
        body_cues=PhraseIndex.build(data["body_cues"]),
        continuation=PhraseIndex.build({"continued": data["continuation"]}),
        note_headers=PhraseIndex.build({"note": data["note_headers"]}),
        period_words=PhraseIndex.build({"period": data["period_words"]}),
        negatives=PhraseIndex.build(data["negatives"]),
    )


def count_numeric_tokens(text: str) -> int:
    """Printed amounts with three or more digits. Years are not amounts."""
    digits, _ = normalize_digits(text)
    return sum(1 for match in _NUMBER.finditer(digits) if not _YEAR.fullmatch(match.group(0)))


def score_page(page: PageText, book: TitleBook) -> PageScore:
    header = reading_variants(page.header_text, page.visual_arabic)
    whole = reading_variants(page.text, page.visual_arabic)

    title_hits = book.titles.find(header)
    title_types = _types(title_hits.values())
    cue_types = [] if title_types else _types(book.body_cues.find(whole).values())
    negatives = sorted({kind for kinds in book.negatives.find(header).values() for kind in kinds})
    numeric = count_numeric_tokens(page.text)
    structure = _structure(page, header, book)

    shared = (
        NUMERIC_WEIGHT * min(numeric / NUMERIC_FULL_AT, 1.0)
        + STRUCTURE_WEIGHT * len(structure)
        - NEGATIVE_WEIGHT * len(negatives)
    )
    type_scores = {
        statement_type: shared
        + (TITLE_WEIGHT if statement_type in title_types else 0.0)
        + (CUE_WEIGHT if statement_type in cue_types else 0.0)
        for statement_type in StatementType
    }
    return PageScore(
        page_no=page.page_no,
        type_scores=type_scores,
        title_types=title_types,
        cue_types=cue_types,
        title_hits=sorted(title_hits),
        numeric_tokens=numeric,
        structure=structure,
        negatives=negatives,
        continuation=bool(book.continuation.find(header)),
    )


def find_ranges(scores: Sequence[PageScore]) -> list[StatementRange]:
    """Group candidate pages per type. An untitled numeric page directly after a range
    continues it, which covers statements printed over several pages."""
    ordered = sorted(scores, key=lambda score: score.page_no)
    ranges: list[StatementRange] = []
    for statement_type in StatementType:
        groups: list[list[PageScore]] = []
        open_group: list[PageScore] | None = None
        for score in ordered:
            adjacent = open_group is not None and score.page_no == open_group[-1].page_no + 1
            if score.is_candidate(statement_type, CANDIDATE_THRESHOLD):
                if open_group is not None and adjacent:
                    open_group.append(score)
                else:
                    open_group = [score]
                    groups.append(open_group)
            elif open_group is not None and adjacent and _continues(score):
                open_group.append(score)
            else:
                open_group = None
        found = [
            (max(page.type_scores[statement_type] for page in group), group) for group in groups
        ]
        found.sort(key=lambda item: item[0], reverse=True)
        ranges.extend(
            StatementRange(
                type=statement_type,
                first_page=group[0].page_no,
                last_page=group[-1].page_no,
                score=best,
                rank=rank,
            )
            for rank, (best, group) in enumerate(found, start=1)
        )
    return ranges


def plan_conversion(
    ranges: Sequence[StatementRange],
    enabled: Sequence[StatementType],
    page_count: int,
    pad: int,
) -> list[tuple[int, int]]:
    """Padded ranges of the enabled types, merged where they touch or overlap."""
    spans = sorted(r.padded(pad, page_count) for r in ranges if r.type in enabled)
    merged: list[tuple[int, int]] = []
    for first, last in spans:
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def locate(
    pages: Sequence[PageText],
    config: IngestConfig,
    *,
    sha256: str,
    filename: str,
    book: TitleBook | None = None,
    industry_book: IndustryBook | None = None,
    timings: dict[str, float] | None = None,
) -> LocateResult:
    started = time.perf_counter()
    book = book or load_title_book()
    industry_book = industry_book or load_industry_book()

    scores = [score_page(page, book) for page in pages]
    ranges = find_ranges(scores)
    convert = plan_conversion(ranges, config.enabled_types, len(pages), config.pad_pages)
    signal = detect_industry(list(pages), ranges, industry_book)
    flags = locate_flags(pages, ranges, convert, signal, config)

    return LocateResult(
        version=LOCATE_VERSION,
        document=Document(
            sha256=sha256,
            filename=filename,
            page_count=len(pages),
            pages=profiles(pages),
            language=document_language(pages),
        ),
        pages=scores,
        ranges=ranges,
        convert_ranges=convert,
        industry=signal,
        flags=flags,
        timings={**(timings or {}), "score": time.perf_counter() - started},
    )


def locate_flags(
    pages: Sequence[PageText],
    ranges: Sequence[StatementRange],
    convert: Sequence[tuple[int, int]],
    signal: IndustrySignal,
    config: IngestConfig,
) -> list[str]:
    flags: list[str] = []
    unread = sum(1 for page in pages if "ocr_unavailable" in page.flags)
    if unread:
        flags.append(f"image_pages_not_read:{unread}")
    found = {r.type for r in ranges}
    flags.extend(f"statement_not_found:{t.value}" for t in config.enabled_types if t not in found)
    if not convert:
        flags.append("no_statements_found")
    elif pages:
        share = sum(last - first + 1 for first, last in convert) / len(pages)
        if share > config.low_selectivity_share:
            flags.append("low_selectivity")
    if signal.kind in ("bank", "insurer"):
        flags.append(f"likely_{signal.kind}")
    elif signal.kind == "other_financial":
        flags.append(f"likely_other_financial:{signal.subkind or 'other'}")
    return flags


def _continues(score: PageScore) -> bool:
    if score.title_types:
        return False
    return score.continuation or score.numeric_tokens >= CONTINUATION_MIN_NUMBERS


def _types(groups: Iterable[frozenset[str]]) -> list[StatementType]:
    names = {name for group in groups for name in group}
    return [statement_type for statement_type in StatementType if statement_type.value in names]


def _structure(page: PageText, header: list[str], book: TitleBook) -> list[str]:
    found: list[str] = []
    digits, _ = normalize_digits(page.header_text)
    years = set(_YEAR.findall(digits))
    if len(years) >= 2 or (years and book.period_words.find(header)):
        found.append("period")
    if book.note_headers.find(header):
        found.append("note_column")
    if any(detect_scale(variant) for variant in header):
        found.append("scale")
    return found
