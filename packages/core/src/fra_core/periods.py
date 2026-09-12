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
from datetime import date

from fra_core.numbers import normalize_digits, strip_bidi
from fra_core.schemas.statement import Period, PeriodKind

__all__ = ["parse_period"]

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
    """The first one or two digit number that is not part of the year."""
    for match in _SMALL_NUMBER.finditer(cleaned):
        if match.start() >= year_span[0] and match.end() <= year_span[1]:
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
