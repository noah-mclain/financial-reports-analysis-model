"""Visual-order Arabic repair (spec 11, Data flow step 3; measured on Almarai AR)."""

from fra_core.numbers import parse_number
from fra_core.periods import parse_period
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_grid import Grid, GridCell
from fra_ingest.visual_order import (
    _matches,
    _reversed_words,
    is_letter_spaced,
    is_reorderable,
    join_spaced_letters,
    join_split_year,
    repair_grid,
    repair_text,
    restore_digits,
    restore_word_order,
)


def test_a_reversed_number_is_restored() -> None:
    assert parse_number(restore_digits("٢٤٣,٠٥٧,٢٢")).value == 22750342
    assert restore_digits("٠١") == "١٠"


def test_a_reversed_negative_with_spaced_brackets_is_restored() -> None:
    restored = restore_digits(") ٣٦٠,٧٧١,٥١ (")
    assert parse_number(restored).value == -15177063


def test_numbers_inside_text_are_restored_token_by_token() -> None:
    assert restore_digits("إ ي ر ا د ا ت ٣٣") == "إ ي ر ا د ا ت ٣٣"
    assert restore_digits("۱۳ د ي س م ب ر ٥٢٠٢") == "۳۱ د ي س م ب ر ٢٠٢٥"


def test_percentages_are_restored_spaced_or_not() -> None:
    assert restore_digits("٩٫٣١ %") == "١٣٫٩ %"
    assert restore_digits("٩٫٣١٪") == "١٣٫٩٪"


def test_ranges_keep_their_token_order() -> None:
    assert restore_digits("٢ - ٠١ س ن و ا ت") == "٢ - ١٠ س ن و ا ت"
    assert restore_digits("٢ - ٠١") == "٢ - ١٠"


def test_an_unspaced_mirrored_negative_is_restored() -> None:
    assert restore_digits(")٣٢١(") == "(١٢٣)"
    assert parse_number(restore_digits(")٣٢١(")).value == -123


def test_latin_digits_are_untouched() -> None:
    assert restore_digits("IFRS 16") == "IFRS 16"
    assert restore_digits("2025") == "2025"


def test_ordinary_arabic_with_a_separate_waw_is_not_joined() -> None:
    assert join_spaced_letters("ممتلكات و آلات و معدات") == "ممتلكات و آلات و معدات"


def test_a_dash_is_left_alone() -> None:
    assert restore_digits("-") == "-"


def test_spaced_letters_are_joined_and_other_tokens_kept_apart() -> None:
    assert is_letter_spaced("إ ي ض ا ح ا ت")
    assert join_spaced_letters("إ ي ض ا ح ا ت") == "إيضاحات"
    assert join_spaced_letters("۳۱ د ي س م ب ر ٢٠٢٥ م ب آ لا ف X") == "۳۱ ديسمبر ٢٠٢٥ مبآلاف X"
    assert join_spaced_letters("ذ م م م د ي ن ة م ق د ًم ا") == "ذمممدينةمقدًما"
    assert not is_letter_spaced("Property, Plant and Equipment")
    assert join_spaced_letters("شركة جهينة للصناعات") == "شركة جهينة للصناعات"


def test_the_almarai_header_parses_after_repair() -> None:
    text = repair_text("۱۳ د ي س م ب ر ٥٢٠٢ م ب آ لا ف X", visual=True)
    period = parse_period(text)
    assert period is not None and period.key == "2025-12-31"


def test_digits_are_only_reversed_on_visual_pages() -> None:
    assert repair_text("٢٢,٧٥٠,٣٤٢", visual=False) == "٢٢,٧٥٠,٣٤٢"


def test_a_repaired_grid_flags_what_changed() -> None:
    grid = Grid(
        table_ref="#/tables/0",
        docling_path="docling/p156-156.json",
        page_no=156,
        page_width=793.7,
        num_rows=1,
        num_cols=2,
        cells=(
            GridCell(text="٢٤٣,٠٥٧,٢٢", row=0, col=0, bbox=None, page_no=156),
            GridCell(text="م و ج و د ا ت ح ي و ي ة", row=0, col=1, bbox=None, page_no=156),
        ),
    )
    repaired = repair_grid(grid, visual=True)
    number, label = repaired.cells
    assert number.text == "٢٢,٧٥٠,٣٤٢" and number.flags == ("digits_reversed",)
    assert label.text == "موجوداتحيوية" and label.flags == ("letters_spaced",)
    assert repair_grid(grid, visual=False).cells[0].flags == ()


def _label_grid(*labels: str) -> Grid:
    cells = tuple(
        GridCell(text=text, row=row, col=0, bbox=None, page_no=4) for row, text in enumerate(labels)
    )
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p4-4.json",
        page_no=4,
        page_width=595.0,
        num_rows=len(labels),
        num_cols=1,
        cells=cells,
    )


def _texts(grid: Grid) -> list[str]:
    return [cell.text for cell in grid.cells]


INDEX = LabelIndex(load_taxonomy())
# Arabic table cells that docling returns with the words of each cell reversed (measured on
# the Arabic digital fit documents): the letters read correctly, the word order does not. Each
# matches the lexicon only with its words reversed, and each LOGICAL cell only as printed
# (``test_the_fixture_cells_match_the_way_the_tests_say``): a threshold test is only as good as
# its cells.
REVERSED = ("الإيرادات تكلفة", "الربح إجمالي", "الموجودات مجموع", "مدينة ذمم", "المال رأس")
LOGICAL = ("تكلفة الإيرادات", "إجمالي الربح", "مجموع الموجودات", "ذمم مدينة", "رأس المال")


def _grid_of(backwards: int, forwards: int) -> Grid:
    return _label_grid(*REVERSED[:backwards], *LOGICAL[:forwards])


def _reversed_in(grid: Grid) -> bool:
    return "words_reversed" in grid.flags


def test_the_fixture_cells_match_the_way_the_tests_say() -> None:
    for text in REVERSED:
        assert not _matches(text, INDEX) and _matches(_reversed_words(text), INDEX), text
    for text in LOGICAL:
        assert _matches(text, INDEX) and not _matches(_reversed_words(text), INDEX), text


def test_a_table_whose_labels_match_only_reversed_is_put_in_reading_order() -> None:
    repaired = restore_word_order(_label_grid(*REVERSED[:4]), INDEX)
    assert _texts(repaired) == list(LOGICAL[:4])
    assert repaired.flags == ("words_reversed",)


def test_a_table_in_reading_order_is_left_alone() -> None:
    grid = _label_grid(*LOGICAL)
    assert restore_word_order(grid, INDEX) == grid


def test_exactly_the_least_evidence_reverses_a_table() -> None:
    assert _reversed_in(restore_word_order(_grid_of(3, 0), INDEX))
    two = restore_word_order(_grid_of(2, 0), INDEX)
    assert _texts(two) == list(REVERSED[:2])


def test_reversed_cells_must_outnumber_forward_ones_more_than_two_to_one() -> None:
    four_to_two = restore_word_order(_grid_of(4, 2), INDEX)
    assert _texts(four_to_two) == [*REVERSED[:4], *LOGICAL[:2]]
    five_to_two = restore_word_order(_grid_of(5, 2), INDEX)
    assert _texts(five_to_two) == [*LOGICAL[:5], *LOGICAL[:2]]


def test_a_cell_already_in_reading_order_is_not_reversed_with_its_table() -> None:
    repaired = restore_word_order(_grid_of(3, 1), INDEX)
    assert _texts(repaired) == [*LOGICAL[:3], LOGICAL[0]]
    assert repaired.flags == ("words_reversed",)


def test_reversed_evidence_the_rule_does_not_act_on_is_flagged() -> None:
    for backwards, forwards in ((1, 0), (2, 0), (4, 2)):
        grid = restore_word_order(_grid_of(backwards, forwards), INDEX)
        assert grid.flags == ("word_order_uncertain",), (backwards, forwards)
    assert restore_word_order(_label_grid(*LOGICAL), INDEX).flags == ()
    assert restore_word_order(_label_grid("إيضاح عام", "قائمة"), INDEX).flags == ()


def test_a_cell_with_digits_in_a_reversed_table_stays_and_the_table_is_flagged() -> None:
    note = "(٥ إيضاح) مدينة ذمم"
    repaired = restore_word_order(_label_grid(*REVERSED[:3], note), INDEX)
    assert _texts(repaired)[3] == note
    assert repaired.flags == ("words_reversed", "word_order_uncertain")


def test_a_column_heading_with_digits_does_not_make_a_reversed_table_uncertain() -> None:
    grid = _label_grid(*REVERSED[:3])
    heading = GridCell(
        text="31 مارس 2026 م", row=9, col=0, bbox=None, page_no=4, is_column_header=True
    )
    repaired = restore_word_order(grid.model_copy(update={"cells": (*grid.cells, heading)}), INDEX)
    assert repaired.flags == ("words_reversed",)


def test_cells_with_digits_and_single_words_are_not_reordered() -> None:
    grid = _label_grid(*REVERSED[:3], "31 مارس 2026", "الموجودات", "Total assets")
    assert _texts(restore_word_order(grid, INDEX))[3:] == [
        "31 مارس 2026",
        "الموجودات",
        "Total assets",
    ]


def test_a_latin_run_inside_a_reversed_cell_keeps_its_order() -> None:
    assert _reversed_words("Total assets الموجودات مجموع") == "مجموع الموجودات Total assets"
    assert _reversed_words("Net IFRS الربح") == "الربح Net IFRS"
    assert _reversed_words("الإيرادات تكلفة") == "تكلفة الإيرادات"


def test_cells_in_presentation_forms_are_reordered_too() -> None:
    reversed_cells = ("اﻹﯾرادات ﺗﻛﻠﻔﺔ", "اﻟرﺑﺢ ﻣﺟﻣل", "اﻟﻣوﺟودات ﻣﺟﻣوع")
    repaired = restore_word_order(_label_grid(*reversed_cells), INDEX)
    assert _texts(repaired) == [" ".join(reversed(c.split())) for c in reversed_cells]


def test_a_cell_written_wholly_in_presentation_forms_is_a_candidate() -> None:
    isolated = "ﺍﻝﺭ ﺏﺡ"  # two words, no character in the Arabic block
    assert is_reorderable(isolated)
    assert not is_reorderable("ﺍﻝﺭ")


def test_a_year_the_text_layer_split_after_its_last_digit_is_joined() -> None:
    # Measured on two Arabic digital fit documents: "2025" comes back as "5 202" in a column
    # heading, so the heading names no date.
    assert join_split_year("في كما 31 ديسمبر 5 202 م") == "في كما 31 ديسمبر 2025 م"
    assert join_split_year("٣١ مارس ٦ ٢٠٢ م") == "٣١ مارس ٢٠٢٦ م"
    period = parse_period(join_split_year("31 مارس 6 202 م"))
    assert period is not None and period.key == "2026-03-31"
    for kept in ("31 مارس 2026 م", "5 2024 م", "12 202 م", "5 مارس 2026", "1,5 202"):
        assert join_split_year(kept) == kept, kept


def test_a_split_year_is_joined_in_column_headings_only() -> None:
    def cell(text: str, header: bool) -> GridCell:
        return GridCell(text=text, row=0, col=0, bbox=None, page_no=4, is_column_header=header)

    grid = Grid(
        table_ref="#/tables/0",
        docling_path="docling/p4-4.json",
        page_no=4,
        page_width=595.0,
        num_rows=1,
        num_cols=1,
        cells=(cell("31 مارس 6 202 م", True), cell("5 202", False)),
    )
    heading, body = repair_grid(grid, visual=False).cells
    assert heading.text == "31 مارس 2026 م" and heading.flags == ("year_joined",)
    assert body.text == "5 202" and body.flags == ()
