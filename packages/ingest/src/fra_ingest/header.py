"""Header rows to periods, the label column and the note column (spec 11, Data flow step 5).

Columns are bound to periods by what their header says, never by position, so a mirrored
Arabic table needs no special case: the label column is simply the one holding the most text.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from fra_core.numbers import normalize_digits, parse_number
from fra_core.periods import interpret_period, parse_period
from fra_core.schemas import Period, PeriodKind, StatementType, TextSource
from fra_core.schemas.statement import PeriodCandidate, PeriodConflict, PeriodEvidence
from fra_core.units import detect_currency, detect_scale
from fra_ingest.label_match import squash
from fra_ingest.period_evidence import period_identity, unresolved, validate_evidence
from fra_ingest.table_grid import Grid
from fra_ingest.visual_order import join_split_year, repair_text

_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")
_NOTE = re.compile(
    r"^\(?\d{1,2}(\s*[-.]\s*\d{1,2})?\)?(\s*[-,]\s*\(?\d{1,2}(\s*[-.]\s*\d{1,2})?\)?)*$"
)
_NOTE_HEADERS = frozenset(
    squash(w) for w in ("note", "notes", "note no.", "إيضاح", "إيضاحات", "إيضاح رقم")
)
# Digit groups after the first are thousands groups: "2 077 685 182", "٢٠٧٧ ٦٨٥ ١٨٢".
_GROUPED = re.compile(r"[(\-]?\d+(?:[\s,\u066c\u060c]\d{3})+(?:\.\d+)?\)?")
# A number after one of these names a standard, a level or a stage, not a note.
_NUMBERED_TERMS = frozenset(
    {"ifrs", "ias", "ifric", "sic", "eas", "level", "tier", "stage", "phase"}
    | {"المعيار", "المستوى", "المرحلة"}
)
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


def _grouped_amount(text: str) -> bool:
    """A number printed in digit groups, which OCR can make look like a year and more."""
    return _GROUPED.fullmatch(_plain(text)) is not None and parse_number(text).value is not None


def is_amount(text: str) -> bool:
    """A value: parses as a number and is neither a date nor a bare year."""
    if not text:
        return False
    if _grouped_amount(text):
        return True
    if parse_period(text) is not None or _YEAR.fullmatch(_plain(text)):
        return False
    return parse_number(text).value is not None


def split_note(label: str) -> tuple[str, str | None]:
    """A label ending in a note reference, as Almarai AR prints them: (label, note). A number
    that closes a bracket opened earlier, or follows a word such as IFRS or Level, belongs to
    the label."""
    head, _, tail = label.rpartition(" ")
    if not head or not is_note_ref(tail) or _YEAR.fullmatch(_plain(tail)):
        return label, None
    if tail.count("(") != tail.count(")"):
        return label, None
    if head.split()[-1].strip("()[],.:-").casefold() in _NUMBERED_TERMS:
        return label, None
    return head, tail


class HeaderLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_conflicts: tuple[PeriodConflict, ...] = ()
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
    if _grouped_amount(text):
        return False
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


_INTERIM_LENGTHS = {3: "three", 6: "six", 9: "nine"}


def _with_date_hint(period: Period, header: str, hint: Period | None) -> Period:
    """A header naming only a year takes the day and month of the statement's date line, and
    for a duration its length: a year, or the three, six or nine months the line names."""
    if hint is None or not _year_only(header):
        return period
    month = calendar.month_name[hint.end_date.month]
    text = f"{hint.end_date.day} {month} {period.end_date.year}"
    if period.kind is PeriodKind.DURATION:
        length = _INTERIM_LENGTHS.get(hint.months or 12)
        text = f"For the {length} months ended {text}" if length else f"For the year ended {text}"
    moved = parse_period(text, default_kind=period.kind)
    return moved.model_copy(update={"restated": period.restated}) if moved else period


def has_period_context(text: str) -> bool:
    return interpret_period(text).duration != "none"


def printed_date(text: str) -> str | None:
    """Return the core interpretation's date fragment, including partial dates."""
    facts = interpret_period(text)
    return facts.date_text if facts.calendar != "year_only" else None


def printed_period(text: str, kind: PeriodKind = PeriodKind.INSTANT) -> Period | None:
    return interpret_period(text, default_kind=kind).period


def is_printed_period_label(text: str) -> bool:
    facts = interpret_period(text)
    return facts.period is not None and facts.calendar in {"complete", "year_only"}


def period_with_context(
    header: str,
    context: str,
    kind: PeriodKind,
    *,
    match_caption_date: bool = False,
    preserve_explicit: bool = False,
) -> Period | None:
    """Fill missing facts from context without healing or rewriting printed facts."""
    facts = interpret_period(header, default_kind=kind)
    period = facts.period
    if facts.end_date is None or facts.duration == "invalid" or not context:
        return period
    caption = interpret_period(context, default_kind=kind, reference_year=facts.end_date.year)
    if caption.calendar == "invalid" or caption.duration == "invalid":
        return None
    if facts.duration == "generic":
        if caption.duration != "explicit":
            return None
        # Resolve only the proved missing length through strict grammar, so reporting
        # status keeps the same token boundaries as every other observation.
        period = interpret_period(
            f"{caption.months} months ended {header}", default_kind=kind
        ).period
    if period is None:
        return None
    if caption.calendar in {"complete", "month_year"}:
        # Validate the caption's printed year first, then each comparative year separately.
        dated = _YEAR.sub(str(period.end_date.year), caption.date_text or "")
        if not _YEAR.search(dated):
            dated = f"{dated} {period.end_date.year}"
        comparative = interpret_period(dated, default_kind=kind).period
        if comparative is None:
            return None
        if facts.calendar == "year_only":
            period = period.model_copy(update={"end_date": comparative.end_date})
        elif match_caption_date and (period.end_date.month, period.end_date.day) != (
            comparative.end_date.month,
            comparative.end_date.day,
        ):
            return period if preserve_explicit else None
    elif facts.calendar == "year_only":
        return None
    if (
        caption.duration == "generic"
        and period.kind is PeriodKind.DURATION
        and facts.duration != "explicit"
    ):
        # Recovery cannot infer an annual length from an unspecified period caption.
        return None
    length = period.months or 12
    if period.kind is PeriodKind.DURATION and facts.duration != "explicit":
        length = caption.months or length
    dated = period.end_date.isoformat()
    if period.kind is PeriodKind.DURATION:
        dated = f"{length} months ended {dated}"
    resolved = printed_period(dated, period.kind)
    return (
        resolved.model_copy(update={"restated": period.restated, "audited": period.audited})
        if resolved is not None
        else None
    )


def parse_header(
    grid: Grid,
    statement_type: StatementType,
    date_hint: str | None,
    *,
    period_evidence: Sequence[PeriodEvidence] = (),
    visual: bool = False,
) -> HeaderLayout:
    layout = HeaderLayout(header_rows=_header_rows(grid))
    validate_evidence(grid, period_evidence, layout.header_rows)
    data = layout.data_rows(grid)
    kind = PeriodKind.DURATION if statement_type in _DURATION_TYPES else PeriodKind.INSTANT
    hint = parse_period(date_hint, default_kind=kind) if date_hint else None
    recovered_context = " ".join(c.text for c in grid.recovered_context)

    for col in range(grid.num_cols):
        parts = [c.text for c in grid.recovered_headers if c.col == col]
        parts += [grid.text(r, col) for r in layout.header_rows]
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

    supplied = {e.col: e for e in period_evidence}
    if any(col not in others or col == layout.note_col or not amounts[col] for col in supplied):
        raise ValueError("period evidence must name a value column")

    for col in others:
        if col == layout.note_col or amounts[col] == 0:
            continue
        header = layout.header_texts.get(col, "")
        recovered = next((c.text for c in grid.recovered_headers if c.col == col), None)
        if recovered:
            period = period_with_context(
                recovered, recovered_context, kind, match_caption_date=True
            )
        else:
            period = parse_period(header, default_kind=kind) if header else None
            if period is not None:
                period = _with_date_hint(period, header, hint)
        if col in supplied:
            context = recovered_context if recovered else (date_hint or "")
            period = period_with_context(
                recovered or header,
                context,
                kind,
                match_caption_date=bool(recovered),
                preserve_explicit=not bool(recovered),
            )
            candidates = []
            explicit_durations: set[str] = set()
            for observation in supplied[col].observations:
                text = repair_text(
                    observation.raw_text,
                    visual=visual and observation.provenance.source is TextSource.TEXT,
                )
                anchor = grid.cell(observation.provenance.row, col)
                if anchor is not None and anchor.is_column_header:
                    text = join_split_year(text)
                if interpret_period(text).duration == "explicit":
                    explicit_durations.add(observation.source_id)
                candidate = period_with_context(
                    text,
                    context,
                    kind,
                    match_caption_date=bool(recovered),
                    preserve_explicit=True,
                )
                candidates.append(PeriodCandidate(observation=observation, period=candidate))
            conflict = unresolved(tuple(candidates))
            omitted = period is not None and not any(
                c.period is not None and period_identity(c.period) == period_identity(period)
                for c in candidates
            )
            context_disagrees = False
            for reading in candidates:
                if reading.period is None:
                    continue
                caption = interpret_period(
                    context,
                    default_kind=kind,
                    reference_year=reading.period.end_date.year,
                )
                context_disagrees |= (
                    caption.duration == "explicit"
                    and reading.period.kind is PeriodKind.DURATION
                    and reading.observation.source_id in explicit_durations
                    and reading.period.months != caption.months
                ) or (
                    caption.calendar in {"complete", "month_year"}
                    and caption.end_date is not None
                    and (reading.period.end_date.month, reading.period.end_date.day)
                    != (caption.end_date.month, caption.end_date.day)
                )
            if conflict is None and (context_disagrees or (omitted and recovered)):
                conflict = PeriodConflict(
                    reason="conflicting_context", candidates=tuple(candidates)
                )
            if conflict is not None:
                layout.period_conflicts = (*layout.period_conflicts, conflict)
                layout.unbound_cols.append(col)
                layout.evidence.extend((f"period_unbound:{col}", f"period_conflict:{col}"))
                continue
            if omitted:
                raise ValueError(
                    f"period evidence column {col} omits the bound header interpretation"
                )
        if period is None:
            layout.unbound_cols.append(col)
            layout.evidence.append(f"period_unbound:{col}")
            continue
        bound = next((p for p in layout.value_cols.values() if p.key == period.key), None)
        if bound is not None:
            twin = period.restated or bound.restated
            layout.unbound_cols.append(col)
            layout.evidence.append(
                f"{'restated_duplicate' if twin else 'duplicate_period'}:{col}:{period.key}"
            )
            continue
        layout.value_cols[col] = period
    return layout
