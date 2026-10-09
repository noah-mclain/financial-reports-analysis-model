"""Read a statement column header into a :class:`Period`.

Headers in the golden set look like ``31 December 2025``, ``٣١ ديسمبر ٢٠٢٥م``,
``For the financial year ended 31 December 2025``, ``2023 (Restated)`` and bare ``2024``.

Whether a column is a balance at a date or a flow over a period is often not stated in the
header at all: a balance sheet and an income statement can both be headed ``31 December 2025``.
The caller knows which statement it is reading, so it passes ``default_kind``; explicit cues in
the text always win over that default.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

from fra_core.numbers import normalize_digits, strip_bidi
from fra_core.schemas.statement import Period, PeriodKind
from fra_core.units import unit_marker_spans

__all__ = ["PeriodInterpretation", "interpret_period", "parse_period"]

_MONTHS: dict[str, int] = {
    # English, full and abbreviated.
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    # Arabic, Egyptian and Gulf usage.
    "يناير": 1,
    "فبراير": 2,
    "مارس": 3,
    "أبريل": 4,
    "إبريل": 4,
    "ابريل": 4,
    "مايو": 5,
    "يونيو": 6,
    "يونية": 6,
    "يوليو": 7,
    "يولية": 7,
    "أغسطس": 8,
    "اغسطس": 8,
    "سبتمبر": 9,
    "أكتوبر": 10,
    "اكتوبر": 10,
    "نوفمبر": 11,
    "ديسمبر": 12,
    # Arabic, Levant usage.
    "كانون الثاني": 1,
    "شباط": 2,
    "آذار": 3,
    "اذار": 3,
    "نيسان": 4,
    "أيار": 5,
    "ايار": 5,
    "حزيران": 6,
    "تموز": 7,
    "آب": 8,
    "أيلول": 9,
    "ايلول": 9,
    "تشرين الأول": 10,
    "تشرين الاول": 10,
    "تشرين الثاني": 11,
    "تشرين الثاني ": 11,
    "كانون الأول": 12,
    "كانون الاول": 12,
}

# Longest first, so "كانون الأول" wins over "كانون" style prefixes and "sept" over "sep".
_MONTH_NAMES_BY_LENGTH = sorted(_MONTHS, key=len, reverse=True)

_DURATION_CUES: tuple[tuple[str, int], ...] = (
    ("three months", 3),
    ("3 months", 3),
    ("quarter", 3),
    ("ثلاثة أشهر", 3),
    ("ثلاثة اشهر", 3),
    ("six months", 6),
    ("6 months", 6),
    ("half year", 6),
    ("ستة أشهر", 6),
    ("ستة اشهر", 6),
    ("nine months", 9),
    ("9 months", 9),
    ("تسعة أشهر", 9),
    ("تسعة اشهر", 9),
    ("year ended", 12),
    ("year end", 12),
    ("full year", 12),
    ("twelve months", 12),
    ("للسنة", 12),
    ("السنة المنتهية", 12),
    ("عن السنة", 12),
)

_PERIOD_CUES: tuple[str, ...] = ("period ended", "للفترة", "الفترة المنتهية")

_RESTATED_CUES: tuple[str, ...] = ("restated", "re-stated", "معاد", "معدلة", "معدّلة")
_UNAUDITED_CUES: tuple[str, ...] = ("unaudited", "غير مراجعة", "غير مدققة")
_AUDITED_CUES: tuple[str, ...] = ("audited", "مراجعة", "مدققة")

# "هـ" marks a Hijri year. Converting the calendar is out of scope, so such a column is
# reported as unreadable rather than silently treated as Gregorian.
_HIJRI_CUES: tuple[str, ...] = ("هـ", "هجري", "ه‍")

_YEAR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?!\d)")
_SMALL_NUMBER = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")
# After a number, these make it a count of months ("6 months ended"), not a day.
_MONTH_COUNT = re.compile(r"\s*(?:months?|شهر|أشهر|اشهر|شهور)", re.IGNORECASE)


def parse_period(text: str, *, default_kind: PeriodKind = PeriodKind.INSTANT) -> Period | None:
    """Read one column header. Returns ``None`` when no period can be read from it."""
    if not text or not text.strip():
        return None

    cleaned = unicodedata.normalize("NFKC", strip_bidi(text)).strip()
    cleaned, _ = normalize_digits(cleaned)
    lowered = cleaned.casefold()

    if any(cue in cleaned for cue in _HIJRI_CUES):
        return None

    kind, months = _detect_kind(lowered, default_kind)
    end = _detect_end_date(cleaned, lowered)
    if end is None:
        return None

    restated = any(cue in lowered for cue in _RESTATED_CUES)
    audited: bool | None = None
    if any(cue in lowered for cue in _UNAUDITED_CUES):
        audited = False
    elif any(cue in lowered for cue in _AUDITED_CUES):
        audited = True

    return Period(
        key=_make_key(end, kind, months),
        end_date=end,
        kind=kind,
        months=months if kind is PeriodKind.DURATION else None,
        restated=restated,
        audited=audited,
    )


def _detect_kind(lowered: str, default_kind: PeriodKind) -> tuple[PeriodKind, int]:
    """Explicit wording wins; otherwise the caller's default for this statement applies."""
    for cue, months in _DURATION_CUES:
        if cue in lowered:
            return PeriodKind.DURATION, months
    if any(cue in lowered for cue in _PERIOD_CUES):
        return PeriodKind.DURATION, 12
    return default_kind, 12


def _detect_end_date(cleaned: str, lowered: str) -> date | None:
    iso = _ISO_DATE.search(cleaned)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    dmy = _DMY.search(cleaned)
    if dmy:
        return _safe_date(int(dmy.group(3)), int(dmy.group(2)), int(dmy.group(1)))

    year_match = _YEAR.search(cleaned)
    if not year_match:
        return None
    year = int(year_match.group(1))

    month = _find_month(lowered)
    if month is None:
        # A bare year column means the end of that reporting year.
        return _safe_date(year, 12, 31)

    day = _find_day(cleaned, year_match.span())
    if day is None:
        day = calendar.monthrange(year, month)[1]
    return _safe_date(year, month, day)


def _find_month(lowered: str) -> int | None:
    for name in _MONTH_NAMES_BY_LENGTH:
        if name in lowered:
            return _MONTHS[name]
    return None


def _find_day(cleaned: str, year_span: tuple[int, int]) -> int | None:
    """The first one or two digit number that is not part of the year or a count of months."""
    for match in _SMALL_NUMBER.finditer(cleaned):
        if match.start() >= year_span[0] and match.end() <= year_span[1]:
            continue
        if _MONTH_COUNT.match(cleaned, match.end()):
            continue
        return int(match.group(1))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _make_key(end: date, kind: PeriodKind, months: int) -> str:
    if kind is PeriodKind.INSTANT:
        return end.isoformat()
    if months == 12:
        return f"FY{end.year}"
    return f"{months}M-{end.isoformat()}"


@dataclass(frozen=True)
class PeriodInterpretation:
    """Printed facts before the compatibility parser supplies a date or length default.

    ``reference_year`` on the interpreter may complete a caption's day/month, never a
    malformed date. Invalid facts remain distinct from missing facts and cannot be filled.
    """

    calendar: Literal["complete", "month_year", "year_only", "missing", "invalid"]
    duration: Literal["explicit", "generic", "none", "invalid"]
    end_date: date | None
    months: int | None
    date_text: str | None
    period: Period | None


_MONTH_NAME = "(?:" + "|".join(re.escape(n) for n in _MONTH_NAMES_BY_LENGTH) + ")"
_TEXT_DATE = re.compile(
    rf"(?<!\w)(?:\d{{1,2}}\s+{_MONTH_NAME}[\s.,]*\d{{4}}|"
    rf"{_MONTH_NAME}\s+\d{{1,2}}[,\s]+\d{{4}}|{_MONTH_NAME}[\s.]+\d{{4}})(?!\d)",
    re.I,
)
_TEXT_DATE_TAIL = re.compile(rf"(?<!\w)\d{{1,2}}\s+{_MONTH_NAME}(?!\w)", re.I)
# Strict observations require one separator; the compatibility parser keeps _DMY.
_STRICT_DMY = re.compile(r"(?<!\d)(\d{1,2})([/\-.])(\d{1,2})\2(\d{4})(?!\d)")
_DATE_SHAPE = re.compile(r"(?<!\w)\d{1,2}\s+[^\W\d_]+(?:\s+[^\W\d_]+)?(?:\s+\d{4})?(?!\w)")
_MONTH_WORD = re.compile(
    r"(?<!\w)(?:" + _MONTH_COUNT.pattern.removeprefix(r"\s*") + r"|شهرين)(?!\w)", re.I
)
_SUPPORTED_MONTHS = frozenset(months for _, months in _DURATION_CUES)
_STATED_MONTH_COUNT = re.compile(
    r"(?<![\w.])([-+]?\d+(?:\.\d+)?)" + _MONTH_COUNT.pattern + r"(?!\w)", re.I
)
_NEGATION = re.compile(r"(?<!\w)(?:not|no|غير|ليس)(?!\w)", re.I)
# Grammar glue, rather than additional month or duration vocabulary.
_GLUE = re.compile(
    r"(?<!\w)(?:for|the|financial|ended|end|as|at|of|on|in|and|عن|في|المنتهية|"
    r"إدراجها|ادراجها|المالية|المنتهيه|م)(?!\w)",
    re.I,
)


def _cue_pattern(cue: str) -> re.Pattern[str]:
    boundary = r"(?<![\w.+-])" if cue[0].isdigit() else r"(?<!\w)"
    return re.compile(rf"{boundary}{re.escape(cue)}(?!\w)", re.I)


def _masked_text(text: str, covered: bytearray) -> str:
    return "".join(" " if mask else char for char, mask in zip(text, covered, strict=True))


def _stated_months(token: str) -> int:
    if not token.isdecimal():
        return -1
    # Compare canonical decimal digits without converting an unbounded integer. Leading
    # zeroes do not change a count, including zeroes outside the Arabic digit alphabets.
    digits = "".join(str(unicodedata.decimal(char)) for char in token).lstrip("0")
    return next((months for months in _SUPPORTED_MONTHS if digits == str(months)), -1)


def _duration_description(
    text: str,
) -> tuple[Literal["explicit", "generic", "none", "invalid"], int | None, str]:
    lengths: set[int] = set()
    covered = bytearray(len(text))
    for cue, months in _DURATION_CUES:
        for match in _cue_pattern(cue).finditer(text):
            # Existing cue precedence handles nested wording such as half year ended.
            start, end = match.span()
            if not any(covered[start:end]):
                lengths.add(months)
            # Cue width and vocabulary are fixed; overlap checks and writes are bounded.
            covered[start:end] = b"\x01" * (end - start)
    remainder = _masked_text(text, covered)
    # Use the same month-count vocabulary as the day detector, but validate whole counts.
    for match in _STATED_MONTH_COUNT.finditer(remainder):
        lengths.add(_stated_months(match[1]))
    remainder = _STATED_MONTH_COUNT.sub(" ", remainder)
    generic = any(_cue_pattern(cue).search(text) for cue in _PERIOD_CUES)
    for cue in _PERIOD_CUES:
        remainder = _cue_pattern(cue).sub(" ", remainder)
    invalid = -1 in lengths or len(lengths) > 1 or bool(_MONTH_WORD.search(remainder))
    duration_words = "|".join(re.escape(cue) for cue, _ in _DURATION_CUES)
    if re.search(
        _NEGATION.pattern
        + rf"\s+(?:the\s+)?(?:{duration_words}|"
        + _STATED_MONTH_COUNT.pattern
        + ")",
        text,
        re.I,
    ):
        invalid = True
    if invalid:
        # Mask unsupported count words too so duration and calendar remain independent.
        # Start only at digit/letter runs, retaining glued-token residue while avoiding
        # repeated scans of every suffix in a long run when no month word follows.
        remainder = re.sub(
            r"(?:(?<!\d)[-+]?\d+(?:\.\d+)?|(?<![^\W\d_])[^\W\d_]+)\s*" + _MONTH_WORD.pattern,
            " ",
            remainder,
            flags=re.I,
        )
        return "invalid", None, _NEGATION.sub(" ", remainder)
    if lengths:
        return "explicit", next(iter(lengths)), remainder
    return ("generic" if generic else "none"), None, remainder


def _calendar_remainder(text: str) -> str:
    covered = bytearray(len(text))
    for start, end in unit_marker_spans(text):
        covered[start:end] = b"\x01" * (end - start)
    text = _masked_text(text, covered)
    for cue in (*_RESTATED_CUES, *_UNAUDITED_CUES, *_AUDITED_CUES):
        text = _cue_pattern(cue).sub(" ", text)
    text = _GLUE.sub(" ", text)
    tokens = []
    for token in text.split():
        bare = token.strip("()[].,:;،؛'\"")
        if bare.casefold() == "x":
            continue
        tokens.append(token)
    return " ".join(tokens).strip(" ()[].,:;،؛…")


def interpret_period(
    text: str,
    *,
    default_kind: PeriodKind = PeriodKind.INSTANT,
    reference_year: int | None = None,
) -> PeriodInterpretation:
    """Conservatively interpret one observation using the existing Gregorian grammar.

    Supported lengths are 3/6/9/12 months and the existing annual/quarter cues. Negated,
    contradictory and unsupported counts are invalid. This is bounded header grammar,
    not natural-language date inference; ``parse_period`` keeps its permissive defaults.
    """
    cleaned = unicodedata.normalize("NFKC", strip_bidi(text)).strip()
    cleaned, _ = normalize_digits(cleaned)
    duration, months, calendar_text = _duration_description(cleaned)
    calendar_fact: Literal["complete", "month_year", "year_only", "missing", "invalid"]
    end: date | None = None
    date_text: str | None = None
    matches = list(_ISO_DATE.finditer(calendar_text)) or list(_STRICT_DMY.finditer(calendar_text))
    if not matches:
        matches = list(_TEXT_DATE.finditer(calendar_text))
    if matches:
        match = matches[0]
        date_text = match.group()
        end = _detect_end_date(match.group(), match.group().casefold())
        remainder = calendar_text[: match.start()] + " " + calendar_text[match.end() :]
        calendar_fact = "complete"
        if (
            _ISO_DATE.fullmatch(match.group()) is None
            and _STRICT_DMY.fullmatch(match.group()) is None
        ):
            year_match = _YEAR.search(match.group())
            if year_match is not None and _find_day(match.group(), year_match.span()) is None:
                calendar_fact = "month_year"
        if len(matches) != 1 or _calendar_remainder(remainder) or end is None:
            calendar_fact = "invalid"
    else:
        year = _YEAR.search(calendar_text)
        tail = _TEXT_DATE_TAIL.search(calendar_text)
        shape = _DATE_SHAPE.search(calendar_text)
        if tail and not year:
            date_text = tail.group()
            if reference_year is not None:
                end = _detect_end_date(
                    f"{date_text} {reference_year}", f"{date_text.casefold()} {reference_year}"
                )
            remainder = calendar_text[: tail.start()] + " " + calendar_text[tail.end() :]
            calendar_fact = "complete" if end and not _calendar_remainder(remainder) else "invalid"
        elif year:
            date_text = year.group()
            end = _safe_date(int(year.group()), 12, 31)
            remainder = calendar_text[: year.start()] + " " + calendar_text[year.end() :]
            calendar_fact = "year_only" if end and not _calendar_remainder(remainder) else "invalid"
        elif re.search(r"\d", calendar_text) or shape:
            date_text = shape.group() if shape else None
            calendar_fact = "invalid"
        else:
            calendar_fact = "missing"
    if any(cue in cleaned for cue in _HIJRI_CUES):
        calendar_fact = "invalid"
    if calendar_fact == "invalid":
        end = None
    period = None
    if end is not None and duration not in {"invalid", "generic"}:
        kind = PeriodKind.DURATION if duration == "explicit" else default_kind
        length = months or 12
        parsed = parse_period(cleaned, default_kind=kind)
        period = Period(
            key=_make_key(end, kind, length),
            end_date=end,
            kind=kind,
            months=length if kind is PeriodKind.DURATION else None,
            restated=parsed.restated if parsed else False,
            audited=parsed.audited if parsed else None,
        )
    return PeriodInterpretation(calendar_fact, duration, end, months, date_text, period)
