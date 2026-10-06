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
from fra_core.schemas import Document, StatementType, TextSource
from fra_core.units import detect_currency, detect_scale
from fra_ingest.config import IngestConfig
from fra_ingest.industry import IndustryBook, detect_industry, load_industry_book
from fra_ingest.pages import GARBLED_TEXT_LAYER, document_language, profiles
from fra_ingest.results import (
    CANDIDATE_THRESHOLD,
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
CONTINUATION_MIN_NUMBERS = 15
TITLE_LINE_MAX_WORDS = 7

LOCATE_VERSION = "6"  # other financial cues for consumer finance and licensed asset managers

_DIGIT = re.compile(r"\d")
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# Thousands separators: comma, Arabic, dot, and plain, no-break, narrow no-break and thin spaces.
_NUMBER = re.compile(r"\d{1,3}(?:[,\u066c. \u00a0\u202f\u2009]\d{3})+|\d{3,}")
_TWO_YEARS = re.compile(r"(?:19|20)\d{2}(?:19|20)\d{2}")


@dataclass(frozen=True)
class TitleBook:
    titles: PhraseIndex
    title_stems: PhraseIndex
    body_cues: PhraseIndex
    revenue_lines: PhraseIndex
    continuation: PhraseIndex
    note_headers: PhraseIndex
    period_words: PhraseIndex
    negatives: PhraseIndex
    footer_markers: PhraseIndex


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
        title_stems=PhraseIndex.build(data["title_stems"]),
        body_cues=PhraseIndex.build(data["body_cues"]),
        revenue_lines=PhraseIndex.build({"revenue": data["revenue_lines"]}),
        continuation=PhraseIndex.build({"continued": data["continuation"]}),
        note_headers=PhraseIndex.build({"note": data["note_headers"]}),
        period_words=PhraseIndex.build({"period": data["period_words"]}),
        negatives=PhraseIndex.build(data["negatives"]),
        footer_markers=PhraseIndex.build({"footer": data["footer_markers"]}),
    )


def count_numeric_tokens(text: str) -> int:
    """Printed amounts with three or more digits. Years are not amounts."""
    digits, _ = normalize_digits(text)
    count = 0
    for match in _NUMBER.finditer(digits):
        token = match.group(0)
        if _YEAR.fullmatch(token) or _TWO_YEARS.fullmatch(token):
            continue
        if token[0] == "0" and len(token) > 1 and not token[1].isdigit():
            continue  # 0.123 is a ratio: no amount's leading group is a bare zero
        if match.start() > 0 and digits[match.start() - 1] == ".":
            continue  # the fractional part of a decimal such as 12.345
        count += 1
    return count


def score_page(page: PageText, book: TitleBook) -> PageScore:
    header = reading_variants(page.header_text, page.visual_arabic)
    whole = reading_variants(page.text, page.visual_arabic)

    title_hits = book.titles.find(header)
    # Stems (a title without its first word) only count on a short line without figures, as
    # titles are: line items reuse the words (the other comprehensive income reserve), with
    # their amounts beside them or, in OCR output, on a line of their own.
    stem_lines = [
        line
        for text in header
        for line in text.splitlines()
        if not _DIGIT.search(line) and len(line.split()) <= TITLE_LINE_MAX_WORDS
    ]
    title_hits.update(
        {
            phrase: groups
            for phrase, groups in book.title_stems.find(stem_lines).items()
            if not any(phrase in hit for hit in title_hits)
        }
    )
    title_types = _types(title_hits.values())
    if (
        StatementType.COMPREHENSIVE_INCOME in title_types
        and StatementType.INCOME not in title_types
        and book.revenue_lines.find(whole)
    ):
        # A single statement titled comprehensive income that carries revenue.
        title_types = [t for t in StatementType if t in {*title_types, StatementType.INCOME}]
    cue_types = [] if title_types else _types(book.body_cues.find(whole).values())
    # A statement's closing line mentions its notes; it is not a notes heading.
    heading_lines = [
        line
        for text in header
        for line in text.splitlines()
        if not book.footer_markers.find([line])
    ]
    negatives = sorted(
        {kind for kinds in book.negatives.find(heading_lines).values() for kind in kinds}
    )
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
    failed = sum(1 for page in pages if "ocr_failed" in page.flags)
    if failed:
        flags.append(f"ocr_failed_pages:{failed}")
    garbled = sum(
        1
        for page in pages
        if GARBLED_TEXT_LAYER in page.flags and page.source is not TextSource.OCR
    )
    if garbled:
        flags.append(f"garbled_text_layers_not_read:{garbled}")
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
    # A titled page starts its own range, and a page with auditor, contents or notes wording in
    # its header is never part of a statement, however many numbers it holds.
    if score.title_types or score.negatives:
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
    if any(detect_scale(variant) or detect_currency(variant) for variant in header):
        found.append("scale_or_currency")
    return found
