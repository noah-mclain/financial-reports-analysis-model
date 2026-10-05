"""Identity checks over mapped canonical items, with the D6 tolerance of n x 0.5 reported units."""

from decimal import Decimal

import pytest
from statement_builders import (
    BALANCE_ROWS,
    INCOME_ROWS,
    acme,
    annual,
    build,
    closing,
    replace_row,
    row,
)

from fra_analytics import identities
from fra_analytics.frame import to_frame
from fra_analytics.identities import Tie, check_identities
from fra_core.schemas import CheckResult, StatementType
from fra_core.taxonomy.loader import load_taxonomy


def balance(*figures: tuple[str, str]) -> list[CheckResult]:
    """Check a one-period balance sheet made of ``(canonical id, figure)`` pairs."""
    statement = build(
        "b",
        StatementType.BALANCE,
        [closing(2025)],
        [row(item, {"2025-12-31": figure}) for item, figure in figures],
    )
    return check_identities(to_frame([statement]))


def only(results: list[CheckResult], kind: str) -> CheckResult:
    [one] = [r for r in results if r.kind == kind]
    return one


def test_acme_passes_every_identity_and_tie_in_both_years() -> None:
    results = check_identities(to_frame(acme()))
    assert {r.status for r in results} == {"pass"}
    identities = [r for r in results if r.kind == "balance_identity"]
    assert [(r.period_key, r.expected, r.actual) for r in identities] == [
        ("2025-12-31", Decimal("2000"), Decimal("2000")),
        ("2024-12-31", Decimal("1600"), Decimal("1600")),
    ]
    # 2 periods x (identity + 3 balance ties) + 2 periods x (gross profit and net income ties)
    assert len(results) == 2 * 4 + 2 * 2


def test_a_result_names_the_line_items_it_used_and_the_total_first() -> None:
    results = check_identities(to_frame(acme()))
    identity = next(
        r for r in results if r.kind == "balance_identity" and r.period_key == "2025-12-31"
    )
    # acme-balance rows are numbered from 1 in BALANCE_ROWS order: total_assets is the 7th,
    # total_liabilities the 16th and total_equity the 19th.
    assert identity.line_item_ids == ["acme-balance-r7", "acme-balance-r16", "acme-balance-r19"]
    assert identity.statement_id == "acme-balance"
    assert identity.id == "acme-balance:balance_identity:balance:2025-12-31"


def test_the_identity_holds_one_unit_off_and_fails_two_units_off() -> None:
    # Two addends: tolerance 2 x 0.5 = 1.0 reported units.
    base = (("total_liabilities", "1000"), ("total_equity", "1000"))
    inside = only(balance(("total_assets", "2001"), *base), "balance_identity")
    assert (inside.status, inside.difference, inside.tolerance) == (
        "pass",
        Decimal("1"),
        Decimal("1.0"),
    )
    outside = only(balance(("total_assets", "2002"), *base), "balance_identity")
    assert (outside.status, outside.difference) == ("fail", Decimal("2"))
    below = only(balance(("total_assets", "1999"), *base), "balance_identity")
    assert below.status == "pass"


def test_with_a_printed_total_of_liabilities_and_equity_there_is_one_addend() -> None:
    # One addend: tolerance 0.5.
    printed = ("total_liabilities_and_equity", "2000")
    inside = only(balance(("total_assets", "2000.5"), printed), "balance_identity")
    assert (inside.status, inside.tolerance, inside.expected) == (
        "pass",
        Decimal("0.5"),
        Decimal("2000"),
    )
    outside = only(balance(("total_assets", "2001"), printed), "balance_identity")
    assert outside.status == "fail"


def test_the_subtotal_tie_has_its_own_tolerance_at_the_boundary() -> None:
    # total_equity = parent + nci: two addends, tolerance 1.0.
    parts = (("equity_attributable_parent", "900"), ("non_controlling_interests", "100"))
    ok = only(balance(("total_equity", "1001"), *parts), "subtotal")
    assert ok.status == "pass"
    assert ok.detail == "total_equity = equity_attributable_parent + non_controlling_interests"
    bad = only(balance(("total_equity", "1002"), *parts), "subtotal")
    assert (bad.status, bad.difference, bad.tolerance) == ("fail", Decimal("2"), Decimal("1.0"))


def test_a_missing_part_skips_the_tie_and_names_it() -> None:
    skipped = only(
        balance(("total_equity", "1000"), ("equity_attributable_parent", "900")), "subtotal"
    )
    assert skipped.status == "skipped"
    assert skipped.detail == "missing_input:non_controlling_interests"
    assert skipped.expected is None


def test_a_missing_total_skips_the_balance_identity_and_names_it() -> None:
    skipped = only(
        balance(("total_liabilities", "1000"), ("total_equity", "1000")), "balance_identity"
    )
    assert skipped.status == "skipped"
    assert skipped.detail == "missing_input:total_assets"


def test_a_tie_whose_total_is_not_printed_is_not_reported() -> None:
    results = balance(("equity_attributable_parent", "900"), ("non_controlling_interests", "100"))
    assert [r.kind for r in results if r.kind == "subtotal"] == []
    assert only(results, "balance_identity").status == "skipped"


def test_two_rows_on_one_item_skip_the_check_as_ambiguous() -> None:
    results = balance(
        ("total_assets", "2000"),
        ("total_assets", "2000"),
        ("total_liabilities", "1000"),
        ("total_equity", "1000"),
    )
    assert only(results, "balance_identity").detail == "ambiguous_input:total_assets"
    assert only(results, "balance_identity").status == "skipped"


def test_an_expense_is_taken_as_a_magnitude_in_the_gross_profit_tie() -> None:
    # 1000 - |-600| = 400, whether cost is printed in brackets or not.
    for cost in ("-600", "600"):
        statement = build(
            "i",
            StatementType.INCOME,
            [annual(2025)],
            [
                row("revenue", {"FY2025": "1000"}),
                row("cost_of_revenue", {"FY2025": cost}),
                row("gross_profit", {"FY2025": "400"}),
            ],
        )
        [tie] = check_identities(to_frame([statement]))
        assert (tie.status, tie.expected, tie.actual) == ("pass", Decimal("400"), Decimal("400"))
        assert tie.detail == "gross_profit = revenue - cost_of_revenue"


def test_a_wrong_net_income_split_fails() -> None:
    rows = replace_row(
        INCOME_ROWS,
        "net_income_attributable_nci",
        row("net_income_attributable_nci", {"FY2025": 30, "FY2024": 10}),
    )
    statement = build("i", StatementType.INCOME, [annual(2025), annual(2024)], rows)
    results = {
        r.period_key: r for r in check_identities(to_frame([statement])) if "net_income" in r.id
    }
    assert results["FY2025"].status == "fail"  # 100 + 30 against 120
    assert results["FY2025"].difference == Decimal("-10")
    assert results["FY2024"].status == "pass"


def test_a_balance_sheet_with_a_dropped_total_still_reports_the_other_period() -> None:
    rows = replace_row(BALANCE_ROWS, "total_assets", row("total_assets", {"2025-12-31": 2000}))
    statement = build("b", StatementType.BALANCE, [closing(2025), closing(2024)], rows)
    results = [r for r in check_identities(to_frame([statement])) if r.kind == "balance_identity"]
    assert {r.period_key: r.status for r in results} == {
        "2025-12-31": "pass",
        "2024-12-31": "skipped",
    }


def test_a_tie_that_names_an_item_outside_the_taxonomy_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = Tie(StatementType.BALANCE, "total_assets", ("total_current_assets", "not_an_item"))
    monkeypatch.setattr(identities, "TIES", (bad,))
    with pytest.raises(ValueError, match="not_an_item"):
        check_identities(to_frame(acme()))


def test_every_tie_item_is_a_canonical_item() -> None:
    for tie in identities.TIES:
        for item in (tie.total, *tie.parts, *tie.subtractions):
            assert load_taxonomy().by_id(item) is not None, f"{tie.text}: {item}"
