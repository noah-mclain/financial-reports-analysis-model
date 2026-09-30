"""Header rows bound to periods (spec 11, Data flow step 5)."""

from fra_core.schemas import StatementType
from fra_ingest.header import is_amount, is_note_ref, parse_header, split_note
from fra_ingest.table_grid import Grid, GridCell

BALANCE, INCOME = StatementType.BALANCE, StatementType.INCOME


def grid(rows: list[list[str]], header_rows: int = 0) -> Grid:
    cells = tuple(
        GridCell(text=text, row=r, col=c, bbox=None, page_no=1, is_column_header=r < header_rows)
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
        if text
    )
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p1-1.json",
        page_no=1,
        page_width=600,
        num_rows=len(rows),
        num_cols=max(len(r) for r in rows),
        cells=cells,
    )


def test_amounts_dates_years_and_note_references() -> None:
    assert is_amount("26,058,632") and is_amount("(15,177,063)") and is_amount("٢٢,٧٥٠,٣٤٢")
    assert is_amount("5") and is_amount("-")
    assert not is_amount("31 December 2025") and not is_amount("2025") and not is_amount("Notes")
    assert not is_amount("(13-2)") and not is_amount("")
    assert is_note_ref("7") and is_note_ref("(13-2)") and is_note_ref("١٠")
    assert not is_note_ref("26,058,632") and not is_note_ref("2025")


def test_a_trailing_note_reference_is_split_from_the_label() -> None:
    assert split_note("إيرادات ٣٣") == ("إيرادات", "٣٣")
    assert split_note("Zakat for 2025") == ("Zakat for 2025", None)
    assert split_note("Revenue") == ("Revenue", None)


def test_an_english_balance_sheet_header() -> None:
    layout = parse_header(
        grid(
            [
                ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
                ["ASSETS", "", "", ""],
                ["Property, Plant and Equipment", "7", "26,058,632", "22,750,342"],
                ["Investments", "12", "-", "3,256"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0]
    assert (layout.label_col, layout.note_col) == (0, 1)
    assert {c: p.key for c, p in layout.value_cols.items()} == {2: "2025-12-31", 3: "2024-12-31"}
    assert layout.unbound_cols == []


def test_a_two_row_header_is_joined_per_column() -> None:
    layout = parse_header(
        grid(
            [
                ["", "", "For the year ended", "For the year ended"],
                ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
                ["Revenue", "33", "22,064,876", "20,979,512"],
            ],
            header_rows=1,
        ),
        INCOME,
        None,
    )
    assert layout.header_rows == [0, 1]
    assert {c: p.key for c, p in layout.value_cols.items()} == {2: "FY2025", 3: "FY2024"}


def test_a_mirrored_arabic_table_finds_its_label_column_on_the_right() -> None:
    layout = parse_header(
        grid(
            [
                ["31 ديسمبر 2024 مبآلاف X", "31 ديسمبر 2025 مبآلاف X", "إيضاحات", ""],
                ["22,750,342", "26,058,632", "7", "ممتلكاتوآلاتومعدات"],
                ["525,391", "492,514", "8", "مصاريفمدفوعةمقدما"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert (layout.label_col, layout.note_col) == (3, 2)
    assert {c: p.key for c, p in layout.value_cols.items()} == {0: "2024-12-31", 1: "2025-12-31"}


def test_a_year_only_header_takes_day_and_month_from_the_date_line() -> None:
    layout = parse_header(
        grid([["", "2025", "2024"], ["Revenue", "100", "90"]], header_rows=1),
        INCOME,
        "For the year ended 30 June 2025",
    )
    assert {p.end_date.isoformat() for p in layout.value_cols.values()} == {
        "2025-06-30",
        "2024-06-30",
    }


def test_a_value_column_without_a_date_is_unbound() -> None:
    layout = parse_header(
        grid(
            [["", "31 December 2025", "بآلاف X"], ["Revenue", "100", "90"]],
            header_rows=1,
        ),
        INCOME,
        None,
    )
    assert list(layout.value_cols) == [1]
    assert layout.unbound_cols == [2]
    assert "period_unbound:2" in layout.evidence


def test_a_restated_column_keeps_its_marker() -> None:
    layout = parse_header(
        grid([["", "2025 EGP", "2024 (Restated) EGP"], ["Cash", "5", "4"]], header_rows=1),
        BALANCE,
        None,
    )
    assert [p.restated for p in layout.value_cols.values()] == [False, True]


def test_a_note_column_without_a_header_is_found_next_to_the_labels() -> None:
    layout = parse_header(
        grid(
            [
                ["", "", "2025", "2024"],
                ["Property, plant and equipment", "(14)", "5 175 562 196", "3 886 899 018"],
                ["Biological assets", "(16) - (17-1)", "574 493 531", "445 704 631"],
                ["Goodwill", "(35)", "97 092 890", "97 092 890"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.note_col == 1
    assert sorted(layout.value_cols) == [2, 3]


def test_small_values_stay_value_columns() -> None:
    layout = parse_header(
        grid(
            [
                ["", "Notes", "2025", "2024"],
                ["Inventories", "19", "10", "8"],
                ["Cash", "21", "5", "4"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.note_col == 1 and sorted(layout.value_cols) == [2, 3]


def test_a_section_row_above_the_first_amount_is_not_a_header() -> None:
    layout = parse_header(
        grid(
            [
                ["", "Notes", "31 December 2025", "31 December 2024"],
                ["Non-Current Assets", "", "", ""],
                ["Property, plant and equipment", "7", "26,058,632", "22,750,342"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0]


def test_a_table_with_no_amounts_has_no_value_columns() -> None:
    layout = parse_header(
        grid([["Danko Maras", "Fawaz Bin Mohammed", "Prince Naif"], ["CFO", "CEO", "Chairman"]]),
        BALANCE,
        None,
    )
    assert layout.value_cols == {} and layout.unbound_cols == []
