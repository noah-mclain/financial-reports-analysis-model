"""The label mapping report: slot states, and what an expected file can and cannot confirm."""

from datetime import date
from decimal import Decimal

from harness.expected import ExpectedFile, ExpectedRow, ExpectedStatement
from harness.mapping import Verdict, document_period_kind, slot_state, verdict

from fra_core.schemas import (
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
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_match import LabelIndex

TAXONOMY = load_taxonomy()
INDEX = LabelIndex(TAXONOMY)
P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(
    n: int,
    label: str,
    value: str,
    canonical: str | None = None,
    source: MappingSource = MappingSource.LEXICON,
    flag: str | None = None,
    evidence: str | None = None,
) -> LineItem:
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value),
        raw_text=value,
        provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
    )
    return LineItem(
        id=f"r{n}",
        raw_label=label,
        cells=[cell],
        canonical_id=canonical,
        mapping_source=source if canonical else None,
        mapping_flag=flag,  # type: ignore[arg-type]
        mapping_evidence=evidence,
    )


def statement(items: list[LineItem], kind: StatementType = StatementType.BALANCE) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1,
        periods=[P],
        line_items=items,
    )


def expected(*rows: tuple[str, str], kind: StatementType = StatementType.BALANCE) -> ExpectedFile:
    return ExpectedFile(
        id="d",
        sha256="b" * 64,
        status="draft",
        statements=[
            ExpectedStatement(
                type=kind,
                pages=[1],
                page_mode="digital",
                scale=1,
                currency="SAR",
                periods=[P],
                rows=[ExpectedRow(label=label, values={P.key: Decimal(v)}) for label, v in rows],
            )
        ],
    )


def test_a_slot_is_mapped_flagged_or_missing() -> None:
    s = statement(
        [
            item(1, "Total assets", "30", "total_assets"),
            item(2, "x", "5", flag="ambiguous", evidence="alias_total_unconfirmed:total_equity"),
        ]
    )
    assert slot_state(s, "total_assets") == "mapped"
    assert slot_state(s, "total_equity") == "ambiguous"
    assert slot_state(s, "total_liabilities") == "unmapped"
    assert slot_state(None, "total_assets") == "statement_not_found"


def test_a_flag_names_an_item_by_its_exact_id_not_by_a_substring() -> None:
    flagged = item(
        1,
        "x",
        "5",
        flag="ambiguous",
        evidence="alias_multiple:net_income_attributable_parent|revenue",
    )
    s = statement([flagged])
    assert slot_state(s, "net_income") == "unmapped"
    assert slot_state(s, "net_income_attributable_parent") == "ambiguous"


def test_a_mapped_row_the_expected_label_confirms_through_the_lexicon_is_only_consistent() -> None:
    s = statement([item(1, "Total assets", "30", "total_assets")])
    found = verdict(s, "total_assets", expected(("Total assets", "30")), INDEX)
    assert found == Verdict("consistent", "")


def test_an_anchored_row_the_expected_label_confirms_is_verified() -> None:
    s = statement([item(1, "", "15", "total_current_assets", MappingSource.ANCHOR)])
    found = verdict(s, "total_current_assets", expected(("Total current assets", "15")), INDEX)
    assert found.kind == "verified"


def test_an_expected_row_of_another_item_is_a_disagreement() -> None:
    s = statement([item(1, "Total assets", "30", "total_assets")])
    found = verdict(s, "total_assets", expected(("Total liabilities", "30")), INDEX)
    assert found.kind == "disagrees"
    assert "total_liabilities" in found.reason


def test_an_item_whose_id_extends_another_is_not_confirmed_by_it() -> None:
    s = statement([item(1, "Net profit", "9", "net_income")], StatementType.INCOME)
    found = verdict(
        s,
        "net_income",
        expected(
            ("Profit attributable to shareholders of the parent", "9"), kind=StatementType.INCOME
        ),
        INDEX,
    )
    assert found.kind == "disagrees"


def test_total_assets_and_the_closing_total_are_told_apart_by_position() -> None:
    both = [
        item(1, "Total assets", "30", "total_assets"),
        item(2, "Total equity and liabilities", "30", "total_liabilities_and_equity"),
    ]
    s = statement(both)
    exp = expected(("Total assets", "30"), ("Total equity and liabilities", "30"))
    assert verdict(s, "total_assets", exp, INDEX).kind == "consistent"
    assert verdict(s, "total_liabilities_and_equity", exp, INDEX).kind == "consistent"
    swapped = statement(
        [
            item(1, "Total assets", "30", "total_liabilities_and_equity"),
            item(2, "Total equity and liabilities", "30", "total_assets"),
        ]
    )
    assert verdict(swapped, "total_assets", exp, INDEX).kind == "disagrees"
    assert verdict(swapped, "total_liabilities_and_equity", exp, INDEX).kind == "disagrees"


def test_rows_sharing_figures_that_the_expected_file_does_not_share_give_no_verdict() -> None:
    s = statement(
        [
            item(1, "Total assets", "30", "total_assets"),
            item(2, "Total equity and liabilities", "30", "total_liabilities_and_equity"),
        ]
    )
    found = verdict(s, "total_assets", expected(("Total assets", "30")), INDEX)
    assert found.kind == "no_verdict" and "shared" in found.reason


def test_no_verdict_without_an_expected_file_a_label_or_a_row() -> None:
    s = statement([item(1, "Total assets", "30", "total_assets")])
    assert verdict(s, "total_assets", None, INDEX).reason == "no expected file"
    assert verdict(s, "total_assets", expected((" ", "30")), INDEX).reason == "blank expected label"
    nothing = verdict(s, "total_assets", expected(("Total assets", "31")), INDEX)
    assert nothing.kind == "no_verdict" and nothing.reason == "figures on no expected row"
    unknown = verdict(s, "total_assets", expected(("Chairman", "30")), INDEX)
    assert unknown.kind == "no_verdict" and unknown.reason == "expected label not in the lexicon"


def test_the_period_kind_is_read_from_the_statements() -> None:
    annual = Period(key="FY", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
    half = Period(key="H1", end_date=date(2025, 6, 30), kind=PeriodKind.DURATION, months=6)
    base = statement([item(1, "x", "1")])
    assert document_period_kind([base]) == "unknown"
    assert document_period_kind([base.model_copy(update={"periods": [annual]})]) == "annual"
    assert document_period_kind([base.model_copy(update={"periods": [half]})]) == "interim"
