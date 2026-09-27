"""Find the pages that hold each statement.

Pure functions over page text. A page scores for a type from its title in the header, line
item cues when the title is missing, numeric density, and structural cues every statement
page carries (period header, note column, scale line). Header wording of auditor's reports,
contents pages and notes subtracts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from fra_core.numbers import normalize_digits
from fra_core.schemas import StatementType
from fra_core.units import detect_scale
from fra_ingest.results import PageScore, PageText
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
