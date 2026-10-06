"""Hand-computed margins, ambiguity and numerical boundaries."""

import json
from dataclasses import replace
from datetime import date
from decimal import ROUND_UP, Clamped, Decimal, Inexact, Rounded, Underflow, localcontext

import pytest
from statement_builders import POLICY

from fra_analytics.metrics.profitability import compute_margins
from fra_core.schemas.caveat import Caveat
from fra_core.schemas.metric import MetricUnit, MetricValue
from fra_core.schemas.statement import (
    BBox,
    Cell,
    LineItem,
    MappingSource,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)


@pytest.fixture
def income() -> Statement:
    period = Period(key="FY2025", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
    rows = []
    for index, (canonical, amount) in enumerate(
        (
            ("revenue", "200"),
            ("gross_profit", "60"),
            ("operating_income", "30"),
            ("net_income", "-10"),
        )
    ):
        rows.append(
            LineItem(
                id=f"source_{index}",
                raw_label=canonical,
                canonical_id=canonical,
                cells=[
                    Cell(
                        period_key=period.key,
                        reported=Decimal(amount),
                        raw_text=amount,
                        provenance=Provenance(
                            page_no=2,
                            bbox=BBox(left=1, top=1, right=2, bottom=2),
                            table_ref="#/tables/1",
                            row=index,
                            col=1,
                        ),
                    )
                ],
            )
        )
    return Statement(
        id="income_1",
        document_sha256="a" * 64,
        type=StatementType.INCOME,
        currency="EGP",
        scale=1,
        periods=[period],
        line_items=rows,
    )


def rows_of(result: MetricValue) -> dict[str, list[str]]:
    """The line item ids behind each role of a result."""
    return {role: [cell.line_item_id for cell in cells] for role, cells in result.inputs.items()}


def test_hand_computed_and_provenance(income: Statement) -> None:
    before = income.model_dump_json()
    results = compute_margins(income, policy=POLICY)
    assert [r.metric_id for r in results] == ["gross_margin", "operating_margin", "net_margin"]
    assert [r.value for r in results] == [0.3, 0.15, -0.05]
    assert [r.flags for r in results] == [[], [], ["sign_unexpected"]]
    assert all(
        r.period_key == "FY2025" and r.unit == MetricUnit.RATIO and r.formula_version == "1"
        for r in results
    )
    assert [rows_of(r) for r in results] == [
        {"numerator": ["source_1"], "denominator": ["source_0"]},
        {"numerator": ["source_2"], "denominator": ["source_0"]},
        {"numerator": ["source_3"], "denominator": ["source_0"]},
    ]
    # Every input is the printed cell, with its provenance, not only the id of its row.
    first = results[0].inputs["numerator"][0]
    assert (first.statement_id, first.canonical_id, first.period_key) == (
        "income_1",
        "gross_profit",
        "FY2025",
    )
    assert (first.reported, first.scale, first.currency) == (Decimal("60"), 1, "EGP")
    assert (first.provenance.page_no, first.provenance.row) == (2, 1)
    assert income.model_dump_json() == before
    for result in results:
        assert MetricValue.model_validate_json(result.model_dump_json()) == result
        json.dumps(result.model_dump(mode="json"), allow_nan=False)
    income.line_items.reverse()
    assert compute_margins(income, policy=POLICY) == results


@pytest.mark.parametrize("amount", ["0", "-0"])
def test_zero_revenue(income: Statement, amount: str) -> None:
    income.line_items[0].cells[0].reported = Decimal(amount)
    results = compute_margins(income, policy=POLICY)
    assert all(r.value is None and "undefined_zero_denominator" in r.flags for r in results)


@pytest.mark.parametrize("amount", ["0", "-0"])
@pytest.mark.parametrize(
    "index,canonical", [(1, "gross_profit"), (2, "operating_income"), (3, "net_income")]
)
@pytest.mark.parametrize("missing", ["row", "unmapped", "cell", "null"])
def test_missing_profit_with_zero_revenue(
    income: Statement, amount: str, index: int, canonical: str, missing: str
) -> None:
    income.line_items[0].cells[0].reported = Decimal(amount)
    row = income.line_items[index]
    if missing == "row":
        income.line_items.remove(row)
    elif missing == "unmapped":
        row.canonical_id = None
    elif missing == "cell":
        row.cells.clear()
    else:
        row.cells[0].reported = None
    before = income.model_dump_json()
    result = compute_margins(income, policy=POLICY)[index - 1]
    assert result.value is None
    assert result.flags == [f"missing_input:{canonical}", "undefined_zero_denominator"]
    # A row with no printed figure gives no input cell, however it came to have none.
    assert rows_of(result) == {"numerator": [], "denominator": ["source_0"]}
    assert income.model_dump_json() == before


@pytest.mark.parametrize("ambiguous", ["row", "cell"])
def test_ambiguous_zero_revenue(income: Statement, ambiguous: str) -> None:
    revenue = income.line_items[0]
    revenue.cells[0].reported = Decimal("0")
    if ambiguous == "row":
        duplicate = revenue.model_copy(deep=True)
        duplicate.id = "candidate"
        income.line_items.append(duplicate)
        flag = "duplicate_input:revenue"
    else:
        revenue.cells.append(revenue.cells[0].model_copy(deep=True))
        flag = "duplicate_cell:source_0:FY2025"
    results = compute_margins(income, policy=POLICY)
    assert all(r.value is None and r.flags == [flag] for r in results[:2])
    assert all("undefined_zero_denominator" not in r.flags for r in results)


@pytest.mark.parametrize(
    "option,expected,flag",
    [
        ("compute_and_flag", -0.3, "negative_base"),
        ("null", None, "undefined_negative_denominator"),
    ],
)
def test_negative_revenue(
    income: Statement, option: str, expected: float | None, flag: str
) -> None:
    income.line_items[0].cells[0].reported = Decimal("-200")
    policy = replace(POLICY, negative_margin_denominator=option)  # type: ignore[arg-type]
    result = compute_margins(income, policy=policy)[0]
    assert result.value == expected
    assert result.flags == ["sign_unexpected", flag]


def test_zero_numerator(income: Statement) -> None:
    income.line_items[1].cells[0].reported = Decimal("0")
    assert compute_margins(income, policy=POLICY)[0].value == 0.0


def test_signed_losses_and_double_negative(income: Statement) -> None:
    income.line_items[1].cells[0].reported = Decimal("-60")
    income.line_items[2].cells[0].reported = Decimal("-30")
    assert [r.value for r in compute_margins(income, policy=POLICY)] == [-0.3, -0.15, -0.05]
    income.line_items[0].cells[0].reported = Decimal("-200")
    results = compute_margins(income, policy=POLICY)
    assert [r.value for r in results] == [0.3, 0.15, 0.05]
    assert all(r.flags == ["sign_unexpected", "negative_base"] for r in results)


def test_supplied_mapping_is_authoritative(income: Statement) -> None:
    for row in income.line_items:
        row.mapping_confidence = 0.0
        row.mapping_source = MappingSource.MODEL
        row.raw_label = "unrecognised label"
    assert [r.value for r in compute_margins(income, policy=POLICY)] == [0.3, 0.15, -0.05]


@pytest.mark.parametrize(
    "index,canonical",
    [(0, "revenue"), (1, "gross_profit"), (2, "operating_income"), (3, "net_income")],
)
@pytest.mark.parametrize("missing", ["row", "unmapped", "cell", "null"])
def test_missing_input(income: Statement, index: int, canonical: str, missing: str) -> None:
    row = income.line_items[index]
    if missing == "row":
        income.line_items.remove(row)
    elif missing == "unmapped":
        row.canonical_id = None
    elif missing == "cell":
        row.cells.clear()
    else:
        row.cells[0].reported = None
    results = compute_margins(income, policy=POLICY)
    affected = results if index == 0 else [results[index - 1]]
    assert all(r.value is None and f"missing_input:{canonical}" in r.flags for r in affected)
    assert all("undefined_zero_denominator" not in r.flags for r in affected)
    role = "denominator" if index == 0 else "numerator"
    assert all(rows_of(r)[role] == [] for r in affected)


@pytest.mark.parametrize("index", [0, 1])
def test_duplicate_mapping(income: Statement, index: int) -> None:
    duplicate = income.line_items[index].model_copy(deep=True)
    duplicate.id = "candidate"
    duplicate.cells[0].flags = ["uncertain_candidate"]
    income.line_items.append(duplicate)
    results = compute_margins(income, policy=POLICY)
    affected = results if index == 0 else [results[0]]
    role = "denominator" if index == 0 else "numerator"
    assert all(
        r.value is None
        and f"duplicate_input:{duplicate.canonical_id}" in r.flags
        and "uncertain_candidate" in r.flags
        for r in affected
    )
    assert all(rows_of(r)[role] == [income.line_items[index].id, "candidate"] for r in affected)
    if index == 1:
        assert results[1].value == 0.15


@pytest.mark.parametrize("index", [0, 1])
def test_duplicate_cell(income: Statement, index: int) -> None:
    row = income.line_items[index]
    row.cells.append(row.cells[0].model_copy(update={"flags": ["second_read"]}))
    results = compute_margins(income, policy=POLICY)
    affected = results if index == 0 else [results[0]]
    assert all(
        r.value is None
        and f"duplicate_cell:{row.id}:FY2025" in r.flags
        and "second_read" in r.flags
        for r in affected
    )


def test_flags_only_relevant_cells(income: Statement) -> None:
    income.flags = ["scale_missing", "needs_review", "needs_review"]
    income.line_items[1].cells[0].flags = ["needs_review", "digit_suspect"]
    income.line_items[0].cells[0].flags = ["digit_suspect", "ocr"]
    income.line_items[2].cells[0].flags = ["other_profit"]
    results = compute_margins(income, policy=POLICY)
    assert results[0].flags == ["scale_missing", "needs_review", "digit_suspect", "ocr"]
    assert "other_profit" not in results[0].flags


def test_no_parent_profit_substitution(income: Statement) -> None:
    income.line_items[3].canonical_id = "net_income_attributable_parent"
    result = compute_margins(income, policy=POLICY)[2]
    assert result.value is None and result.inputs["numerator"] == []
    assert result.flags == ["missing_input:net_income"]


def test_periods_no_annualization(income: Statement) -> None:
    income.periods = [
        Period(
            key=key,
            end_date=date(2025, 12, 31),
            kind=PeriodKind.DURATION,
            months=months,
            restated=restated,
        )
        for key, months, restated in (
            ("annual", 12, False),
            ("quarter", 3, False),
            ("half", 6, False),
            ("nine", 9, False),
            ("restated", 12, True),
        )
    ]
    for row in income.line_items:
        original = row.cells[0]
        row.cells = [
            original.model_copy(update={"period_key": p.key}, deep=True) for p in income.periods
        ]
    income.line_items[1].cells[-1].reported = Decimal("80")
    income.line_items[1].cells[-1].flags = ["restated_read"]
    results = compute_margins(income, policy=POLICY)
    assert [(r.period_key, r.value) for r in results] == [
        ("annual", 0.3),
        ("annual", 0.15),
        ("annual", -0.05),
        ("quarter", 0.3),
        ("quarter", 0.15),
        ("quarter", -0.05),
        ("half", 0.3),
        ("half", 0.15),
        ("half", -0.05),
        ("nine", 0.3),
        ("nine", 0.15),
        ("nine", -0.05),
        ("restated", 0.4),
        ("restated", 0.15),
        ("restated", -0.05),
    ]
    assert "restated_read" not in results[0].flags
    assert "restated_read" in results[-3].flags


@pytest.mark.parametrize(
    "scale,currency", [(1, "EGP"), (1000, "SAR"), (1000000, "USD"), (1000000000, "EUR")]
)
def test_scale_currency_caveat_invariance(income: Statement, scale: int, currency: str) -> None:
    income.scale = scale
    income.currency = currency
    income.caveats = [Caveat(id="scale_assumed_units", scope="currency_amounts", evidence={})]
    for row in income.line_items:
        amount = row.cells[0].reported
        assert amount is not None
        row.cells[0].reported = amount * 1000
    results = compute_margins(income, policy=POLICY)
    assert [r.value for r in results] == [0.3, 0.15, -0.05]
    assert all(r.model_dump().get("caveats", []) == [] for r in results)


@pytest.mark.parametrize(
    "kind",
    [
        StatementType.BALANCE,
        StatementType.CASH_FLOW,
        StatementType.COMPREHENSIVE_INCOME,
        StatementType.EQUITY,
    ],
)
def test_wrong_statement_kind(income: Statement, kind: StatementType) -> None:
    income.type = kind
    with pytest.raises(ValueError, match="income"):
        compute_margins(income, policy=POLICY)


def test_wrong_runtime_type() -> None:
    with pytest.raises(ValueError, match="Statement"):
        compute_margins(None, policy=POLICY)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid", ["instant", "duplicate_ids", "unknown_period", "duplicate_periods", "no_periods"]
)
def test_malformed_statement(income: Statement, invalid: str) -> None:
    if invalid == "instant":
        income.periods = [
            Period(key="FY2025", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
        ]
    elif invalid == "duplicate_ids":
        income.line_items[1].id = income.line_items[0].id
    elif invalid == "unknown_period":
        income.line_items[0].cells[0].period_key = "unknown"
    elif invalid == "duplicate_periods":
        income.periods.append(income.periods[0])
    else:
        income.periods.clear()
    with pytest.raises(ValueError, match=r"period|row id"):
        compute_margins(income, policy=POLICY)


def test_context_independence(income: Statement) -> None:
    income.line_items[0].cells[0].reported = Decimal("3")
    income.line_items[1].cells[0].reported = Decimal("1")
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.rounding = ROUND_UP
        ambient.Emax = 1
        ambient.Emin = -1
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        ambient.traps[Clamped] = True
        ambient.traps[Underflow] = True
        before = ambient.copy()
        result = compute_margins(income, policy=POLICY)[0]
        assert result.value == 0.3333333333333333
        assert (
            ambient.prec == before.prec
            and ambient.flags == before.flags
            and ambient.traps == before.traps
        )


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("index", [0, 1])
def test_nonfinite_input(income: Statement, value: str, index: int) -> None:
    income.line_items[index].cells[0].reported = Decimal(value)
    with pytest.raises(ValueError, match=r"nonfinite.*source_"):
        compute_margins(income, policy=POLICY)


@pytest.mark.parametrize(
    "numerator,denominator,expected,flag",
    [
        ("1e1000", "1e1000", 1.0, None),
        ("1e1000", "1", None, "unrepresentable_result"),
        ("1e-1000", "1", None, "unrepresentable_result"),
        ("1e999999", "1e-999999", None, "arithmetic_result"),
        ("1e-999999", "1e999999", None, "arithmetic_result"),
        ("5e-324", "1", 5e-324, None),
    ],
)
def test_extreme_results(
    income: Statement, numerator: str, denominator: str, expected: float | None, flag: str | None
) -> None:
    income.line_items[1].cells[0].reported = Decimal(numerator)
    income.line_items[0].cells[0].reported = Decimal(denominator)
    result = compute_margins(income, policy=POLICY)[0]
    assert result.value == expected
    if flag is not None:
        assert flag in result.flags
    json.dumps(result.model_dump(mode="json"), allow_nan=False)
