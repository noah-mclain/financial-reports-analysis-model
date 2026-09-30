"""Check results shared by structure and analytics."""

from decimal import Decimal

from fra_core.schemas import CheckResult


def test_a_check_result_round_trips_with_exact_decimals() -> None:
    check = CheckResult(
        id="s1:balance_identity:2025-12-31",
        statement_id="s1",
        kind="balance_identity",
        period_key="2025-12-31",
        status="fail",
        expected=Decimal("100.5"),
        actual=Decimal("99.5"),
        difference=Decimal("-1.0"),
        tolerance=Decimal("0.5"),
        line_item_ids=["a", "b"],
        detail="total assets against total liabilities and equity",
    )
    assert CheckResult.model_validate_json(check.model_dump_json()) == check
