"""The structure stage end to end on a hand-made docling document (spec 11, Data flow)."""

from __future__ import annotations

from decimal import Decimal

from fra_core.schemas import PageMode, StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.results import ConvertResult, RangeConversion
from fra_ingest.structure import StructureInputs, structure_document

SHA = "b" * 64


def cells(rows: list[list[str]], x0: float = 60) -> list[dict[str, object]]:
    out = []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if not text:
                continue
            left = x0 + 150 * c
            out.append(
                {
                    "text": text,
                    "start_row_offset_idx": r,
                    "end_row_offset_idx": r + 1,
                    "start_col_offset_idx": c,
                    "end_col_offset_idx": c + 1,
                    "column_header": r == 0,
                    "bbox": {
                        "l": left,
                        "t": 100 + 20 * r,
                        "r": left + 120,
                        "b": 112 + 20 * r,
                        "coord_origin": "TOPLEFT",
                    },
                }
            )
    return out


def table(ref: int, page: int, rows: list[list[str]], x0: float = 60) -> dict[str, object]:
    return {
        "self_ref": f"#/tables/{ref}",
        "prov": [
            {
                "page_no": page,
                "bbox": {"l": 50, "t": 700, "r": 700, "b": 300, "coord_origin": "BOTTOMLEFT"},
            }
        ],
        "data": {"num_rows": len(rows), "num_cols": 4, "table_cells": cells(rows, x0)},
    }


HEADER = ["", "Notes", "31 December 2025 SAR '000", "31 December 2024 SAR '000"]
PAGE_1 = [
    HEADER,
    ["Inventories", "19", "10", "8"],
    ["Cash", "21", "5", "4"],
    ["Total current assets", "", "15", "12"],
    ["Total assets", "", "15", "12"],
]
PAGE_2 = [
    HEADER,
    ["Trade payables", "", "6", "5"],
    ["Total liabilities", "", "6", "5"],
    ["Share capital", "", "9", "7"],
    ["Total equity", "", "9", "7"],
]
SIGNATURES = [["Chief Financial Officer", "Chief Executive Officer", "Chairman", ""]]


def inputs(documents: list[tuple[str, DlDocument]]) -> StructureInputs:
    convert = ConvertResult(
        version="1",
        sha256=SHA,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=1,
                last_page=2,
                ocr="pdf_aware",
                ocr_language="en-US",
                docling_path="docling/p1-2.json",
                status="ok",
            )
        ],
    )
    return StructureInputs(
        sha256=SHA,
        language="en",
        industry_flags=(),
        page_modes={1: PageMode.TEXT, 2: PageMode.TEXT},
        visual_pages=set(),
        page_texts={1: "Statement of financial position", 2: ""},
        title_types={1: (StatementType.BALANCE,), 2: (StatementType.BALANCE,)},
        cue_types={},
        documents=documents,
        convert=convert,
    )


def document() -> DlDocument:
    return DlDocument.model_validate(
        {
            "tables": [table(0, 1, PAGE_1), table(1, 2, PAGE_2), table(2, 2, SIGNATURES)],
            "texts": [],
            "pages": {
                "1": {"page_no": 1, "size": {"width": 800, "height": 1000}},
                "2": {"page_no": 2, "size": {"width": 800, "height": 1000}},
            },
        }
    )


def test_a_balance_sheet_over_two_pages_becomes_one_checked_statement() -> None:
    result, checks = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    assert len(result.statements) == 1
    statement = result.statements[0]
    assert statement.type is StatementType.BALANCE
    assert (statement.currency, statement.scale) == ("SAR", 1000)
    assert statement.source_pages == [1, 2]
    assert [p.key for p in statement.periods] == ["2025-12-31", "2024-12-31"]
    labels = [i.raw_label for i in statement.line_items]
    assert labels[0] == "Inventories" and labels[-1] == "Total equity"
    assert statement.line_items[0].cells[0].reported == Decimal("10")
    identity = [c for c in checks if c.kind == "balance_identity"]
    assert {c.status for c in identity} == {"pass"}
    signature = [t for t in result.tables if t.table_ref == "#/tables/2"]
    assert signature and signature[0].type is None


def test_decisions_name_the_statement_each_table_became() -> None:
    result, _ = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    statement_id = result.statements[0].id
    kept = {t.table_ref: t.statement_id for t in result.tables}
    assert kept == {"#/tables/0": statement_id, "#/tables/1": statement_id, "#/tables/2": None}


def test_two_balance_sheets_on_one_page_are_flagged_ambiguous() -> None:
    # Columns that do not line up keep the second table from continuing the first.
    doubled = DlDocument.model_validate(
        {
            "tables": [table(0, 1, PAGE_1), table(1, 1, PAGE_1, x0=160)],
            "texts": [],
            "pages": {"1": {"page_no": 1, "size": {"width": 800, "height": 1000}}},
        }
    )
    result, _ = structure_document(inputs([("docling/p1-2.json", doubled)]), IngestConfig())
    balance = [s for s in result.statements if s.type is StatementType.BALANCE]
    assert len(balance) == 2
    assert all("ambiguous_statement:balance" in s.flags for s in balance)


CLOSED = [*PAGE_1, ["Total equity and liabilities", "", "15", "12"]]


def _balance_tables(*tables: dict[str, object]) -> DlDocument:
    return DlDocument.model_validate(
        {
            "tables": list(tables),
            "texts": [],
            "pages": {
                "1": {"page_no": 1, "size": {"width": 800, "height": 1000}},
                "2": {"page_no": 2, "size": {"width": 800, "height": 1000}},
            },
        }
    )


def test_a_closed_balance_sheet_is_not_merged_with_one_printed_after_it() -> None:
    doubled = _balance_tables(table(0, 1, CLOSED), table(1, 1, CLOSED))
    result, _ = structure_document(inputs([("docling/p1-2.json", doubled)]), IngestConfig())
    balance = [s for s in result.statements if s.type is StatementType.BALANCE]
    assert [s.source_pages for s in balance] == [[1], [1]]
    assert all("ambiguous_statement:balance" in s.flags for s in balance)
    reviews = {r.statement_id: r for r in result.reviews}
    assert "duplicate_statement" not in reviews[balance[0].id].reasons
    assert reviews[balance[1].id].reasons == ["duplicate_statement"]


def test_two_balance_sheets_on_one_page_each_keep_their_own_scale_and_currency() -> None:
    other = [["", "Notes", "31 December 2025 EGP millions", "31 December 2024 EGP millions"]]
    separate = _balance_tables(table(0, 1, CLOSED), table(1, 1, [*other, *CLOSED[1:]]))
    result, _ = structure_document(inputs([("docling/p1-2.json", separate)]), IngestConfig())
    balance = [s for s in result.statements if s.type is StatementType.BALANCE]
    assert sorted((s.currency, s.scale) for s in balance) == [("EGP", 1_000_000), ("SAR", 1000)]


def test_a_statement_after_a_closed_one_still_takes_an_undated_period_by_position() -> None:
    undated = [["", "Notes", "31 December 2025 SAR '000", ""], *CLOSED[1:]]
    document = _balance_tables(table(0, 1, CLOSED), table(1, 2, undated))
    result, _ = structure_document(inputs([("docling/p1-2.json", document)]), IngestConfig())
    balance = [s for s in result.statements if s.type is StatementType.BALANCE]
    assert [s.source_pages for s in balance] == [[1], [2]]
    assert [p.key for p in balance[1].periods] == ["2025-12-31", "2024-12-31"]
    assert not any(f.startswith("period_unbound") for f in balance[1].flags)


def _after_closed(first: list[list[str]]) -> StructureInputs:
    tail = [HEADER, ["Trade payables", "", "6", "5"]]
    document = _balance_tables(table(0, 1, first), table(1, 2, tail))
    base = inputs([("docling/p1-2.json", document)])
    return base.model_copy(
        update={
            "title_types": {1: (StatementType.BALANCE,), 2: ()},
            "cue_types": {2: (StatementType.BALANCE,)},
        }
    )


def test_a_tail_grid_after_a_closed_balance_sheet_is_refused_with_its_reason() -> None:
    structured = _after_closed(CLOSED)
    result, _ = structure_document(structured, IngestConfig())
    balance = [s for s in result.statements if s.type is StatementType.BALANCE]
    assert [s.source_pages for s in balance] == [[1]]
    assert "Trade payables" not in [i.raw_label for i in balance[0].line_items]
    decision = next(t for t in result.tables if t.table_ref == "#/tables/1")
    assert decision.type is None and decision.statement_id is None
    assert decision.evidence[-1] == "after_closed:balance"


def test_a_tail_grid_after_an_open_balance_sheet_still_continues() -> None:
    structured = _after_closed(PAGE_1)
    result, _ = structure_document(structured, IngestConfig())
    decision = next(t for t in result.tables if t.table_ref == "#/tables/1")
    assert "continuation_of:balance" in decision.evidence
    assert not any(e.startswith("after_closed:") for e in decision.evidence)


def test_a_tail_grid_that_would_not_have_continued_gets_no_closed_reason() -> None:
    structured = _after_closed(CLOSED)
    shifted = _balance_tables(
        table(0, 1, CLOSED), table(1, 2, [HEADER, ["Trade payables", "", "6", "5"]], x0=160)
    )
    result, _ = structure_document(
        structured.model_copy(update={"documents": [("docling/p1-2.json", shifted)]}),
        IngestConfig(),
    )
    decision = next(t for t in result.tables if t.table_ref == "#/tables/1")
    assert not any(e.startswith("after_closed:") for e in decision.evidence)


def test_a_missing_enabled_type_is_flagged() -> None:
    result, _ = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    assert "statement_not_extracted:income" in result.flags


INCOME_HEADER = ["", "Notes", "2025 SAR '000", "2024 SAR '000"]
INCOME_1 = [
    INCOME_HEADER,
    ["Revenue", "5", "100", "90"],
    ["Cost of sales", "", "(60)", "(55)"],
    ["Gross profit", "", "40", "35"],
]
INCOME_2 = [
    INCOME_HEADER,
    ["Profit for the year", "", "40", "35"],
    ["Basic earnings per share", "", "0.4", "0.35"],
]


def test_a_tail_page_below_confidence_continues_the_statement_before_it() -> None:
    income = DlDocument.model_validate(
        {
            "tables": [table(0, 1, INCOME_1), table(1, 2, INCOME_2)],
            "texts": [],
            "pages": {
                "1": {"page_no": 1, "size": {"width": 800, "height": 1000}},
                "2": {"page_no": 2, "size": {"width": 800, "height": 1000}},
            },
        }
    )
    base = inputs([("docling/p1-2.json", income)])
    tail = base.model_copy(
        update={
            "page_texts": {1: "Statement of profit or loss", 2: ""},
            "title_types": {1: (StatementType.INCOME,), 2: ()},
            "cue_types": {2: (StatementType.INCOME,)},
        }
    )
    result, _ = structure_document(tail, IngestConfig())
    statements = [s for s in result.statements if s.type is StatementType.INCOME]
    assert len(statements) == 1
    assert statements[0].source_pages == [1, 2]
    assert [i.raw_label for i in statements[0].line_items][-2:] == [
        "Profit for the year",
        "Basic earnings per share",
    ]
    decision = next(t for t in result.tables if t.table_ref == "#/tables/1")
    assert decision.type is StatementType.INCOME
    assert "continuation_of:income" in decision.evidence
    assert decision.statement_id == statements[0].id


def _income_pages(second: list[list[str]], second_page: int = 2, x0: float = 60) -> DlDocument:
    pages = sorted({1, second_page})
    return DlDocument.model_validate(
        {
            "tables": [table(0, 1, INCOME_1), table(1, second_page, second, x0)],
            "texts": [],
            "pages": {
                str(n): {"page_no": n, "size": {"width": 800, "height": 1000}} for n in pages
            },
        }
    )


def _income_inputs(
    document: DlDocument, titles: dict[int, tuple[StatementType, ...]]
) -> StructureInputs:
    base = inputs([("docling/p1-2.json", document)])
    return base.model_copy(
        update={
            "page_texts": {1: "Statement of profit or loss"},
            "title_types": {1: (StatementType.INCOME,), **titles},
            "cue_types": {n: (StatementType.INCOME,) for n in titles},
            "page_modes": {n: PageMode.TEXT for n in (1, *titles)},
        }
    )


def _tail_decision(
    document: DlDocument, titles: dict[int, tuple[StatementType, ...]]
) -> tuple[list[int], list[str], StatementType | None]:
    result, _ = structure_document(_income_inputs(document, titles), IngestConfig())
    income = [s for s in result.statements if s.type is StatementType.INCOME]
    assert len(income) == 1
    decision = next(t for t in result.tables if t.table_ref == "#/tables/1")
    return income[0].source_pages, decision.evidence, decision.type


def test_a_tail_page_titled_as_another_statement_does_not_continue() -> None:
    pages, evidence, kind = _tail_decision(
        _income_pages(INCOME_2), {2: (StatementType.COMPREHENSIVE_INCOME,)}
    )
    assert pages == [1]
    assert not any(e.startswith("continuation_of:") for e in evidence)
    assert kind is not StatementType.INCOME


def test_a_tail_page_with_other_periods_stays_unattached() -> None:
    other = [["", "Notes", "2023 SAR '000", "2022 SAR '000"], *INCOME_2[1:]]
    pages, evidence, kind = _tail_decision(_income_pages(other), {2: ()})
    assert pages == [1]
    assert kind is None and "continuation_of:income" not in evidence


def test_a_tail_page_with_shifted_columns_stays_unattached() -> None:
    pages, evidence, kind = _tail_decision(_income_pages(INCOME_2, x0=160), {2: ()})
    assert pages == [1]
    assert kind is None and "continuation_of:income" not in evidence


def test_a_tail_page_two_pages_after_the_part_stays_unattached() -> None:
    pages, evidence, kind = _tail_decision(_income_pages(INCOME_2, second_page=3), {3: ()})
    assert pages == [1]
    assert kind is None and "continuation_of:income" not in evidence


def test_a_tail_page_below_confidence_takes_an_undated_column_from_the_page_before() -> None:
    undated = [["", "Notes", "2025 SAR '000", ""], *INCOME_2[1:]]
    pages, evidence, kind = _tail_decision(_income_pages(undated), {2: ()})
    assert pages == [1, 2]
    assert kind is StatementType.INCOME and "continuation_of:income" in evidence
    result, _ = structure_document(_income_inputs(_income_pages(undated), {2: ()}), IngestConfig())
    profit = next(i for i in result.statements[0].line_items if i.raw_label.startswith("Profit"))
    assert [(c.period_key, c.reported) for c in profit.cells] == [
        ("FY2025", Decimal("40")),
        ("FY2024", Decimal("35")),
    ]
    assert "inherited:3:FY2024" in result.statements[0].flags


def test_a_total_whose_values_were_all_lost_is_missing_values_not_a_heading() -> None:
    lost = [
        HEADER,
        ["Inventories", "19", "10", "8"],
        ["Cash", "21", "5", "4"],
        ["Total assets", "", "", ""],
    ]
    doc = DlDocument.model_validate(
        {
            "tables": [table(0, 1, lost), table(1, 2, PAGE_2)],
            "texts": [],
            "pages": {
                "1": {"page_no": 1, "size": {"width": 800, "height": 1000}},
                "2": {"page_no": 2, "size": {"width": 800, "height": 1000}},
            },
        }
    )
    result, checks = structure_document(inputs([("docling/p1-2.json", doc)]), IngestConfig())
    balance = next(s for s in result.statements if s.type is StatementType.BALANCE)
    total = next(i for i in balance.line_items if i.raw_label == "Total assets")
    assert [c.reported for c in total.cells] == [None, None]
    assert all("numbers_missing" in c.flags for c in total.cells)
    assert total.is_subtotal
    identity = [c for c in checks if c.kind == "balance_identity"]
    assert identity and all((c.status, c.detail) == ("skipped", "missing_values") for c in identity)
    assert "identity_totals_not_found" not in balance.flags


def test_an_uncertain_industry_verdict_holds_every_statement_with_its_reason() -> None:
    held = inputs([("docling/p1-2.json", document())]).model_copy(
        update={"industry_hold": "near_threshold"}
    )
    result, _ = structure_document(held, IngestConfig())
    assert result.statements
    assert all(s.flags.count("needs_review") == 1 for s in result.statements)  # held once
    assert "industry_uncertain:near_threshold" in result.flags
    assert all(
        r.status == "needs_review" and "industry_uncertain:near_threshold" in r.reasons
        for r in result.reviews
    )
    plain, _ = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    assert not any("industry_uncertain:near_threshold" in r.reasons for r in plain.reviews)
    assert not any(f.startswith("industry_uncertain") for f in plain.flags)
