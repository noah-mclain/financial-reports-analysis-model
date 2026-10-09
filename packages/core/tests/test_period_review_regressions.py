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
