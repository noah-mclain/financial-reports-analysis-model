"""Grid rows to line items with provenance (spec 11, Data flow step 7)."""

from decimal import Decimal

from fra_core.schemas import BBox, StatementType, TextSource
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.classify import Classification
from fra_ingest.header import parse_header
from fra_ingest.label_match import LabelIndex
from fra_ingest.parts import PartialStatement, build_part, column_centre, is_per_share
from fra_ingest.table_grid import Grid, GridCell


def box(col: int, row: int, left_pad: float = 0) -> BBox:
    return BBox(
        left=100 * col + 10 + left_pad, top=20 * row + 5, right=100 * col + 90, bottom=20 * row + 15
    )


def grid(rows: list[list[str]], pads: dict[int, float] | None = None) -> Grid:
    cells = tuple(
        GridCell(
            text=t,
            row=r,
            col=c,
            bbox=box(c, r, (pads or {}).get(r, 0) if c == 0 else 0),
            page_no=156,
            is_column_header=r == 0,
        )
        for r, row in enumerate(rows)
        for c, t in enumerate(row)
        if t
    )
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p155-164.json",
        page_no=156,
        page_width=800,
        num_rows=len(rows),
        num_cols=4,
        cells=cells,
    )


ROWS = [
    ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
    ["Non-Current Assets", "", "", ""],
    ["Property, Plant and Equipment", "7", "26,058,632", "22,750,342"],
    ["Investments", "12", "-", "3,256"],
    ["Biological Assets", "11", "", "1,838,353"],
    ["Basic earnings per share", "", "2.10", "1.90"],
]


def part() -> PartialStatement:
    g = grid(ROWS, pads={2: 10, 3: 10, 4: 10})
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    return build_part(g, layout, classification, source=TextSource.TEXT)


def test_what_repair_did_to_the_table_reaches_the_statement_flags() -> None:
    g = grid(ROWS, pads={2: 10, 3: 10, 4: 10}).model_copy(update={"flags": ("words_reversed",)})
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    built = build_part(g, layout, classification, source=TextSource.TEXT)
    assert "words_reversed" in built.flags
    assert "words_reversed" not in part().flags


def test_line_items_carry_values_notes_and_provenance() -> None:
    p = part()
    assert [i.raw_label for i in p.line_items] == [
        "Non-Current Assets",
        "Property, Plant and Equipment",
        "Investments",
        "Biological Assets",
        "Basic earnings per share",
    ]
    ppe = p.line_items[1]
    assert ppe.id == "p156-t0-r2" and ppe.note_ref == "7"
    cell = ppe.cells[0]
    assert (cell.period_key, cell.reported, cell.raw_text) == (
        "2025-12-31",
        Decimal("26058632"),
        "26,058,632",
    )
    assert (
        cell.provenance.page_no,
        cell.provenance.table_ref,
        cell.provenance.row,
        cell.provenance.col,
    ) == (156, "#/tables/0", 2, 2)
    assert cell.provenance.source is TextSource.TEXT
    assert p.line_items[0].cells == []


def test_a_dash_is_zero_and_an_empty_cell_is_missing() -> None:
    p = part()
    investments = p.line_items[2].cells
    assert investments[0].reported == Decimal("0")
    biological = p.line_items[3].cells
    assert biological[0].reported is None and "numbers_missing" in biological[0].flags
    assert biological[0].provenance.bbox.left == 210


def test_per_share_rows_are_marked() -> None:
    eps = part().line_items[4]
    assert all("per_share" in c.flags for c in eps.cells)


def test_periods_centres_and_indents() -> None:
    p = part()
    assert [x.key for x in p.periods] == ["2025-12-31", "2024-12-31"]
    assert p.column_centres == {"2025-12-31": 250.0, "2024-12-31": 350.0}
    assert p.indents["p156-t0-r2"] == 20.0 and p.indents["p156-t0-r1"] == 10.0
    assert (p.first_page, p.last_page, p.table_refs) == (
        156,
        156,
        ["docling/p155-164.json#/tables/0"],
    )


def test_column_centre_is_the_mean_of_its_cells() -> None:
    g = grid(ROWS)
    assert column_centre(g, 2, [2, 3]) == 250.0
    assert column_centre(g, 2, [1]) is None


def test_a_spanning_value_stays_in_its_own_column() -> None:
    rows = [r[:] for r in ROWS[:3]]
    rows[2][3] = ""
    g = grid(rows)
    cells = tuple(
        c.model_copy(update={"col_span": 2}) if (c.row, c.col) == (2, 2) else c for c in g.cells
    )
    g = g.model_copy(update={"cells": cells})
    layout = parse_header(g, StatementType.BALANCE, None)
    p = build_part(
        g,
        layout,
        Classification(type=StatementType.BALANCE, confidence=0.8),
        source=TextSource.TEXT,
    )
    first, second = p.line_items[1].cells
    assert first.reported == Decimal("26058632") and first.provenance.col == 2
    assert second.reported is None and second.raw_text == ""
    assert "spanned_cell" in second.flags and "numbers_missing" in second.flags


def test_a_value_without_any_box_is_recorded_on_the_part() -> None:
    g = grid(ROWS)
    g = g.model_copy(update={"cells": tuple(c.model_copy(update={"bbox": None}) for c in g.cells)})
    layout = parse_header(g, StatementType.BALANCE, None)
    p = build_part(
        g,
        layout,
        Classification(type=StatementType.BALANCE, confidence=0.8),
        source=TextSource.TEXT,
    )
    assert "value_without_box:r2c2" in p.flags and "value_without_box:r2c3" in p.flags
    assert p.line_items[1].cells == []


def test_right_aligned_labels_indent_from_the_right_edge() -> None:
    rows = [["31 December 2025", "31 December 2024", "Notes", ""]] + [
        [v1, v2, "", label]
        for label, v1, v2 in (("Assets", "", ""), ("Inventories", "10", "20"), ("Cash", "30", "40"))
    ]
    g = grid(rows)
    cells = tuple(
        c.model_copy(
            update={
                "bbox": BBox(
                    left=c.bbox.left,
                    top=c.bbox.top,
                    right=c.bbox.right - (15 if (c.col == 3 and c.row == 2) else 0),
                    bottom=c.bbox.bottom,
                )
            }
        )
        if c.bbox is not None
        else c
        for c in g.cells
    )
    g = g.model_copy(update={"cells": cells})
    layout = parse_header(g, StatementType.BALANCE, None)
    assert layout.label_col == 3
    p = build_part(
        g,
        layout,
        Classification(type=StatementType.BALANCE, confidence=0.8),
        source=TextSource.TEXT,
    )
    right = g.right_edge()
    assert right == 390
    assert (
        p.indents["p156-t0-r1"] == 0
        and p.indents["p156-t0-r2"] == 15
        and p.indents["p156-t0-r3"] == 0
    )


def test_a_value_beyond_ten_to_the_fifteen_is_flagged_implausible() -> None:
    rows = [
        ROWS[0],
        ["Merged digits", "", "2,077,685,182,000,000", "(1,000,000,000,000,001)"],
        ["Largest plausible", "", "1,000,000,000,000,000", "5"],
    ]
    g = grid(rows)
    p = build_part(
        g,
        parse_header(g, StatementType.BALANCE, None),
        Classification(type=StatementType.BALANCE, confidence=0.8),
        source=TextSource.TEXT,
    )
    flagged = [["implausible_magnitude" in c.flags for c in item.cells] for item in p.line_items]
    assert flagged == [[True, True], [False, False]]


def test_a_known_total_with_no_values_keeps_its_cells_as_missing() -> None:
    rows = [
        ROWS[0],
        ["Current assets", "", "", ""],
        ["Total assets", "", "", ""],
        ["Total comprehensive income attributable to:", "", "", ""],
        ["Inventories", "", "10", "8"],
    ]
    g = grid(rows)
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    p = build_part(
        g, layout, classification, source=TextSource.OCR, index=LabelIndex(load_taxonomy())
    )
    heading, total, other, _ = p.line_items
    assert heading.cells == [] and other.cells == []
    assert [c.reported for c in total.cells] == [None, None]
    assert all({"numbers_missing", "bbox_synthesized"} <= set(c.flags) for c in total.cells)


def test_a_value_far_beyond_its_column_is_flagged_implausible() -> None:
    rows = [
        ROWS[0],
        ["Inventories", "", "120,500", "110,200"],
        ["Receivables", "", "98,300", "91,000"],
        ["Cash", "", "45,100", "52,700"],
        ["Prepayments", "", "12,900", "123,456,789,012"],
        ["Other", "", "7,400", "6,900"],
        ["Total current assets", "", "284,200", "270,300"],
        ["Basic earnings per share", "", "2.10", "1.90"],
    ]
    g = grid(rows)
    p = build_part(
        g,
        parse_header(g, StatementType.BALANCE, None),
        Classification(type=StatementType.BALANCE, confidence=0.8),
        source=TextSource.OCR,
    )
    flagged = [
        (item.raw_label, c.period_key)
        for item in p.line_items
        for c in item.cells
        if "implausible_magnitude" in c.flags
    ]
    assert flagged == [("Prepayments", "2024-12-31")]


def test_a_moved_cell_keeps_its_docling_row_and_merged_labels_are_flagged() -> None:
    g = grid(ROWS, pads={2: 10, 3: 10, 4: 10})
    cells = tuple(
        c.model_copy(update={"source_row": 9, "flags": ("row_realigned",)})
        if (c.row, c.col) == (2, 2)
        else c
        for c in g.cells
    )
    g = g.model_copy(update={"cells": cells})
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    p = build_part(g, layout, classification, source=TextSource.TEXT, merged_rows=[3])
    moved = p.line_items[1].cells[0]
    assert moved.provenance.row == 9 and "row_realigned" in moved.flags
    assert all("label_merged" in c.flags for c in p.line_items[2].cells)


def test_rows_under_a_per_share_heading_are_per_share() -> None:
    rows = [
        ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
        ["Profit for the year", "", "2,456,673", "2,313,667"],
        ["Earnings per Share, based on Profit for the year", "", "", ""],
        ["- Basic", "32", "2.48", "2.34"],
        ["- Diluted", "32", "2.46", "2.31"],
        ["Dividends", "", "", ""],
        ["Declared", "", "1,000", "900"],
    ]
    g = grid(rows)
    layout = parse_header(g, StatementType.INCOME, None)
    classification = Classification(type=StatementType.INCOME, confidence=0.8)
    p = build_part(g, layout, classification, source=TextSource.TEXT)
    per_share = {
        i.raw_label: all("per_share" in c.flags for c in i.cells) for i in p.line_items if i.cells
    }
    assert per_share == {
        "Profit for the year": False,
        "- Basic": True,
        "- Diluted": True,
        "Declared": False,
    }


def test_arabic_share_of_profit_per_share_rows_are_per_share() -> None:
    rows = [
        ["", "إيضاح", "31 ديسمبر 2025", "31 ديسمبر 2024"],
        ["صافي ربح العام", "", "1,606", "1,613"],
        ["نصيب السهم الأساسي و السهم المخفض في الأرباح", "30", "2,18", "2,25"],
    ]
    g = grid(rows)
    layout = parse_header(g, StatementType.INCOME, None)
    classification = Classification(type=StatementType.INCOME, confidence=0.8)
    p = build_part(g, layout, classification, source=TextSource.TEXT)
    assert all("per_share" in c.flags for c in p.line_items[-1].cells)
    assert not any("per_share" in c.flags for c in p.line_items[0].cells)


def test_per_share_labels_are_recognised_through_abbreviations_and_ocr_noise() -> None:
    for label in (
        "EPS - Basic",
        "DPS",
        "صيب السهم الاساسي و السهم المخفض",
        "توزيعات الأرباح لكل سهم",
    ):
        assert is_per_share(label), label
    for label in ("Share capital", "Deposits", "علاوة إصدار الأسهم", "Steps taken"):
        assert not is_per_share(label), label


def test_a_per_share_heading_reaches_only_the_basic_and_diluted_rows_under_it() -> None:
    rows = [
        ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
        ["Earnings per share", "", "", ""],
        ["- Basic", "32", "2.48", "2.34"],
        ["- Diluted", "32", "2.46", "2.31"],
        ["Profit for the year", "", "2,456,673", "2,313,667"],
        ["Other comprehensive income", "", "212,833", "(443,574)"],
    ]
    g = grid(rows)
    layout = parse_header(g, StatementType.INCOME, None)
    classification = Classification(type=StatementType.INCOME, confidence=0.8)
    p = build_part(g, layout, classification, source=TextSource.TEXT)
    per_share = {
        i.raw_label: all("per_share" in c.flags for c in i.cells) for i in p.line_items if i.cells
    }
    assert per_share == {
        "- Basic": True,
        "- Diluted": True,
        "Profit for the year": False,
        "Other comprehensive income": False,
    }


def test_per_share_wording_with_words_between_and_the_arabic_singular() -> None:
    for label in ("Earnings per ordinary share", "العائد على السهم", "حصة السهم من الأرباح"):
        assert is_per_share(label), label
    for label in ("Share of profit of associates", "رأس المال السهمي", "أسهم خزينة"):
        assert not is_per_share(label), label


def test_per_share_cues_do_not_take_amounts_and_survive_lost_spaces() -> None:
    for label in (
        "Expenses per function: share-based payment",
        "Cost per employee share scheme",
        "رأس المال المصدر - القيمة الاسمية 10 جنيه السهم",
    ):
        assert not is_per_share(label), label
    for label in ("العائدعلىالسهم", "Earnings per common share", "Loss per share - diluted"):
        assert is_per_share(label), label


def test_a_per_share_heading_reaches_continuing_and_discontinued_rows() -> None:
    rows = [
        ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
        ["Profit for the year", "", "2,456,673", "2,313,667"],
        ["Earnings per share", "", "", ""],
        ["From continuing operations", "32", "2.00", "2.48"],
        ["From discontinued operations", "32", "0.10", "0.12"],
    ]
    g = grid(rows)
    layout = parse_header(g, StatementType.INCOME, None)
    classification = Classification(type=StatementType.INCOME, confidence=0.8)
    p = build_part(g, layout, classification, source=TextSource.TEXT)
    assert [all("per_share" in c.flags for c in i.cells) for i in p.line_items if i.cells] == [
        False,
        True,
        True,
    ]
