"""Header strings taken from the golden set documents."""

from datetime import date

import pytest

from fra_core.periods import parse_period
from fra_core.schemas.statement import PeriodKind


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("31 December 2025", date(2025, 12, 31)),
        ("31 Dec 2025", date(2025, 12, 31)),
        ("31 Dec.2025", date(2025, 12, 31)),  # as printed by Juhayna
        ("December 31, 2025", date(2025, 12, 31)),
        ("2025-12-31", date(2025, 12, 31)),
        ("31/12/2025", date(2025, 12, 31)),
        ("30 June 2025", date(2025, 6, 30)),
        ("٣١ ديسمبر ٢٠٢٥", date(2025, 12, 31)),  # Almarai Arabic edition
        ("31 ديسمبر 2024", date(2024, 12, 31)),
        ("30 يونيو 2025", date(2025, 6, 30)),
        ("٣١ كانون الأول ٢٠٢٥", date(2025, 12, 31)),  # Levant month name
    ],
)
def test_reads_end_date(text: str, expected: date) -> None:
    period = parse_period(text)
    assert period is not None
    assert period.end_date == expected


def test_bare_year_ends_at_year_end() -> None:
    period = parse_period("2024")
    assert period is not None
    assert period.end_date == date(2024, 12, 31)


def test_balance_sheet_column_is_an_instant_by_default() -> None:
    """A balance sheet and an income statement can share the header '31 December 2025'."""
    period = parse_period("31 December 2025", default_kind=PeriodKind.INSTANT)
    assert period is not None
    assert period.kind is PeriodKind.INSTANT
    assert period.months is None
    assert period.key == "2025-12-31"


def test_explicit_duration_wording_overrides_the_default() -> None:
    period = parse_period(
        "For the financial year ended 31 December 2025", default_kind=PeriodKind.INSTANT
    )
    assert period is not None
    assert period.kind is PeriodKind.DURATION
    assert period.months == 12
    assert period.key == "FY2025"


@pytest.mark.parametrize(
    ("text", "months"),
    [
        ("Six months ended 30 June 2025", 6),
        ("Three months ended 31 March 2025", 3),
        ("Nine months ended 30 September 2025", 9),
        ("للسنة المنتهية في 31 ديسمبر 2025", 12),
        ("ستة أشهر المنتهية في 30 يونيو 2025", 6),
    ],
)
def test_period_length(text: str, months: int) -> None:
    period = parse_period(text)
    assert period is not None
    assert period.kind is PeriodKind.DURATION
    assert period.months == months


def test_interim_key_is_distinct_from_the_annual_one() -> None:
    period = parse_period("Six months ended 30 June 2025")
    assert period is not None
    assert period.key == "6M-2025-06-30"


@pytest.mark.parametrize("text", ["2023 (Restated)", "2023 معاد إدراجها"])
def test_restated_is_recorded(text: str) -> None:
    period = parse_period(text)
    assert period is not None
    assert period.restated is True


def test_unaudited_is_recorded() -> None:
    period = parse_period("30 June 2025 (Unaudited)")
    assert period is not None
    assert period.audited is False


def test_hijri_dates_are_refused_rather_than_assumed_gregorian() -> None:
    """Converting the Hijri calendar is out of scope, and guessing would shift the year."""
    assert parse_period("٣٠ يونيو ١٤٤٦هـ") is None


@pytest.mark.parametrize("text", ["", "   ", "Notes", "إيضاحات", "total"])
def test_non_period_headers_return_none(text: str) -> None:
    assert parse_period(text) is None


def test_impossible_date_returns_none() -> None:
    assert parse_period("31 February 2025") is None
