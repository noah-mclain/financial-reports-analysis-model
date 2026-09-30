"""Header rows to periods, the label column and the note column (spec 11, Data flow step 5).

Columns are bound to periods by what their header says, never by position, so a mirrored
Arabic table needs no special case: the label column is simply the one holding the most text.
"""

from __future__ import annotations

import calendar
import re

from pydantic import BaseModel, ConfigDict, Field

from fra_core.numbers import normalize_digits, parse_number
from fra_core.periods import parse_period
from fra_core.schemas import Period, PeriodKind, StatementType
from fra_core.units import detect_currency, detect_scale
from fra_ingest.label_match import squash
from fra_ingest.table_grid import Grid

_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_NOTE = re.compile(
    r"^\(?\d{1,2}(\s*[-.]\s*\d{1,2})?\)?(\s*[-,]\s*\(?\d{1,2}(\s*[-.]\s*\d{1,2})?\)?)*$"
)
_NOTE_HEADERS = frozenset(squash(w) for w in ("note", "notes", "إيضاح", "إيضاحات"))
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_MARKERS = frozenset({"restated", "audited", "unaudited", "معدلة", "مدققة", "غير", "'000"})
_DURATION_TYPES = frozenset(
    {
        StatementType.INCOME,
        StatementType.COMPREHENSIVE_INCOME,
        StatementType.CASH_FLOW,
        StatementType.EQUITY,
    }
)


def _plain(text: str) -> str:
    return normalize_digits(text)[0].strip()


def is_note_ref(text: str) -> bool:
    return bool(text) and _NOTE.match(_plain(text)) is not None


def is_amount(text: str) -> bool:
    """A value: parses as a number and is neither a date nor a bare year."""
    if not text or parse_period(text) is not None or _YEAR.fullmatch(_plain(text)):
        return False
    return parse_number(text).value is not None


def split_note(label: str) -> tuple[str, str | None]:
    """A label ending in a note reference, as Almarai AR prints them: (label, note)."""
    head, _, tail = label.rpartition(" ")
    if head and is_note_ref(tail) and not _YEAR.fullmatch(_plain(tail)):
        return head, tail
    return label, None


class HeaderLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header_rows: list[int] = Field(default_factory=list)
    label_col: int | None = None
    note_col: int | None = None
    value_cols: dict[int, Period] = Field(default_factory=dict)
    unbound_cols: list[int] = Field(default_factory=list)
    header_texts: dict[int, str] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)

    def data_rows(self, grid: Grid) -> list[int]:
        return [r for r in range(grid.num_rows) if r not in self.header_rows]


def _row_has_amount(grid: Grid, row: int) -> bool:
    return any(is_amount(grid.text(row, col)) for col in range(grid.num_cols))


def _is_header_text(text: str) -> bool:
    return parse_period(text) is not None or squash(text) in _NOTE_HEADERS


def _is_marker(token: str) -> bool:
    bare = token.strip("()[]\"'.,:").lower()
    return (
        token.lower() in _MARKERS
        or bare in _MARKERS
        or detect_currency(token) is not None
        or detect_scale(token) is not None
    )


def _is_sub_header_text(text: str) -> bool:
    """Scale, currency or restated/audited wording, as printed under a date row."""
    if detect_scale(text) is not None or detect_currency(text) is not None:
        return True
    tokens = text.split()
    return bool(tokens) and all(_is_marker(t) for t in tokens)


def _header_rows(grid: Grid) -> list[int]:
    """Rows docling flags as column headers, plus rows above the first amount that carry a
    date or a notes heading, and sub-rows of scale, currency or restated wording below one.
    A section row such as "Non-current assets" is not a header."""
    rows = {c.row for c in grid.cells if c.is_column_header}
    label = _label_guess(grid)
    for row in range(grid.num_rows):
        if _row_has_amount(grid, row):
            break
        cells = [grid.text(row, c) for c in range(grid.num_cols) if c != label]
        texts = [t for t in cells if t]
        if any(_is_header_text(grid.text(row, c)) for c in range(grid.num_cols)) or (
            rows and texts and all(_is_sub_header_text(t) for t in texts)
        ):
            rows.add(row)
    return sorted(r for r in rows if not _row_has_amount(grid, r))


def _label_guess(grid: Grid) -> int | None:
    """The column with the most text; only used to ignore labels when testing sub-rows."""
    counts = {
        c: sum(
            1
            for r in range(grid.num_rows)
            if _LETTER.search(grid.text(r, c)) and not is_amount(grid.text(r, c))
        )
        for c in range(grid.num_cols)
    }
    return max(counts, key=lambda c: counts[c]) if any(counts.values()) else None


def _year_only(header: str) -> bool:
    """True when, besides the year, the header holds only currency, scale or status markers."""
    rest = _YEAR.sub(" ", _plain(header), count=1)
    tokens = [t for t in rest.split() if not _is_marker(t)]
    return bool(_YEAR.search(_plain(header))) and not re.search(
        r"[^\W_]", "".join(tokens).strip("()[]\"'.,:")
    )


def _with_date_hint(period: Period, header: str, hint: Period | None) -> Period:
    """A header naming only a year takes the day and month of the statement's date line."""
    if hint is None or not _year_only(header):
        return period
    month = calendar.month_name[hint.end_date.month]
    text = f"{hint.end_date.day} {month} {period.end_date.year}"
    if period.kind is PeriodKind.DURATION:
        text = f"For the year ended {text}"
    moved = parse_period(text, default_kind=period.kind)
    return moved.model_copy(update={"restated": period.restated}) if moved else period


def parse_header(grid: Grid, statement_type: StatementType, date_hint: str | None) -> HeaderLayout:
    layout = HeaderLayout(header_rows=_header_rows(grid))
    data = layout.data_rows(grid)
    kind = PeriodKind.DURATION if statement_type in _DURATION_TYPES else PeriodKind.INSTANT
    hint = parse_period(date_hint, default_kind=kind) if date_hint else None

    for col in range(grid.num_cols):
        parts = [grid.text(r, col) for r in layout.header_rows]
        joined = " ".join(p for i, p in enumerate(parts) if p and p not in parts[:i])
        if joined:
            layout.header_texts[col] = joined

    texts = {
        col: sum(
            1
            for r in data
            if _LETTER.search(grid.text(r, col)) and not is_amount(grid.text(r, col))
        )
        for col in range(grid.num_cols)
    }
    amounts = {
        col: sum(1 for r in data if is_amount(grid.text(r, col))) for col in range(grid.num_cols)
    }
    if any(texts.values()):
        layout.label_col = max(texts, key=lambda c: (texts[c], -amounts[c]))

    others = [c for c in range(grid.num_cols) if c != layout.label_col]
    named = [c for c in others if squash(layout.header_texts.get(c, "")) in _NOTE_HEADERS]
    if named:
        layout.note_col = named[0]
    elif layout.label_col is not None:
        amount_cols = [c for c in others if amounts[c]]
        for col in (layout.label_col - 1, layout.label_col + 1):
            cells = [grid.text(r, col) for r in data if grid.text(r, col)]
            if len(cells) >= 2 and all(is_note_ref(t) for t in cells) and len(amount_cols) >= 3:
                layout.note_col = col
                break

    for col in others:
        if col == layout.note_col or amounts[col] == 0:
            continue
        header = layout.header_texts.get(col, "")
        period = parse_period(header, default_kind=kind) if header else None
        if period is None:
            layout.unbound_cols.append(col)
            layout.evidence.append(f"period_unbound:{col}")
            continue
        period = _with_date_hint(period, header, hint)
        if any(
            (p.key, p.restated) == (period.key, period.restated) for p in layout.value_cols.values()
        ):
            layout.unbound_cols.append(col)
            layout.evidence.append(f"duplicate_period:{col}:{period.key}")
            continue
        layout.value_cols[col] = period
    return layout
