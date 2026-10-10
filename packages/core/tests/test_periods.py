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


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("For the 6 months ended 30 June 2025", "6M-2025-06-30"),
        ("For the 9 months ended 30 September 2025", "9M-2025-09-30"),
        ("3 months ended 31 March 2025", "3M-2025-03-31"),
    ],
)
def test_a_month_count_in_digits_is_not_the_day(text: str, key: str) -> None:
    period = parse_period(text)
    assert period is not None
    assert period.key == key


@pytest.mark.parametrize(
    "text,calendar_fact,duration_fact,months,end",
    [
        ("31/12/3025", "complete", "none", None, "3025-12-31"),
        ("3025-12-31", "complete", "none", None, "3025-12-31"),
        ("31 December 3025", "complete", "none", None, "3025-12-31"),
        ("December 31, 2025", "complete", "none", None, "2025-12-31"),
        ("31 Dec.2025", "complete", "none", None, "2025-12-31"),
        ("٣١ كانون الأول ٢٠٢٥", "complete", "none", None, "2025-12-31"),
        ("June 2025", "month_year", "none", None, "2025-06-30"),
        ("3025 (Restated)", "year_only", "none", None, "3025-12-31"),
        ("2025 SAR '000", "year_only", "none", None, "2025-12-31"),
        ("2023 معاد إدراجها", "year_only", "none", None, "2023-12-31"),
        ("30/06 3025", "invalid", "none", None, None),
        ("30 06/2025", "invalid", "none", None, None),
        ("2025-06 30", "invalid", "none", None, None),
        ("31/12/202", "invalid", "none", None, None),
        ("31 December", "invalid", "none", None, None),
        ("31 Flober 3025", "invalid", "none", None, None),
        ("Flober 2025", "invalid", "none", None, None),
        ("31 February 2025", "invalid", "none", None, None),
        ("29 February 2024", "complete", "none", None, "2024-02-29"),
        ("29 February 2025", "invalid", "none", None, None),
        ("31/12/2025 7", "invalid", "none", None, None),
        ("30 يونيو 1446هـ", "invalid", "none", None, None),
        ("For the period ended 31 December 2025", "complete", "generic", None, "2025-12-31"),
        ("عن الفترة المنتهية في 31 ديسمبر 2025", "complete", "generic", None, "2025-12-31"),
        ("For the 3 months ended 31 December 2025", "complete", "explicit", 3, "2025-12-31"),
        ("For the 6 months ended 31 December 2025", "complete", "explicit", 6, "2025-12-31"),
        ("For the 9 months ended 31 December 2025", "complete", "explicit", 9, "2025-12-31"),
        ("For the 12 months ended 31 December 2025", "complete", "explicit", 12, "2025-12-31"),
        ("For the year ended 31 December 2025", "complete", "explicit", 12, "2025-12-31"),
        ("ستة أشهر المنتهية في 30 يونيو 2025", "complete", "explicit", 6, "2025-06-30"),
        ("For the 10 months ended 31 December 2025", "complete", "invalid", None, "2025-12-31"),
        ("For the 13 months ended 31 December 2025", "complete", "invalid", None, "2025-12-31"),
        ("For the two months ended 31 December 2025", "complete", "invalid", None, "2025-12-31"),
        (
            "For the unknown months ended 31 December 2025",
            "complete",
            "invalid",
            None,
            "2025-12-31",
        ),
        ("not three months ended 31 December 2025", "complete", "invalid", None, "2025-12-31"),
        (
            "three months and six months ended 31 December 2025",
            "complete",
            "invalid",
            None,
            "2025-12-31",
        ),
        ("Notes", "missing", "none", None, None),
        ("For the three months ended", "missing", "explicit", 3, None),
    ],
)
def test_interpretation_distinguishes_source_facts_from_defaults(
    text: str, calendar_fact: str, duration_fact: str, months: int | None, end: str | None
) -> None:
    from fra_core.periods import interpret_period

    facts = interpret_period(text)
    assert facts.calendar == calendar_fact
    assert facts.duration == duration_fact
    assert facts.months == months
    assert (facts.end_date.isoformat() if facts.end_date else None) == end
    if calendar_fact == "invalid" or duration_fact == "invalid":
        assert facts.period is None


def test_strict_interpretation_does_not_change_permissive_parser() -> None:
    from fra_core.periods import interpret_period

    text = "For the 10 months ended 31 December 2025"
    assert interpret_period(text).period is None
    period = parse_period(text, default_kind=PeriodKind.DURATION)
    assert period is not None and period.months == 12


@pytest.mark.parametrize(
    "digits", ["0123456789", "٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹", "०१२३४५६७८९", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"]
)
@pytest.mark.parametrize(
    "first,second", [("/", "-"), ("/", "."), ("-", "/"), ("-", "."), (".", "/"), (".", "-")]
)
def test_strict_numeric_dmy_rejects_mixed_separators_but_preserves_legacy(
    digits: str, first: str, second: str
) -> None:
    from fra_core.periods import interpret_period

    text = f"31{first}12{second}2025".translate(str.maketrans("0123456789", digits))
    facts = interpret_period(text, reference_year=2025)
    assert facts.calendar == "invalid"
    assert facts.end_date is None
    assert facts.period is None
    legacy = parse_period(text)
    assert legacy is not None and legacy.end_date == date(2025, 12, 31)


@pytest.mark.parametrize(
    "digits", ["0123456789", "٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹", "०१२३४५६७८९", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫"]
)
@pytest.mark.parametrize("separator", ["/", "-", "."])
@pytest.mark.parametrize("day,month,year", [(31, 12, 2025), (1, 2, 2025), (29, 2, 2024)])
def test_strict_numeric_dmy_preserves_homogeneous_dates(
    digits: str, separator: str, day: int, month: int, year: int
) -> None:
    from fra_core.periods import interpret_period

    text = f"{day}{separator}{month}{separator}{year}".translate(
        str.maketrans("0123456789", digits)
    )
    facts = interpret_period(text)
    assert facts.calendar == "complete"
    assert facts.end_date == date(year, month, day)
    assert facts.period is not None and facts.period.end_date == facts.end_date


@pytest.mark.parametrize("separator", ["/", "-", "."])
@pytest.mark.parametrize("day,month", [(32, 12), (31, 4), (29, 2), (0, 12), (31, 0), (31, 13)])
def test_strict_numeric_dmy_rejects_impossible_dates(separator: str, day: int, month: int) -> None:
    from fra_core.periods import interpret_period

    facts = interpret_period(f"{day}{separator}{month}{separator}2025")
    assert facts.calendar == "invalid"
    assert facts.end_date is None
    assert facts.period is None


@pytest.mark.parametrize(
    "text",
    [
        "131/12/2025",
        "31/12/20250",
        "31/12-2025-12-31",
        "31/12/2025-12.2025",
        "31/12-31.12.2025",
        "31.12.2025/12-2025",
        "31/12-2025 (Audited) SAR '000",
    ],
)
def test_strict_numeric_date_match_cannot_hide_malformed_residue(text: str) -> None:
    from fra_core.periods import interpret_period

    facts = interpret_period(text)
    assert facts.calendar == "invalid"
    assert facts.end_date is None
    assert facts.period is None


@pytest.mark.parametrize("count", ["-3", "+3", "0", "3.5", "13", "103", "thirteen", "شهرين"])
def test_nonsensical_or_unsupported_length_is_explicitly_invalid(count: str) -> None:
    from fra_core.periods import interpret_period

    text = (
        f"For the {count} months ended 31 December 2025"
        if count != "شهرين"
        else "عن شهرين المنتهية في 31 ديسمبر 2025"
    )
    facts = interpret_period(text)
    assert facts.duration == "invalid"
    assert facts.period is None


def test_caption_year_completion_cannot_heal_missing_numeric_separator() -> None:
    from fra_core.periods import interpret_period

    assert interpret_period("30 June", reference_year=3025).end_date == date(3025, 6, 30)
    assert interpret_period("30/06 3025", reference_year=3025).period is None


@pytest.mark.parametrize("cue,months", [("full year ended", 12), ("half year ended", 6)])
def test_overlapping_duration_cues_describe_one_length(cue: str, months: int) -> None:
    from fra_core.periods import interpret_period

    facts = interpret_period(f"For the {cue} 30 June 2025")
    assert facts.duration == "explicit"
    assert facts.period is not None and facts.period.months == months


@pytest.mark.parametrize(
    "residue",
    [
        "SAR30/06",
        "2024SAR",
        "SR30/06",
        "30/06LE",
        "KD2024",
        "million30/06",
        "30/06million",
        "million2024",
        "(000)7",
        "'00030/06",
        "'000+1",
        "SAR 7",
        "7 million",
        "ريال30/06",
        "بالملايين2024",
        "SARunknown",
        "unknownmillion",
        "restated30/06",
        "30/06audited",
        "(Audited)30/06",
    ],
)
def test_marker_adjacent_residue_cannot_complete_a_date(residue: str) -> None:
    from fra_core.periods import interpret_period

    facts = interpret_period(f"31 December3025 {residue}")
    assert facts.calendar == "invalid"
    assert facts.period is None


@pytest.mark.parametrize("year", [2025, 3025])
@pytest.mark.parametrize(
    "marker",
    [
        "SR",
        "LE",
        "KD",
        "SAR",
        "EGP",
        "KWD",
        "sAr",
        "eGp",
        "kwd",
        "'000",
        "’000",
        "(000)",
        "SR '000",
        "LE (000)",
        "in millions",
        "USD millions",
        "in thousands of Egyptian pounds",
        "Saudi Riyals",
        "ريال سعودي",
        "بالريال السعودي",
        "الريالات",
        "بالجنيه المصري",
        "الجنيهات المصرية",
        "بآلاف",
        "بآالف",
        "بالملايين",
        "ريال سعودي بآلاف",
        "$ million",
        "€m",
        "US$m",
        "L.E.",
        "(Restated)",
        "(Unaudited)",
    ],
)
def test_valid_year_markers_preserve_case_and_complete_spans(year: int, marker: str) -> None:
    from fra_core.periods import interpret_period

    facts = interpret_period(f"{year} {marker}")
    assert facts.calendar == "year_only"
    assert facts.period is not None
    assert facts.period.end_date == date(year, 12, 31)


@pytest.mark.parametrize("marker", ["sr", "le", "kd", "Sr", "Le", "Kd"])
def test_lowercase_short_abbreviations_are_not_period_markers(marker: str) -> None:
    from fra_core.periods import interpret_period

    assert interpret_period(f"2025 {marker}").period is None


@pytest.mark.parametrize(
    "residue",
    [
        "بالريال السعودي30/06",
        "2024الريالات",
        "الريالات7",
        "unknownريال",
        "necessary",
        "الدينار الكويتي",
    ],
)
def test_attached_currency_markers_do_not_hide_invalid_residue(residue: str) -> None:
    from fra_core.periods import interpret_period

    assert interpret_period(f"30 June 2025 {residue}").period is None
