"""Repair of Arabic text that docling returns in visual order (spec 11, Data flow step 3).

Measured on Almarai AR: each run of Arabic digits comes out reversed in place (mirrored
brackets swapped back), and letters come out in reading order but separated by single spaces.
Digit runs are reversed back in place only on pages Part 1 marked ``visual_arabic``; spaced
letters are joined wherever they occur.

A second pattern, measured on the Arabic digital fit documents: docling returns the cells of a
table with the words of each cell in reverse order and the letters of each word in reading
order. The tables of one document agree with each other and its pages are not ``visual_arabic``,
so the order is decided per table from the lexicon (``restore_word_order``).
"""

from __future__ import annotations

import re
import unicodedata

from fra_core.schemas import StatementType
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_grid import Grid, GridCell

_DIGIT = "٠-٩۰-۹"
_RUN = re.compile(rf"[{_DIGIT}](?:[{_DIGIT},.٫٬]*[{_DIGIT}])?")
_DIACRITICS = frozenset("ًٌٍَُِّْٰ")
_ARABIC_LETTER = re.compile(r"^[ء-يٱ-ۓ]$")
_LAM_ALEF = frozenset({"لا", "لأ", "لإ", "لآ"})
# A year whose last digit came back as a token of its own, before the other three: "5 202".
_ANY_DIGIT = rf"0-9{_DIGIT}"
_SEPARATORS = ",.٫٬"
_SPLIT_YEAR = re.compile(
    rf"(?<![{_ANY_DIGIT}{_SEPARATORS}])([{_ANY_DIGIT}]) "
    rf"([2٢۲][0٠۰][0-3٠-٣۰-۳])(?![{_ANY_DIGIT}{_SEPARATORS}])"
)
_ARABIC = re.compile(r"[ء-يٱ-ۓ]")
_LATIN = re.compile(r"[A-Za-z]")
# Cells that must match the lexicon only when reversed before a table is reversed, and by how
# much they must outnumber the cells that match as printed. Chosen on the 17 fit and 3 validation
# Arabic digital documents (135 tables, 35 reversed): a reversed table held 3 to 11 such cells
# against 0 or 1 matching as printed, a table left alone 0 to 2 against 0 to 3. So the
# thresholds are an in-sample choice (docs/blueprint/11-ingest-structure.md).
MIN_REVERSED_CELLS = 3
REVERSED_MAJORITY = 2
WORDS_REVERSED = "words_reversed"  # statement flags the repair leaves on the grid
WORD_ORDER_UNCERTAIN = "word_order_uncertain"


def restore_digits(text: str) -> str:
    restored = _RUN.sub(lambda m: m.group()[::-1], text)
    stripped = restored.strip()
    if stripped.startswith(")") and stripped.endswith("(") and len(stripped) > 1:
        start = restored.index(")")
        end = restored.rindex("(")
        restored = restored[:start] + "(" + restored[start + 1 : end] + ")" + restored[end + 1 :]
    return restored


def _is_letter(token: str) -> bool:
    core = "".join(c for c in token if c not in _DIACRITICS)
    return bool(_ARABIC_LETTER.match(core)) or core in _LAM_ALEF


def is_letter_spaced(text: str) -> bool:
    tokens = text.split()
    letters = sum(1 for t in tokens if _is_letter(t))
    return letters >= 3 and letters >= 0.6 * len(tokens)


def join_spaced_letters(text: str) -> str:
    if not is_letter_spaced(text):
        return text
    out: list[str] = []
    run: list[str] = []
    for token in text.split():
        if _is_letter(token):
            run.append(token)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(token)
    if run:
        out.append("".join(run))
    return " ".join(out)


def join_split_year(text: str) -> str:
    """The year in one piece: ``5 202`` is 2025. Measured on two Arabic digital fit documents,
    whose column headings carry it; applied to headings only (``repair_grid``)."""
    return _SPLIT_YEAR.sub(lambda m: m.group(2) + m.group(1), text)


def repair_text(text: str, *, visual: bool) -> str:
    return join_spaced_letters(restore_digits(text) if visual else text)


def repair_grid(grid: Grid, *, visual: bool) -> Grid:
    cells = []
    for cell in grid.cells:
        flags = list(cell.flags)
        text = restore_digits(cell.text) if visual else cell.text
        if text != cell.text:
            flags.append("digits_reversed")
        joined = join_spaced_letters(text)
        if joined != text:
            flags.append("letters_spaced")
        if cell.is_column_header:
            year = join_split_year(joined)
            if year != joined:
                flags.append("year_joined")
                joined = year
        cells.append(cell.model_copy(update={"text": joined, "flags": tuple(flags)}))
    return grid.model_copy(update={"cells": tuple(cells)})


def is_reorderable(text: str) -> bool:
    """A cell of two or more words with Arabic letters and no digit: a label or a heading. A
    date or an amount keeps its order, since digit runs are not reversed with the words. The
    letters are read after NFKC, as the lexicon reads them: some text layers store Arabic as
    presentation forms."""
    return (
        len(text.split()) >= 2
        and _ARABIC.search(unicodedata.normalize("NFKC", text)) is not None
        and not any(c.isdigit() for c in text)
    )


def _is_unordered_digit_cell(cell: GridCell) -> bool:
    """An Arabic cell of two or more words that holds digits and is not a column heading: it
    may be a label (``(٥ إيضاح) مدينة ذمم``) whose words came back reversed, and the rule
    cannot tell where its digit runs belong. Column headings are left out: they are dates and
    period captions, read apart from the lexicon."""
    return (
        not cell.is_column_header
        and len(cell.text.split()) >= 2
        and _ARABIC.search(unicodedata.normalize("NFKC", cell.text)) is not None
        and any(c.isdigit() for c in cell.text)
    )


def _reversed_words(text: str) -> str:
    """The words in the opposite order; a run of Latin words (``Total assets``) keeps its own."""
    units: list[list[str]] = []
    in_latin = False
    for word in text.split():
        latin = _LATIN.search(word) is not None
        if latin and in_latin:
            units[-1].append(word)
        else:
            units.append([word])
        in_latin = latin
    return " ".join(word for unit in reversed(units) for word in unit)


def _matches(text: str, index: LabelIndex) -> bool:
    return any(index.match_all(text, statement) for statement in StatementType)


def restore_word_order(grid: Grid, index: LabelIndex) -> Grid:
    """The grid with the words of each Arabic text cell in reading order when the table's
    cells come back reversed.

    Both readings of every candidate cell (``is_reorderable``, in any column) are tried against
    the lexicon. The table is reversed only when at least ``MIN_REVERSED_CELLS`` cells match
    solely with their words reversed and those outnumber the cells that match as printed by
    ``REVERSED_MAJORITY`` to one, so a table in reading order (any cell matches as printed) is
    never reversed by a few chance matches. A cell that matches as printed and not reversed is
    already in reading order and stays, whatever its table does.

    The grid says what was done, in ``flags``, for the statement built from it:
    ``words_reversed`` when the table was reversed, and ``word_order_uncertain`` when at least
    one cell matched only reversed and the table was not (too little evidence), or when the
    table was reversed and a cell with digits was left as it came, since the rule does not
    reorder around digit runs.
    """
    read = {
        (c.row, c.col): (_matches(c.text, index), _matches(_reversed_words(c.text), index))
        for c in grid.cells
        if is_reorderable(c.text)
    }
    backwards = sum(1 for printed, flipped in read.values() if flipped and not printed)
    forwards = sum(1 for printed, _ in read.values() if printed)
    if backwards < MIN_REVERSED_CELLS or backwards <= REVERSED_MAJORITY * forwards:
        if not backwards:
            return grid
        return grid.model_copy(update={"flags": (*grid.flags, WORD_ORDER_UNCERTAIN)})
    flags = [WORDS_REVERSED]
    if any(_is_unordered_digit_cell(c) for c in grid.cells):
        flags.append(WORD_ORDER_UNCERTAIN)
    cells = tuple(
        c.model_copy(update={"text": _reversed_words(c.text)})
        if (c.row, c.col) in read and not (read[c.row, c.col][0] and not read[c.row, c.col][1])
        else c
        for c in grid.cells
    )
    return grid.model_copy(update={"cells": cells, "flags": (*grid.flags, *flags)})
