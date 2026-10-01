"""Visual-order Arabic repair (spec 11, Data flow step 3; measured on Almarai AR)."""

from fra_core.numbers import parse_number
from fra_core.periods import parse_period
from fra_ingest.table_grid import Grid, GridCell
from fra_ingest.visual_order import (
    is_letter_spaced,
    join_spaced_letters,
    repair_grid,
    repair_text,
    restore_digits,
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
