"""Header rows bound to periods (spec 11, Data flow step 5)."""

from fra_core.schemas import StatementType
from fra_ingest.header import is_amount, is_note_ref, parse_header, split_note
from fra_ingest.table_grid import Grid, GridCell


def test_standalone_interim_context_holds_duration_but_keeps_instant_balance_facts() -> None:
    from fra_core.schemas import PeriodKind
    from fra_ingest.header import has_period_context, period_with_context

    assert has_period_context("Interim")
    assert not has_period_context("interimly")
    assert period_with_context("2024", "Interim 30 June 2025", PeriodKind.DURATION) is None
    balance = period_with_context("2024", "Interim 30 June 2025", PeriodKind.INSTANT)
    assert balance is not None and balance.key == "2024-06-30" and balance.months is None


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


def _moved(header: str) -> str:
    layout = parse_header(
        grid([["", header], ["Revenue", "100"]], header_rows=1), BALANCE, "As at 30 June 2025"
    )
    return next(iter(layout.value_cols.values())).end_date.isoformat()


def test_a_header_naming_a_month_or_a_full_date_is_never_re_dated() -> None:
    for header in ("31 Dec 2024", "31 May 2024", "31 ديسمبر 2024", "31/12/2024", "2024-12-31"):
        assert _moved(header) == header_date(header), header


def header_date(header: str) -> str:
    return {"31 May 2024": "2024-05-31"}.get(header, "2024-12-31")


def test_year_only_headers_with_currency_or_restated_markers_move() -> None:
    for header in ("2024", "2024 EGP", "2024 (Restated) EGP", "2024 ألف جنيه"):
        assert _moved(header) == "2024-06-30", header


def test_two_comparatives_keep_their_own_dates_under_a_hint() -> None:
    layout = parse_header(
        grid([["", "30 June 2025", "31 Dec 2024"], ["Cash", "5", "4"]], header_rows=1),
        BALANCE,
        "As at 30 June 2025",
    )
    assert [p.key for p in layout.value_cols.values()] == ["2025-06-30", "2024-12-31"]


def test_a_scale_and_currency_sub_row_joins_the_header() -> None:
    layout = parse_header(
        grid(
            [
                ["", "", "31 December 2025", "31 December 2024"],
                ["", "", "EGP '000", "EGP '000"],
                ["Revenue", "33", "22,064,876", "20,979,512"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0, 1]
    assert "EGP '000" in layout.header_texts[2]


def test_a_restated_sub_row_marks_the_second_column_without_a_duplicate() -> None:
    layout = parse_header(
        grid(
            [
                ["", "2025", "2024"],
                ["", "", "(Restated)"],
                ["Cash", "5", "4"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0, 1]
    assert {p.key: p.restated for p in layout.value_cols.values()} == {
        "2025-12-31": False,
        "2024-12-31": True,
    }
    assert layout.unbound_cols == []


def test_a_same_date_restated_column_is_listed_as_unbound() -> None:
    layout = parse_header(
        grid(
            [["", "31 December 2025", "31 December 2025 (Restated)"], ["Cash", "5", "4"]],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert list(layout.value_cols) == [1]
    assert layout.unbound_cols == [2]
    assert "restated_duplicate:2:2025-12-31" in layout.evidence


def test_a_duplicate_period_column_is_listed_as_unbound() -> None:
    layout = parse_header(
        grid([["", "31 December 2025", "31 December 2025"], ["Cash", "5", "4"]], header_rows=1),
        BALANCE,
        None,
    )
    assert list(layout.value_cols) == [1]
    assert layout.unbound_cols == [2]
    assert "duplicate_period:2:2025-12-31" in layout.evidence


def test_an_amount_whose_digit_groups_ocr_merged_is_never_a_date() -> None:
    assert is_amount("٢٠٧٧ ٦٨٥ ١٨٢") and is_amount("5 175 562 196")
    assert not is_amount("31 December 2025") and not is_amount("2025")
    assert not is_amount("٣١ ديسمبر ٢٠٢٤")


def test_a_row_of_grouped_amounts_is_data_not_a_header() -> None:
    layout = parse_header(
        grid(
            [
                ["٣١ ديسمبر ٢٠٢٤", "٣١ ديسمبر ٢٠٢٣", ""],
                ["٢٠٧٧ ٦٨٥ ١٨٢", "", "إجمالي الأصول"],
                ["١٠", "٢٠", "النقدية"],
            ]
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0]
    assert sorted(p.key for p in layout.value_cols.values()) == ["2023-12-31", "2024-12-31"]


def test_numbered_note_headers_name_the_note_column() -> None:
    for header in ("إيضاح رقم", "Note No."):
        layout = parse_header(
            grid(
                [
                    ["", "31 December 2025", "31 December 2024", header],
                    ["Cash", "1,200", "1,100", "5"],
                    ["Inventories", "300", "250", "6"],
                ]
            ),
            BALANCE,
            None,
        )
        assert layout.note_col == 3, header
        assert sorted(layout.value_cols) == [1, 2] and layout.unbound_cols == []


def test_a_standard_or_level_number_is_not_a_note_reference() -> None:
    assert split_note("Right-of-use assets (IFRS 16)") == ("Right-of-use assets (IFRS 16)", None)
    assert split_note("Lease liabilities IFRS 16") == ("Lease liabilities IFRS 16", None)
    assert split_note("Financial instruments IAS 39") == ("Financial instruments IAS 39", None)
    assert split_note("Fair value - Level 3") == ("Fair value - Level 3", None)
    assert split_note("Expected credit losses - Stage 2") == (
        "Expected credit losses - Stage 2",
        None,
    )
    assert split_note("Trade receivables (12)") == ("Trade receivables", "(12)")
    assert split_note("Profit for the year 5") == ("Profit for the year", "5")


def test_a_year_only_header_under_an_interim_date_line_keeps_the_interim_length() -> None:
    layout = parse_header(
        grid([["", "2025", "2024"], ["Revenue", "100", "90"], ["Cost of sales", "(60)", "(50)"]]),
        INCOME,
        "For the six months ended 30 June 2025",
    )
    assert [p.key for p in layout.value_cols.values()] == ["6M-2025-06-30", "6M-2024-06-30"]
    assert [p.months for p in layout.value_cols.values()] == [6, 6]


def test_ordinary_complete_headers_keep_core_parser_date_hint_formats() -> None:
    for header, caption, expected in (
        (
            "June 30, 2025",
            "For the six months ended June 30, 2025",
            "FY2025",
        ),
        (
            "٣٠ يونيو ٢٠٢٥",
            "عن ستة أشهر المنتهية في ٣٠ يونيو ٢٠٢٥",
            "FY2025",
        ),
    ):
        layout = parse_header(
            grid([["", header], ["Revenue", "100"]], header_rows=1), INCOME, caption
        )
        assert layout.value_cols[1].key == expected, header


def test_ordinary_year_headers_keep_bare_year_date_hint_formats() -> None:
    layout = parse_header(
        grid([["", "2025", "2024"], ["Revenue", "100", "90"]], header_rows=1),
        INCOME,
        "Six months ended June 30, 2025",
    )
    assert [period.key for period in layout.value_cols.values()] == [
        "6M-2025-06-30",
        "6M-2024-06-30",
    ]
