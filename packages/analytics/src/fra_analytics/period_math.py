"""Calendar arithmetic for periods: where a period began, which period came a year before."""

from __future__ import annotations

import calendar
from datetime import date


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def months_before(end: date, months: int) -> date:
    """The date ``months`` months before ``end``. A month end maps to a month end (30 June less
    6 months is 31 December), any other day keeps its number, clamped to the shorter month."""
    if months < 0:
        msg = f"cannot count back a negative number of months: {months}"
        raise ValueError(msg)
    index = end.year * 12 + (end.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    last = _month_end(year, month)
    if end == _month_end(end.year, end.month):
        return last
    return date(year, month, min(end.day, last.day))
