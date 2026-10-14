"""Bounded period interpretation and the public observation contracts."""

import builtins
import subprocess
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pytest

from fra_core import periods, schemas
from fra_core.periods import interpret_period, parse_period
from fra_core.schemas import PeriodKind


@pytest.mark.parametrize(
    "marker,restated,audited",
    [
        ("المعدلة", True, None),
        ("المعدّلة", True, None),
        ("المعاد", True, None),
        ("المراجعة", False, True),
        ("المدققة", False, True),
        ("غير المراجعة", False, False),
        ("غير المدققة", False, False),
    ],
)
@pytest.mark.parametrize("dated", ["2024", "٣٠ يونيو ٢٠٢٤"])
def test_arabic_status_articles_preserve_facts_and_legacy_metadata(
    marker: str, restated: bool, audited: bool | None, dated: str
) -> None:
    literal = f"{dated} ({marker})"
    legacy = parse_period(literal)
    facts = interpret_period(literal)
    assert facts.calendar == ("year_only" if dated == "2024" else "complete")
    assert legacy is not None
    assert legacy.restated == restated
    # The compatibility parser historically misses the article in negated status.
    assert legacy.audited == (True if marker.startswith("غير ال") else audited)
    assert facts.period is not None
    assert facts.period.end_date == legacy.end_date
    assert (facts.period.restated, facts.period.audited) == (restated, audited)


@pytest.mark.parametrize(
    "residue",
    [
        "المعدلة30/06",
        "30/06المعدلة",
        "المعدلة7",
        "2024المعدلة",
        "والمعدلة",
        "المعدلةx",
        "المعدلات",
        "غيرالمراجعة",
        "غير المراجعةSAR",
        "SARالمعدلة",
        "المعدلةSAR",
        "المعدلة 7",
        "(المعدلة)30/06",
        "restated30/06",
        "30/06audited",
        "unauditedx",
    ],
)
def test_status_cleanup_keeps_malformed_and_adjacent_residue(residue: str) -> None:
    facts = interpret_period(f"30 June 2025 {residue}")
    assert facts.calendar == "invalid"
    assert facts.period is None


def test_interim_is_uncertain_context_without_changing_legacy_parser() -> None:
    assert interpret_period("Interim").duration == "generic"
    facts = interpret_period("Interim 30 June 2025", default_kind=PeriodKind.DURATION)
    assert facts.duration == "generic" and facts.months is None and facts.period is None
    legacy = parse_period("Interim 30 June 2025", default_kind=PeriodKind.DURATION)
    assert legacy is not None and legacy.months == 12


@pytest.mark.parametrize(
    "literal",
    ["2024المعدلة", "المعدلة2024", "30 June 2024المراجعة", "2024audited", "restated2024"],
)
def test_date_removal_cannot_create_a_status_boundary(literal: str) -> None:
    facts = interpret_period(literal)
    assert facts.calendar == "invalid" and facts.period is None


@pytest.mark.parametrize("cue", ["half year ended ", "SAR "])
def test_repeated_cues_have_a_linear_scan_budget(cue: str, monkeypatch: pytest.MonkeyPatch) -> None:
    text = cue * 400 + "30 June 2025"
    visits = 0

    def bounded_any(values: Iterable[object]) -> bool:
        def counted() -> Iterable[object]:
            nonlocal visits
            for value in values:
                visits += 1
                # Count scan work instead of using a machine-dependent timing threshold.
                assert visits <= 20 * len(text), "period span scans exceed a linear work budget"
                yield value

        return builtins.any(counted())

    monkeypatch.setattr(periods, "any", bounded_any, raising=False)
    facts = interpret_period(text)
    assert facts.calendar == "complete"
    assert facts.end_date == date(2025, 6, 30)
    assert facts.duration == ("explicit" if cue.startswith("half") else "none")
    assert facts.months == (6 if cue.startswith("half") else None)


@pytest.mark.parametrize("cue", ["half year ended ", "SAR ", "ريال"])
def test_large_repeated_cues_finish_within_a_bounded_child_process(cue: str) -> None:
    source = str(Path(periods.__file__).parents[1])
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from fra_core.periods import interpret_period
facts = interpret_period(sys.argv[2] * 20000 + ' 30 June 2025')
assert facts.calendar == 'complete'
assert facts.end_date.isoformat() == '2025-06-30'
assert facts.months == (6 if sys.argv[2].startswith('half') else None)
"""
    # The linear scan budget above avoids timing noise; this separate generous deadline
    # also catches regex backtracking that does not pass through the counted scans.
    subprocess.run([sys.executable, "-c", script, source, cue], check=True, timeout=10)


@pytest.mark.parametrize("cue", ["ريال", "SAR ", "half year ended "])
@pytest.mark.parametrize(
    "duration,expected",
    [
        ("For the two months ended", "invalid"),
        ("not six months ended", "invalid"),
        ("three months and six months ended", "invalid"),
        ("For the 6 months ended", "explicit"),
    ],
)
def test_large_repeated_cues_with_duration_finish_in_a_bounded_child_process(
    cue: str, duration: str, expected: str
) -> None:
    source = str(Path(periods.__file__).parents[1])
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from fra_core.periods import interpret_period
facts = interpret_period(sys.argv[2] * 20000 + ' ' + sys.argv[3] + ' 30 June 2025')
assert facts.calendar == 'complete'
assert facts.end_date.isoformat() == '2025-06-30'
assert facts.duration == sys.argv[4]
assert facts.months == (6 if sys.argv[4] == 'explicit' else None)
assert (facts.period is None) == (sys.argv[4] == 'invalid')
"""
    subprocess.run(
        [sys.executable, "-c", script, source, cue, duration, expected], check=True, timeout=10
    )


def test_invalid_duration_cleanup_bounds_word_numeric_and_whitespace_runs() -> None:
    source = str(Path(periods.__file__).parents[1])
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from fra_core.periods import interpret_period
size = 60000
for prefix, calendar in [
    ('a' * size + ' months', 'complete'),
    ('9' * size + ' months', 'complete'),
    ('0' * size + '6 months', 'complete'),
    ('-' + '9' * size + ' months', 'complete'),
    ('+' + '9' * size + '.5 months', 'complete'),
    ('3.' + '9' * size + ' months', 'complete'),
    ('two' + ' ' * size + 'months', 'complete'),
    ('9' * size + ' ' * size + 'x', 'invalid'),
    ('-' + '9' * size + '.' + '9' * size + 'x', 'invalid'),
    ('a' * size + ' ' * size + 'x', 'invalid'),
]:
    facts = interpret_period(prefix + ' For the two months ended 30 June 2025')
    assert facts.calendar == calendar, (prefix[:20], facts)
    assert facts.duration == 'invalid'
    assert facts.period is None
"""
    # A generous process deadline catches rescans without asserting benchmark timings.
    subprocess.run([sys.executable, "-c", script, source], check=True, timeout=10)


@pytest.mark.parametrize(
    "description,calendar,duration",
    [
        ("For the two months ended", "complete", "invalid"),
        ("For the two monthsended", "invalid", "none"),
        ("For the (two months) ended", "complete", "invalid"),
        ("For the unknown months ended", "complete", "invalid"),
        ("For the x2 months ended", "complete", "invalid"),
        ("For the 2x months ended", "invalid", "invalid"),
        ("For the _two months ended", "invalid", "invalid"),
        ("For the 2.months ended", "invalid", "invalid"),
        ("For the +3.5 months ended", "complete", "invalid"),
        ("For the -3 months ended", "complete", "invalid"),
        ("For the two\tmonths ended", "complete", "invalid"),
        ("For the two\nmonths ended", "complete", "invalid"),
        ("For the twoشهور ended", "invalid", "none"),
        ("For the اثنان أشهر المنتهية في", "complete", "invalid"),
    ],
)
def test_invalid_duration_masking_preserves_boundaries_glue_and_date_residue(
    description: str, calendar: str, duration: str
) -> None:
    facts = interpret_period(f"{description} 30 June 2025")
    assert facts.calendar == calendar
    assert facts.end_date == (date(2025, 6, 30) if calendar == "complete" else None)
    assert facts.duration == duration
    assert facts.period is None


@pytest.mark.parametrize("digit", ["9", "٩", "۹", "𝟡"])
def test_oversized_month_counts_are_invalid_without_aborting(digit: str) -> None:
    facts = interpret_period(f"For the {digit * 5000} months ended 30 June 2025")
    assert facts.calendar == "complete"
    assert facts.end_date == date(2025, 6, 30)
    assert facts.duration == "invalid"
    assert facts.period is None


@pytest.mark.parametrize(
    "count,months", [("0" * 5000 + "3", 3), ("०" * 5000 + "६", 6)], ids=["ascii", "devanagari"]
)
def test_long_leading_zero_counts_retain_unicode_decimal_semantics(count: str, months: int) -> None:
    facts = interpret_period(f"For the {count} months ended 30 June 2025")
    assert facts.duration == "explicit"
    assert facts.months == months
    assert facts.period is not None and facts.period.months == months


@pytest.mark.parametrize("kind", list(PeriodKind))
@pytest.mark.parametrize("cue", ["For the period ended", "عن الفترة المنتهية في"])
def test_generic_duration_does_not_supply_an_annual_length(kind: PeriodKind, cue: str) -> None:
    text = f"{cue} 30 June 2025"
    facts = interpret_period(text, default_kind=kind)
    assert facts.calendar == "complete"
    assert facts.duration == "generic"
    assert facts.months is None
    assert facts.period is None
    legacy = parse_period(text, default_kind=kind)
    assert legacy is not None and legacy.months == 12


def test_period_evidence_contracts_are_public_schema_exports() -> None:
    from fra_core.schemas import (
        PeriodCandidate,
        PeriodConflict,
        PeriodEvidence,
        PeriodObservation,
        statement,
    )

    for contract in (PeriodObservation, PeriodEvidence, PeriodCandidate, PeriodConflict):
        assert getattr(schemas, contract.__name__) is getattr(statement, contract.__name__)
        assert contract.__name__ in schemas.__all__


@pytest.mark.parametrize(
    "words,months",
    [("الستة أشهر", 6), ("الثلاثة أشهر", 3), ("التسعة أشهر", 9)],
)
def test_arabic_article_before_a_duration_cue_keeps_the_length(words: str, months: int) -> None:
    facts = interpret_period(
        f"عن فترة {words} المنتهية في 30 يونيو 2025", default_kind=PeriodKind.DURATION
    )
    assert facts.duration == "explicit"
    assert facts.months == months
    assert facts.calendar == "complete"
    assert facts.period is not None
    assert facts.period.key == f"{months}M-2025-06-30"


def test_arabic_article_does_not_join_two_duration_lengths() -> None:
    facts = interpret_period(
        "عن الستة أشهر والثلاثة أشهر المنتهية في 30 يونيو 2025", default_kind=PeriodKind.DURATION
    )
    assert facts.duration == "invalid"


@pytest.mark.parametrize(
    "text,months",
    [
        ("ستة أشهر المنتهية في 30 يونيو 2025", 6),
        ("السنة المنتهية في 31 ديسمبر 2025", 12),
    ],
)
def test_arabic_duration_cues_without_article_are_unchanged(text: str, months: int) -> None:
    facts = interpret_period(text, default_kind=PeriodKind.DURATION)
    assert facts.duration == "explicit"
    assert facts.months == months


def test_arabic_duration_cue_keeps_its_token_boundary_after_the_article() -> None:
    facts = interpret_period(
        "بالستة أشهر المنتهية في 30 يونيو 2025", default_kind=PeriodKind.DURATION
    )
    assert not (facts.duration == "explicit" and facts.months == 6)


@pytest.mark.parametrize("text", ["فترة 30 يونيو 2025", "عن فترة 30 يونيو 2025", "الفترة 2025"])
def test_bare_period_noun_does_not_change_the_compatibility_parser(text: str) -> None:
    # The noun is generic wording for strict facts; a balance sheet header that carries
    # it is still an instant for the permissive parser.
    period = parse_period(text, default_kind=PeriodKind.INSTANT)
    assert period is not None and period.kind is PeriodKind.INSTANT
    assert interpret_period(text, default_kind=PeriodKind.INSTANT).duration in {"generic", "none"}


@pytest.mark.parametrize("text", ["2025 - SAR '000", "2025 – SAR", "2025 — SAR"])
def test_dash_before_a_unit_marker_is_a_separator(text: str) -> None:
    facts = interpret_period(text)
    assert facts.calendar == "year_only"
    assert facts.period is not None and facts.period.key == "2025-12-31"


def test_dash_before_a_unit_marker_keeps_a_complete_date() -> None:
    facts = interpret_period("31 December 2025 - SAR '000")
    assert facts.calendar == "complete"
    assert facts.period is not None and facts.period.end_date == date(2025, 12, 31)


@pytest.mark.parametrize("text", ["2025 -", "2025 - 2024 SAR"])
def test_dash_without_a_unit_marker_or_before_a_year_stays_invalid(text: str) -> None:
    assert interpret_period(text).calendar == "invalid"


def test_dash_inside_a_numeric_date_is_unchanged() -> None:
    assert interpret_period("31-12-2025 SAR").calendar == "complete"


def test_dash_glued_to_another_token_is_unchanged() -> None:
    # Recorded before the separator rule: the rule covers a standalone dash token only.
    assert interpret_period("2025 -SAR").calendar == "invalid"
    assert interpret_period("2025- SAR").calendar == "invalid"


@pytest.mark.parametrize(
    "text",
    ["Dec. 31, 2025", "Dec 31, 2025", "December 31, 2025"],
)
def test_month_first_date_accepts_a_full_stop_after_the_month(text: str) -> None:
    facts = interpret_period(text)
    assert facts.calendar == "complete"
    assert facts.period is not None and facts.period.end_date == date(2025, 12, 31)


def test_month_first_date_with_dotted_september() -> None:
    facts = interpret_period("Sept. 30, 2025")
    assert facts.calendar == "complete"
    assert facts.period is not None and facts.period.end_date == date(2025, 9, 30)


def test_dotted_month_year_is_unchanged() -> None:
    assert interpret_period("Dec. 2025").calendar == "month_year"


@pytest.mark.parametrize("text", ["Dec.. 31, 2025", "Dec. 32, 2025"])
def test_dotted_month_first_date_rejects_malformed_forms(text: str) -> None:
    assert interpret_period(text).calendar == "invalid"


def test_dotted_month_first_date_resolves_a_fiscal_year() -> None:
    facts = interpret_period("For the year ended Dec. 31, 2025", default_kind=PeriodKind.DURATION)
    assert facts.period is not None and facts.period.key == "FY2025"


@pytest.mark.parametrize(
    "text,key",
    [
        ("Dec. 31, 2025", "2025-12-31"),
        ("Dec 31, 2025", "2025-12-31"),
        ("31 Dec. 2025", "2025-12-31"),
        ("Dec. 2025", "2025-12-31"),
        ("Sept. 30, 2025", "2025-09-30"),
        ("2025", "2025-12-31"),
    ],
)
def test_parse_period_is_unchanged_by_the_dotted_month_rule(text: str, key: str) -> None:
    # Values recorded before the change to the strict date pattern.
    period = parse_period(text)
    assert period is not None and period.key == key


def test_audited_and_unaudited_together_yield_no_period() -> None:
    facts = interpret_period("31 December 2025 (Audited and Unaudited)")
    assert facts.calendar == "complete"
    assert facts.period is None


def test_arabic_audited_and_unaudited_together_yield_no_period() -> None:
    facts = interpret_period("31 ديسمبر 2025 مراجعة غير مراجعة")
    assert facts.calendar == "complete"
    assert facts.period is None


@pytest.mark.parametrize(
    "text,audited",
    [
        ("31 December 2025 (Unaudited)", False),
        ("31 December 2025 (Audited)", True),
        ("31 ديسمبر 2025 غير مراجعة", False),
        ("31 December 2025 (Unaudited) (Unaudited)", False),
    ],
)
def test_a_single_audit_status_is_not_a_contradiction(text: str, audited: bool) -> None:
    period = interpret_period(text).period
    assert period is not None and period.audited is audited


def test_per_share_qualifier_after_a_unit_marker_leaves_the_date_complete() -> None:
    facts = interpret_period("31 December 2025 (in millions, except per share data)")
    assert facts.calendar == "complete" and facts.end_date == date(2025, 12, 31)
    assert facts.period is not None


def test_per_share_wording_without_a_unit_marker_stays_invalid() -> None:
    assert interpret_period("31 December 2025 per share data").calendar == "invalid"


@pytest.mark.parametrize(
    "text",
    [
        "ليس الثلاثة أشهر المنتهية في 30 يونيو 2025",
        "غير الستة أشهر المنتهية في 30 يونيو 2025",
        "ليس ثلاثة أشهر المنتهية في 30 يونيو 2025",
    ],
)
def test_negated_arabic_duration_stays_invalid_with_the_article(text: str) -> None:
    facts = interpret_period(text, default_kind=PeriodKind.DURATION)
    assert facts.duration == "invalid"
    assert facts.period is None


@pytest.mark.parametrize("text", ["31 Dec 2025", "31 Dec. 2025", "31 December, 2025"])
def test_day_first_dates_with_one_separator_form_are_complete(text: str) -> None:
    facts = interpret_period(text)
    assert facts.calendar == "complete"
    assert facts.end_date == date(2025, 12, 31)
    assert facts.date_text == text


@pytest.mark.parametrize("text", ["31 Dec2025", "31 Dec.. 2025", "31 December,,2025"])
def test_day_first_dates_with_a_malformed_separator_are_not_complete(text: str) -> None:
    facts = interpret_period(text)
    assert facts.calendar != "complete"


@pytest.mark.parametrize(
    "text,key,months",
    [
        ("31 Dec2025", "2025-12-31", None),
        ("31 Dec.. 2025", "2025-12-31", None),
        ("31 December,,2025", "2025-12-31", None),
        ("31 Dec 2025", "2025-12-31", None),
        ("31 Dec. 2025", "2025-12-31", None),
        ("31 December, 2025", "2025-12-31", None),
        ("30 June 2024", "2024-06-30", None),
        ("1 March 2023", "2023-03-01", None),
        ("December 31, 2025", "2025-12-31", None),
    ],
)
def test_the_permissive_parser_keeps_reading_day_first_separators(
    text: str, key: str, months: int | None
) -> None:
    period = parse_period(text)
    assert period is not None
    assert (period.key, period.end_date.isoformat(), period.months) == (key, key, months)
    assert period.kind is PeriodKind.INSTANT


def test_a_lone_period_before_the_year_stays_a_complete_date() -> None:
    facts = interpret_period("31 Dec.2025")
    assert facts.calendar == "complete" and facts.end_date == date(2025, 12, 31)


@pytest.mark.parametrize(
    "text", ["31 Dec., 2025", "31 Dec,2025", "31 ديسمبر,2025", "31 Dec , 2025", "31 December, 2025"]
)
def test_one_dot_and_one_comma_between_month_and_year_are_a_complete_date(text: str) -> None:
    assert interpret_period(text).calendar == "complete"


@pytest.mark.parametrize("text", ["31 Dec 2025 يتألف", "31 Dec 2025 تألف"])
def test_a_word_that_ends_in_a_scale_spelling_is_not_unit_text(text: str) -> None:
    assert interpret_period(text).calendar == "invalid"
