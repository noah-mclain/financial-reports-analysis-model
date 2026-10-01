"""Tables to statement types (spec 11, Data flow step 4)."""

from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.classify import TableContext, classify
from fra_ingest.header import parse_header
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_grid import Grid, GridCell

INDEX = LabelIndex(load_taxonomy())
BALANCE, INCOME = StatementType.BALANCE, StatementType.INCOME


def grid(rows: list[list[str]]) -> Grid:
    cells = tuple(
        GridCell(text=t, row=r, col=c, bbox=None, page_no=1, is_column_header=r == 0)
        for r, row in enumerate(rows)
        for c, t in enumerate(row)
        if t
    )
    return Grid(
        table_ref="#/tables/0",
        docling_path="d",
        page_no=1,
        page_width=600,
        num_rows=len(rows),
        num_cols=4,
        cells=cells,
    )


def run(rows: list[list[str]], context: TableContext) -> StatementType | None:
    g = grid(rows)
    return classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5).type


NO_TITLE = TableContext(title_types=(), cue_types=(), heading_texts=(), industry_flags=())
BALANCE_PAGE = TableContext(
    title_types=(BALANCE,), cue_types=(), heading_texts=(), industry_flags=()
)
HEADER = ["", "Notes", "2025", "2024"]


def test_an_income_statement_by_its_labels() -> None:
    rows = [
        HEADER,
        ["Revenue", "33", "100", "90"],
        ["Cost of sales", "27", "(60)", "(50)"],
        ["Gross profit", "", "40", "40"],
        ["Finance costs", "", "(5)", "(4)"],
    ]
    assert run(rows, NO_TITLE) is INCOME


def test_a_balance_sheet_by_labels_and_page_title() -> None:
    rows = [HEADER, ["Inventories", "19", "5", "4"], ["Total assets", "", "50", "40"]]
    assert run(rows, BALANCE_PAGE) is BALANCE


def test_arabic_labels_without_word_boundaries_count() -> None:
    rows = [
        HEADER,
        ["الإيرادات", "", "100", "90"],
        ["تكلفةالمبيعات", "", "(60)", "(50)"],
        ["مجملالربح", "", "40", "40"],
        ["تكاليف التمويل", "", "(5)", "(4)"],
    ]
    assert run(rows, NO_TITLE) is INCOME


def test_a_table_without_a_period_header_is_not_a_statement() -> None:
    rows = [["Danko Maras", "Fawaz", "Prince Naif", ""], ["CFO", "CEO", "Chairman", ""]]
    g = grid(rows)
    result = classify(g, parse_header(g, BALANCE, None), BALANCE_PAGE, INDEX, 0.5)
    assert result.type is None and "no_period_header" in result.evidence


def test_a_notes_table_under_a_note_heading_is_rejected() -> None:
    rows = [
        HEADER,
        ["Inventories", "", "5", "4"],
        ["Total assets", "", "50", "40"],
        ["Revenue", "", "1", "1"],
    ]
    context = TableContext(
        title_types=(BALANCE,),
        cue_types=(),
        heading_texts=("12. Property, plant and equipment",),
        industry_flags=(),
    )
    g = grid(rows)
    note_heading = TableContext(
        title_types=(BALANCE,),
        cue_types=(),
        heading_texts=("Note 12 Property, plant and equipment",),
        industry_flags=(),
    )
    assert classify(g, parse_header(g, BALANCE, None), note_heading, INDEX, 0.5).type is None
    assert classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5).type is BALANCE


def test_industry_flags_pass_through() -> None:
    rows = [HEADER, ["Inventories", "19", "5", "4"], ["Total assets", "", "50", "40"]]
    context = TableContext(
        title_types=(BALANCE,), cue_types=(), heading_texts=(), industry_flags=("likely_bank",)
    )
    g = grid(rows)
    assert classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5).industry_flags == [
        "likely_bank"
    ]


COMPREHENSIVE = StatementType.COMPREHENSIVE_INCOME
TWO_TITLES = TableContext(
    title_types=(INCOME, COMPREHENSIVE), cue_types=(), heading_texts=(), industry_flags=()
)


def test_comprehensive_income_by_its_cue_words() -> None:
    rows = [
        HEADER,
        ["Profit for the year", "", "40", "35"],
        ["Other comprehensive income", "", "3", "2"],
        ["Items that will not be reclassified to profit or loss", "", "1", "1"],
        ["Total comprehensive income for the year", "", "43", "37"],
    ]
    assert run(rows, TWO_TITLES) is COMPREHENSIVE


def test_arabic_comprehensive_income_by_its_cue_words() -> None:
    rows = [
        HEADER,
        ["الدخلالشاملالآخر", "", "3", "2"],
        ["بنودلنيعادتصنيفها", "", "1", "1"],
        ["إجماليالدخلالشامل", "", "43", "37"],
    ]
    assert run(rows, TWO_TITLES) is COMPREHENSIVE


def test_an_income_statement_stays_income_on_a_two_title_page() -> None:
    rows = [
        HEADER,
        ["Revenue", "", "100", "90"],
        ["Cost of sales", "", "(60)", "(50)"],
        ["Gross profit", "", "40", "40"],
        ["Finance costs", "", "(5)", "(4)"],
        ["Profit for the year", "", "35", "36"],
    ]
    assert run(rows, TWO_TITLES) is INCOME


def test_a_tie_goes_to_the_first_title_type() -> None:
    rows = [HEADER, ["Unrelated row", "", "1", "1"]]
    low = 0.3
    first_comprehensive = TableContext(title_types=(COMPREHENSIVE, INCOME))
    first_income = TableContext(title_types=(INCOME, COMPREHENSIVE))
    g = grid(rows)
    layout = parse_header(g, BALANCE, None)
    assert classify(g, layout, first_comprehensive, INDEX, low).type is COMPREHENSIVE
    assert classify(g, layout, first_income, INDEX, low).type is INCOME


def test_a_column_header_line_above_the_table_is_not_a_note_heading() -> None:
    rows = [HEADER, ["Inventories", "", "5", "4"], ["Total assets", "", "50", "40"]]
    g = grid(rows)
    for heading in ("Note 2025 2024", "Notes 31 December 2025 31 December 2024", "إيضاح ٢٠٢٥ ٢٠٢٤"):
        context = TableContext(
            title_types=(BALANCE,), cue_types=(), heading_texts=(heading,), industry_flags=()
        )
        result = classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5)
        assert result.type is BALANCE, heading
