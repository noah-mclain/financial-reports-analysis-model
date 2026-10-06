"""Row labels to canonical items: aliases first, then structural anchors, never a guess."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from fra_core.schemas import (
    BBox,
    Cell,
    CheckResult,
    LineItem,
    MappingSource,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_mapping import ANCHOR_SUMS, map_statement
from fra_ingest.label_match import CLOSING_TOTAL_ID, LabelIndex

TAXONOMY = load_taxonomy()
INDEX = LabelIndex(TAXONOMY)
P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
P2 = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def row(
    n: int, label: str, value: str | None, parent: int | None = None, subtotal: bool = False
) -> LineItem:
    cells = (
        []
        if value is None
        else [
            Cell(
                period_key=P.key,
                reported=Decimal(value),
                raw_text=value,
                provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
            )
        ]
    )
    return LineItem(
        id=f"r{n}",
        raw_label=label,
        is_subtotal=subtotal,
        parent_id=None if parent is None else f"r{parent}",
        cells=cells,
    )


def closing() -> LineItem:
    return row(9, "Total equity and liabilities", "99", subtotal=True)


def statement(items: list[LineItem], kind: StatementType = StatementType.BALANCE) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1,
        periods=[P, P2],
        line_items=items,
    )


def check(kind: str, ids: list[str], status: str = "pass", period: Period = P) -> CheckResult:
    return CheckResult(
        id=f"s:{kind}:{ids[-1]}:{period.key}",
        statement_id="s",
        kind=kind,  # type: ignore[arg-type]
        period_key=period.key,
        status=status,  # type: ignore[arg-type]
        line_item_ids=ids,
    )


def passing(
    items: list[LineItem],
    unchecked: frozenset[str] = frozenset(),
    identity: tuple[str, ...] = (),
) -> list[CheckResult]:
    """A passed subtotal check for every total that has children, but those left unchecked,
    and a passed balance identity naming ``identity`` when given."""
    parents = {i.parent_id for i in items if i.parent_id is not None} - unchecked
    checks = [
        check("subtotal", [*(i.id for i in items if i.parent_id == parent), parent])
        for parent in sorted(parents)
    ]
    if identity:
        checks.append(check("balance_identity", list(identity)))
    return checks


def mapped(
    items: list[LineItem],
    kind: StatementType = StatementType.BALANCE,
    unchecked: frozenset[str] = frozenset(),
    identity: tuple[str, ...] = (),
) -> dict[str, LineItem]:
    out = map_statement(statement(items, kind), INDEX, passing(items, unchecked, identity))
    return {i.id: i for i in out.line_items}


def test_english_and_arabic_aliases_resolve_to_the_same_item() -> None:
    english = mapped(
        [row(1, "Inventories", "10"), row(2, "Total assets", "30", subtotal=True), closing()],
        identity=("r2", "r9"),
    )
    arabic = mapped(
        [row(1, "المخزون", "10"), row(2, "إجمالي الموجودات", "30", subtotal=True), closing()],
        identity=("r2", "r9"),
    )
    expected = ["inventories", "total_assets", "total_liabilities_and_equity"]
    assert [i.canonical_id for i in english.values()] == expected
    assert [i.canonical_id for i in arabic.values()] == expected
    assert english["r1"].mapping_source is MappingSource.LEXICON
    assert english["r1"].mapping_flag is None


def test_a_heading_is_not_mapped_or_flagged() -> None:
    out = mapped([row(1, "Current assets", None), row(2, "Inventories", "10", parent=3)])
    assert out["r1"].canonical_id is None and out["r1"].mapping_flag is None


def under_heading(heading: str = "Current assets", total_value: str = "15") -> list[LineItem]:
    return [
        row(1, heading, None),
        row(2, "Inventories", "10", parent=4),
        row(3, "Trade receivables", "5", parent=4),
        row(4, "", total_value, subtotal=True),
    ]


def test_an_anchor_resolves_an_unlabelled_total_under_its_heading() -> None:
    total = mapped(under_heading())["r4"]
    assert total.canonical_id == "total_current_assets"
    assert total.mapping_source is MappingSource.ANCHOR
    assert total.mapping_evidence == "anchor:section_heading"


def test_an_anchor_reads_a_heading_merged_into_the_first_row() -> None:
    out = mapped(
        [
            row(1, "الموجوداتالمتداولةمخزون", "10", parent=3),
            row(2, "ذمم مدينة", "5", parent=3),
            row(3, "", "15", subtotal=True),
        ]
    )
    assert out["r3"].canonical_id == "total_current_assets"
    assert out["r1"].mapping_flag == "unmapped"


def test_a_first_row_that_only_contains_a_heading_names_no_section() -> None:
    for label in ("Non-current assets held for sale", "Net current assets"):
        out = mapped([row(1, label, "10", parent=2), row(2, "", "10", subtotal=True)])
        assert out["r2"].canonical_id is None, label
        assert out["r2"].mapping_flag == "unmapped", label


def test_a_heading_that_carries_a_value_is_not_the_total() -> None:
    for heading, flag in (("Current assets", "ambiguous"), ("الموجودات المتداولة", "unmapped")):
        items = under_heading()
        items[0] = row(1, heading, "10")
        out = mapped(items)
        assert out["r1"].canonical_id is None and out["r1"].mapping_flag == flag, heading
        assert out["r4"].canonical_id is None and out["r4"].mapping_flag == "unmapped", heading


def test_an_alias_to_a_total_needs_a_check_that_confirms_the_row() -> None:
    items = [
        row(1, "Inventories", "10", parent=2),
        row(2, "Total current assets", "10", subtotal=True),
    ]
    assert mapped(items)["r2"].canonical_id == "total_current_assets"
    out = mapped(items, unchecked=frozenset({"r2"}))["r2"]
    assert out.canonical_id is None and out.mapping_flag == "ambiguous"
    assert out.mapping_evidence == "alias_total_unconfirmed:total_current_assets"


def test_a_total_with_no_parts_is_confirmed_by_a_passed_identity_that_names_it() -> None:
    items = [row(1, "Total assets", "30", subtotal=True), closing()]
    assert mapped(items)["r9"].mapping_flag == "ambiguous"
    assert mapped(items, identity=("r1", "r9"))["r9"].canonical_id == CLOSING_TOTAL_ID
    # Naming other rows confirms nothing here.
    assert mapped(items, identity=("r1", "r5"))["r9"].canonical_id is None


def test_the_last_total_of_a_balance_sheet_is_not_the_closing_total_by_position() -> None:
    funds = mapped(
        [row(1, "Inventories", "10"), row(2, "Total shareholders' funds", "10", subtotal=True)]
    )
    assert funds["r2"].canonical_id is None and funds["r2"].mapping_flag == "unmapped"
    bare = mapped([row(1, "Inventories", "10"), row(2, "", "10", subtotal=True)])
    assert bare["r2"].canonical_id is None and bare["r2"].mapping_flag == "unmapped"


def test_an_anchor_is_a_total_of_totals_already_mapped() -> None:
    out = mapped(
        [
            row(1, "Inventories", "20", parent=2),
            row(2, "Total current assets", "20", parent=5, subtotal=True),
            row(3, "Goodwill", "80", parent=4),
            row(4, "Total non-current assets", "80", parent=5, subtotal=True),
            row(5, "Sum of all", "100", subtotal=True),
        ]
    )
    assert out["r5"].canonical_id == "total_assets"
    assert out["r5"].mapping_evidence == "anchor:sum_of_mapped"


def test_a_sum_of_mapped_totals_is_no_anchor_when_the_sum_is_not_checked() -> None:
    items = [
        row(1, "Inventories", "20", parent=2),
        row(2, "Total current assets", "20", parent=5, subtotal=True),
        row(3, "Goodwill", "80", parent=4),
        row(4, "Total non-current assets", "80", parent=5, subtotal=True),
        row(5, "Sum of all", "100", subtotal=True),
    ]
    out = mapped(items, unchecked=frozenset({"r5"}))
    assert out["r5"].canonical_id is None and out["r5"].mapping_flag == "unmapped"
    assert out["r2"].canonical_id == "total_current_assets"


def test_two_anchors_that_name_different_items_flag_the_row() -> None:
    out = mapped(
        [
            row(1, "Current assets", None),
            row(2, "Inventories", "20", parent=5),
            row(3, "Goodwill", "5", parent=4),
            row(4, "Total current assets", "5", parent=5, subtotal=True),
            row(6, "Land", "75", parent=7),
            row(7, "Total non-current assets", "75", parent=5, subtotal=True),
            row(5, "", "100", subtotal=True),
        ]
    )
    assert out["r5"].canonical_id is None and out["r5"].mapping_flag == "ambiguous"
    assert out["r5"].mapping_evidence is not None
    assert out["r5"].mapping_evidence.startswith("anchor_conflict:")


def test_a_label_matching_two_items_is_flagged_not_mapped() -> None:
    # "net sales" and "netsales" are two spellings the taxonomy keeps apart, and one once
    # spaces are dropped.
    taxonomy = Path(__file__).parent / "ambiguous_taxonomy.yaml"
    index = LabelIndex(load_taxonomy(taxonomy))
    out = map_statement(statement([row(1, "Net sales", "10")], StatementType.INCOME), index, [])
    only = out.line_items[0]
    assert only.canonical_id is None and only.mapping_flag == "ambiguous"
    assert only.mapping_evidence is not None and "item_a" in only.mapping_evidence
    assert "item_b" in only.mapping_evidence


def test_an_alias_hit_that_contradicts_an_anchor_is_flagged_not_mapped() -> None:
    out = mapped(
        [
            row(1, "Current assets", None),
            row(2, "Inventories", "10", parent=3),
            row(3, "Total non-current assets", "10", subtotal=True),
        ]
    )
    total = out["r3"]
    assert total.canonical_id is None and total.mapping_flag == "ambiguous"
    assert total.mapping_evidence is not None
    assert "total_non_current_assets" in total.mapping_evidence
    assert "total_current_assets" in total.mapping_evidence


def test_a_sum_the_review_did_not_confirm_is_no_anchor() -> None:
    items = under_heading(total_value="11")
    out = mapped(items, unchecked=frozenset({"r4"}))
    assert out["r4"].canonical_id is None and out["r4"].mapping_flag == "unmapped"
    assert mapped(items)["r4"].canonical_id == "total_current_assets"


def test_a_check_that_failed_in_any_period_confirms_nothing() -> None:
    items = under_heading()
    base = passing(items)
    failed = check("subtotal", ["r2", "r3", "r4"], status="fail", period=P2)
    out = map_statement(statement(items), INDEX, [*base, failed])
    assert out.line_items[3].canonical_id is None
    assert out.line_items[3].mapping_flag == "unmapped"
    # The same check, failing in no period, anchors it.
    skipped = check("subtotal", ["r2", "r3", "r4"], status="skipped", period=P2)
    again = map_statement(statement(items), INDEX, [*base, skipped])
    assert again.line_items[3].canonical_id == "total_current_assets"


def test_a_check_that_never_passed_confirms_nothing() -> None:
    items = under_heading()
    for status in ("fail", "skipped"):
        checks = [check("subtotal", ["r2", "r3", "r4"], status=status)]
        out = map_statement(statement(items), INDEX, checks)
        assert out.line_items[3].canonical_id is None, status


def test_a_balance_identity_that_failed_in_any_period_confirms_nothing() -> None:
    items = [row(1, "Total assets", "30", subtotal=True), closing()]
    checks = [
        check("balance_identity", ["r1", "r9"]),
        check("balance_identity", ["r1", "r9"], status="fail", period=P2),
    ]
    out = map_statement(statement(items), INDEX, checks)
    assert out.line_items[1].canonical_id is None


def test_a_label_nothing_resolves_is_flagged_unmapped() -> None:
    out = mapped([row(1, "Quarterly widgets", "10")])
    assert out["r1"].canonical_id is None
    assert out["r1"].mapping_flag == "unmapped"


def test_two_rows_with_different_values_for_one_item_are_both_flagged() -> None:
    out = mapped([row(1, "Statutory reserve", "5"), row(2, "Other reserves", "7")])
    assert {i.mapping_flag for i in out.values()} == {"ambiguous"}
    assert all(i.canonical_id is None for i in out.values())


def equity_pair() -> list[LineItem]:
    return [
        row(1, "Paid up capital", "40", parent=2),
        row(2, "Total equity", "40", parent=4, subtotal=True),
        row(3, "Non-controlling interest", "2", parent=4),
        row(4, "Total equity", "42", subtotal=True),
    ]


def test_of_two_total_rows_only_the_outer_one_a_check_confirms_is_mapped() -> None:
    out = mapped(equity_pair())
    assert out["r4"].canonical_id == "total_equity"
    assert out["r2"].mapping_flag == "ambiguous"
    assert out["r2"].mapping_evidence == "duplicate:total_equity"


def test_both_total_rows_are_flagged_when_no_check_confirms_the_outer_one() -> None:
    out = mapped(equity_pair(), unchecked=frozenset({"r4"}))
    assert {out["r2"].mapping_flag, out["r4"].mapping_flag} == {"ambiguous"}
    assert out["r4"].canonical_id is None


def test_a_figure_printed_twice_maps_once() -> None:
    out = mapped(
        [
            row(1, "Profit for the year", "9"),
            row(2, "Net profit", "9"),
        ],
        StatementType.INCOME,
    )
    assert out["r1"].canonical_id == "net_income"
    assert out["r2"].canonical_id is None and out["r2"].mapping_flag == "ambiguous"
    assert out["r2"].mapping_evidence == "repeat_of:r1:net_income"


def test_statements_outside_balance_and_income_are_left_alone() -> None:
    cash = statement([row(1, "Inventories", "10")], StatementType.CASH_FLOW)
    assert map_statement(cash, INDEX, []) == cash


def test_the_wordings_the_golden_documents_print_resolve() -> None:
    out = mapped(
        [
            row(1, "Net Profit/(Loss) for the year", "9"),
            row(2, "Net Profit/(Loss) for the year Before Income Tax", "12"),
            row(3, "نتائج أنشطة التشغيل", "20"),
        ],
        StatementType.INCOME,
    )
    assert out["r1"].canonical_id == "net_income"
    assert out["r2"].canonical_id == "profit_before_tax"
    assert out["r3"].canonical_id == "operating_income"


def test_every_anchor_names_items_of_the_balance_sheet() -> None:
    for total, parts in ANCHOR_SUMS.items():
        for item_id in (total, *parts):
            item = TAXONOMY.by_id(item_id)
            assert item is not None and item.statement is StatementType.BALANCE, item_id


def test_missing_critical_items_are_named_after_mapping_without_changing_observations() -> None:
    original = statement([row(1, "Inventories", "0"), row(2, "Unknown", "9")])
    result = map_statement(original, INDEX, [])
    assert [f.item_id for f in result.mapping_findings] == sorted(
        TAXONOMY.critical_ids(original.type)
    )
    assert all(f.reason == "critical_item_unmapped" for f in result.mapping_findings)
    assert all(f.observed_period_keys == (P.key, P2.key) for f in result.mapping_findings)
    before = original.model_dump(exclude={"line_items", "mapping_findings"})
    assert result.model_dump(exclude={"line_items", "mapping_findings"}) == before
    mapping_fields = {
        "canonical_id",
        "mapping_source",
        "mapping_confidence",
        "mapping_flag",
        "mapping_evidence",
    }
    assert [r.model_dump(exclude=mapping_fields) for r in result.line_items] == [
        r.model_dump(exclude=mapping_fields) for r in original.line_items
    ]
    assert map_statement(result, INDEX, []) == result


def test_remapping_removes_a_resolved_finding() -> None:
    unresolved = map_statement(statement([row(1, "Unknown", "0")], StatementType.INCOME), INDEX, [])
    assert "revenue" in {f.item_id for f in unresolved.mapping_findings}
    edited = unresolved.model_copy(update={"line_items": [row(1, "Revenue", "0")]})
    resolved = map_statement(edited, INDEX, [])
    assert resolved.find("revenue") is not None
    assert "revenue" not in {f.item_id for f in resolved.mapping_findings}
    assert map_statement(resolved, INDEX, []) == resolved


def test_duplicate_claims_get_findings_only_after_settlement() -> None:
    result = map_statement(
        statement([row(1, "Revenue", "10"), row(2, "Revenue", "11")], StatementType.INCOME),
        INDEX,
        [],
    )
    assert result.find("revenue") is None
    assert all(r.mapping_flag == "ambiguous" for r in result.line_items)
    assert "revenue" in {f.item_id for f in result.mapping_findings}
    repeat = map_statement(
        statement([row(1, "Revenue", "0"), row(2, "Revenue", "0")], StatementType.INCOME), INDEX, []
    )
    assert repeat.find("revenue") is not None
    assert "revenue" not in {f.item_id for f in repeat.mapping_findings}


def test_noncritical_and_unvalued_labels_do_not_create_extra_findings() -> None:
    original = statement([row(1, "Revenue", None)], StatementType.INCOME)
    result = map_statement(original, INDEX, [])
    assert result.line_items == original.line_items
    assert "revenue" in {f.item_id for f in result.mapping_findings}
    missing_value = row(1, "Revenue", "0")
    missing_value.cells[0].reported = None
    mapped_none = map_statement(statement([missing_value], StatementType.INCOME), INDEX, [])
    assert mapped_none.find("revenue") is not None
    assert "revenue" not in {f.item_id for f in mapped_none.mapping_findings}
    skipped = statement([], StatementType.CASH_FLOW)
    assert map_statement(skipped, INDEX, []) == skipped
    assert map_statement(skipped, INDEX, []).mapping_findings == ()
