"""Passed, or held for review with the reasons (spec 12, Data flow step 10a)."""

from datetime import date
from decimal import Decimal

from fra_core.schemas import (
    BBox,
    Cell,
    CheckResult,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.review import review_statement

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, value: str | None, *flags: str) -> LineItem:
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value) if value is not None else None,
        raw_text=value or "",
        provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
        flags=list(flags),
    )
    return LineItem(id=f"r{n}", raw_label=f"row {n}", cells=[cell])


def statement(
    items: list[LineItem], kind: StatementType = StatementType.INCOME, flags: tuple[str, ...] = ()
) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1000,
        periods=[P],
        line_items=items,
        flags=list(flags),
    )


def check(kind: str, status: str, ids: list[str], detail: str = "") -> CheckResult:
    return CheckResult.model_validate(
        {
            "id": f"s:{kind}:{ids[-1] if ids else 'none'}:{P.key}",
            "statement_id": "s",
            "kind": kind,
            "period_key": P.key,
            "status": status,
            "line_item_ids": ids,
            "detail": detail,
        }
    )


ROWS = [item(1, "10"), item(2, "5"), item(3, "15"), item(4, "7")]
PASSING = [check("subtotal", "pass", ["r1", "r2", "r3"])]


def test_a_clean_statement_passes_and_counts_what_the_checks_vouch_for() -> None:
    review = review_statement(statement(ROWS), PASSING, primary=True)
    assert review.status == "passed" and review.reasons == []
    assert (review.numeric_cells, review.checked_cells, review.flagged_cells) == (4, 3, 0)


def test_each_failed_check_is_a_reason() -> None:
    checks = [
        *PASSING,
        check("subtotal", "fail", ["r1", "r4"]),
        check("net_profit_tie", "fail", ["r4"]),
    ]
    review = review_statement(statement(ROWS), checks, primary=True)
    assert review.status == "needs_review"
    assert review.reasons == ["subtotal_failed", "tie_failed"]


def test_a_balance_sheet_needs_its_identity_in_every_period() -> None:
    balance = statement(ROWS, StatementType.BALANCE)
    assert review_statement(balance, PASSING, primary=True).reasons == ["identity_not_checked"]
    skipped = [*PASSING, check("balance_identity", "skipped", [], "identity_totals_not_found")]
    assert review_statement(balance, skipped, primary=True).reasons == [
        "identity_not_checked:identity_totals_not_found"
    ]
    held = [*PASSING, check("balance_identity", "fail", ["r3", "r4"])]
    assert review_statement(balance, held, primary=True).reasons == ["identity_failed"]
    ok = [*PASSING, check("balance_identity", "pass", ["r3", "r4"])]
    assert review_statement(balance, ok, primary=True).status == "passed"


def test_a_statement_no_check_vouches_for_is_held() -> None:
    assert review_statement(statement(ROWS), [], primary=True).reasons == ["unchecked"]


def test_critical_cell_flags_hold_with_their_count() -> None:
    rows = [
        item(1, "10", "digit_suspect"),
        item(2, None, "numbers_missing"),
        item(3, None, "unparsed"),
        item(4, "7", "row_realigned"),
    ]
    review = review_statement(statement(rows), PASSING, primary=True)
    assert review.reasons == [
        "numbers_missing:1",
        "unparsed:1",
        "digit_suspect:1",
        "row_realigned:1",
    ]
    assert review.flagged_cells == 4


def test_a_confirmed_blank_holds_nothing() -> None:
    rows = [item(1, "10"), item(2, None, "numbers_missing", "blank_confirmed"), item(3, "10")]
    review = review_statement(statement(rows), PASSING, primary=True)
    assert review.status == "passed" and review.warnings == ["blank_confirmed:1"]


def test_statement_flags_hold_or_warn() -> None:
    held = review_statement(
        statement(ROWS, flags=("period_unbound:2", "scale_missing")), PASSING, primary=True
    )
    assert held.reasons == ["period_unbound:2"] and held.warnings == ["scale_missing"]
    merged = [item(1, "10", "label_merged"), *ROWS[1:]]
    assert review_statement(statement(merged), PASSING, primary=True).warnings == ["label_merged:1"]


def test_a_word_order_repair_warns_and_holds_nothing() -> None:
    review = review_statement(
        statement(ROWS, flags=("words_reversed", "word_order_uncertain")), PASSING, primary=True
    )
    assert review.status == "passed"
    assert review.warnings == ["words_reversed", "word_order_uncertain"]


def test_a_second_statement_of_a_type_is_held_as_a_duplicate() -> None:
    assert review_statement(statement(ROWS), PASSING, primary=False).reasons == [
        "duplicate_statement"
    ]


def test_an_implausible_assumed_scale_holds() -> None:
    flags = ("scale_missing", "scale_implausible")
    held = review_statement(statement(ROWS, flags=flags), PASSING, primary=True)
    assert held.reasons == ["scale_implausible"] and held.warnings == ["scale_missing"]


def test_a_subtotal_that_could_not_be_checked_holds_the_statement() -> None:
    for detail in ("subtotal_scope_uncertain", "subtotal_scope_unknown", "missing_values"):
        checks = [*PASSING, check("subtotal", "skipped", ["r4"], detail)]
        review = review_statement(statement(ROWS), checks, primary=True)
        assert review.status == "needs_review" and review.reasons == ["subtotal_not_checked:1"]


def test_a_figure_dropped_for_want_of_a_box_holds_the_statement() -> None:
    review = review_statement(
        statement(ROWS, flags=("value_without_box:r3c2",)), PASSING, primary=True
    )
    assert review.reasons == ["value_without_box:r3c2"]


def test_an_identity_that_passes_one_period_and_is_skipped_in_another_holds() -> None:
    balance = statement(ROWS, StatementType.BALANCE)
    other = check("balance_identity", "skipped", ["r3", "r4"], "missing_values").model_copy(
        update={"id": "s:balance_identity:other", "period_key": "2024-12-31"}
    )
    checks = [*PASSING, check("balance_identity", "pass", ["r3", "r4"]), other]
    assert review_statement(balance, checks, primary=True).reasons == [
        "identity_not_checked:missing_values"
    ]


def test_a_fraction_among_whole_amounts_holds() -> None:
    rows = [item(1, "10"), item(2, "5"), item(3, "15"), item(4, "7.5", "fraction_among_whole")]
    assert review_statement(statement(rows), PASSING, primary=True).reasons == [
        "fraction_among_whole:1"
    ]


def test_a_one_row_equality_alone_vouches_for_nothing() -> None:
    only = [check("subtotal", "pass", ["r1", "r3"], "single_addend")]
    assert review_statement(statement(ROWS), only, primary=True).reasons == ["unchecked"]
    assert review_statement(statement(ROWS), [*PASSING, *only], primary=True).status == "passed"


def test_a_figure_printed_three_times_is_not_a_check() -> None:
    from fra_core.taxonomy.loader import load_taxonomy
    from fra_ingest.label_match import LabelIndex
    from fra_ingest.table_checks import run_checks

    def row(n: int, label: str, value: str | None, total: bool = False) -> LineItem:
        base = item(n, value) if value is not None else LineItem(id=f"r{n}", raw_label=label)
        return base.model_copy(update={"raw_label": label, "is_subtotal": total})

    rows = [
        row(1, "Profit", "160"),
        row(2, "Total profit", "160", total=True),
        row(3, "Other comprehensive income", "0"),
        row(4, "Total comprehensive income", "160", total=True),
    ]
    checked, checks = run_checks(statement(rows), LabelIndex(load_taxonomy()))
    assert review_statement(checked, checks, primary=True).reasons == ["unchecked"]


def test_named_mapping_findings_hold_a_clean_statement_in_the_review_artifact() -> None:
    from fra_core.taxonomy.loader import load_taxonomy
    from fra_ingest.label_mapping import map_statement
    from fra_ingest.label_match import LabelIndex
    from fra_ingest.results import StructureResult

    mapped = map_statement(statement(ROWS), LabelIndex(load_taxonomy()), PASSING)
    result = review_statement(mapped, PASSING, primary=True)
    assert result.status == "needs_review"
    assert result.reasons == [
        f"critical_item_unmapped:{f.item_id}" for f in mapped.mapping_findings
    ]
    assert (result.numeric_cells, result.checked_cells, result.flagged_cells) == (4, 3, 0)
    artifact = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="2",
        settings_hash="h",
        statements=[mapped],
        reviews=[result],
    )
    assert StructureResult.model_validate_json(artifact.model_dump_json()) == artifact
    # a duplicate keeps the existing single reason; its findings stay on the statement
    duplicate = review_statement(mapped, PASSING, primary=False)
    assert duplicate.reasons == ["duplicate_statement"]
    assert mapped.mapping_findings
