"""Which pages hold which statement, from their text alone."""

import pytest
from support import numbers_block, text_page

from fra_core.schemas import StatementType
from fra_ingest.locate import count_numeric_tokens, load_title_book, score_page

BOOK = load_title_book()
BALANCE = StatementType.BALANCE
INCOME = StatementType.INCOME
COMPREHENSIVE = StatementType.COMPREHENSIVE_INCOME

EN_HEADER = "Consolidated Statement of Financial Position\nNotes 31 December 2025 31 December 2024\nSAR '000 SAR '000"


def test_an_english_balance_sheet_is_a_candidate() -> None:
    score = score_page(
        text_page(EN_HEADER, "ASSETS\n" + numbers_block() + "\nTotal assets 9,999,999"), BOOK
    )
    assert score.is_candidate(BALANCE)
    assert not score.is_candidate(INCOME)
    assert score.structure == ["period", "note_column", "scale_or_currency"]


def test_an_egyptian_arabic_balance_sheet_is_a_candidate() -> None:
    header = "قائمة المركز المالي المجمعة\nفي ٣١ ديسمبر ٢٠٢٥\nإيضاح ٢٠٢٥ ٢٠٢٤\n(جميع المبالغ بالألف جنيه مصري)"
    body = "\n".join(f"بند {i} ٨ ٨٦٤ ٣٨٣ ٢٤٤ ٧ ٩١٢ ٠٠٠ ١١١" for i in range(20))
    assert score_page(text_page(header, body + "\nإجمالي الأصول"), BOOK).is_candidate(BALANCE)


def test_a_gulf_arabic_balance_sheet_is_a_candidate() -> None:
    header = "قائمة المركز المالي الموحدة\nإيضاحات ٣١ ديسمبر ٢٠٢٥م ٣١ ديسمبر ٢٠٢٤م\nبالآلاف"
    body = "الموجودات\n" + numbers_block(label="بند") + "\nمجموع الموجودات"
    assert score_page(text_page(header, body), BOOK).is_candidate(BALANCE)


def test_reversed_arabic_from_a_visual_text_layer_is_still_found() -> None:
    header = "ةدحوملا يلاملا زكرملا ةمئاق\nتاحاضيإ ۳۱ ربمسيد م٢٠٢٥ ۳۱ ربمسيد م۲۰۲٤\nفالآب X"
    score = score_page(text_page(header, numbers_block(label="دنب"), visual=True), BOOK)
    assert score.is_candidate(BALANCE)


def test_an_auditors_report_quoting_the_titles_is_not_a_candidate() -> None:
    header = "Independent auditor's report to the shareholders of Example Company"
    body = (
        "We have audited the consolidated statement of financial position as at 31 December 2025 "
        "and the consolidated statement of profit or loss for the year then ended."
    )
    score = score_page(text_page(header, body), BOOK)
    assert "auditor_report" in score.negatives
    assert not any(score.is_candidate(t) for t in StatementType)


def test_an_auditors_report_page_with_titles_in_its_header_is_not_a_candidate() -> None:
    header = (
        "the consolidated statement of financial position as at 31 December 2025 and the "
        "consolidated statement of profit or loss for the year then ended"
    )
    score = score_page(
        text_page(header, "In our opinion the financial statements present fairly."), BOOK
    )
    assert not any(score.is_candidate(t) for t in StatementType)


def test_a_contents_page_is_not_a_candidate() -> None:
    header = "Contents\nConsolidated statement of financial position 5\nConsolidated statement of profit or loss 6"
    assert not score_page(text_page(header), BOOK).is_candidate(BALANCE)


def test_a_notes_page_with_a_table_is_not_a_candidate() -> None:
    header = "Notes to the consolidated financial statements\nFor the year ended 31 December 2025\nSAR '000 2025 2024"
    body = "Property, plant and equipment\nTotal assets\n" + numbers_block()
    assert not score_page(text_page(header, body), BOOK).is_candidate(BALANCE)


def test_a_continuation_page_is_marked() -> None:
    header = "Consolidated statement of financial position (continued)\n2025 2024"
    assert score_page(text_page(header, numbers_block()), BOOK).continuation


def test_a_garbled_title_is_rescued_by_cues_and_structure() -> None:
    header = "Consolidated statment of flnancial pos1tion\nNote 31 December 2025 2024\nEGP '000"
    body = numbers_block() + "\nTotal assets 12,345,678 11,234,567"
    score = score_page(text_page(header, body), BOOK)
    assert score.title_types == []
    assert score.is_candidate(BALANCE)


def test_a_page_with_no_title_at_all_is_found_by_structure() -> None:
    header = "Note 2025 2024\nEGP thousands"
    body = (
        numbers_block()
        + "\nTotal current liabilities 1,234,567 1,111,111\nTotal assets 9,876,543 9,000,000"
    )
    assert score_page(text_page(header, body), BOOK).is_candidate(BALANCE)


def test_a_combined_statement_counts_for_income_and_comprehensive_income() -> None:
    header = "Statement of profit or loss and other comprehensive income\nNote 2025 2024\nEGP '000"
    score = score_page(text_page(header, numbers_block()), BOOK)
    assert score.is_candidate(INCOME)
    assert score.is_candidate(COMPREHENSIVE)


def test_a_separate_comprehensive_income_statement_is_not_income() -> None:
    header = "قائمة الدخل الشامل المستقلة\nإيضاح ٢٠٢٥ ٢٠٢٤\nبالألف جنيه مصري"
    score = score_page(text_page(header, numbers_block(label="بند")), BOOK)
    assert score.is_candidate(COMPREHENSIVE)
    assert INCOME not in score.title_types


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("٨ ٨٦٤ ٣٨٣ ٢٤٤ ٧ ٩١٢ ٠٠٠ ١١١", 2),
        ("22,064,876 20,979,51233", 2),
        ("31 December 2025 2024", 0),
        ("(509,663) (538,024)", 2),
        ("12 45 7", 0),
        ("١٠,٠٠٠,٠٠٠ ٢,٩٦٦,١٦٥", 2),
    ],
)
def test_numeric_tokens(text: str, expected: int) -> None:
    assert count_numeric_tokens(text) == expected


# Tuning on the train pool, 2026-09-28. Each case is a failure class measured there.


def test_a_currency_line_counts_as_structure() -> None:
    # Al Dawaa (OCR): title garbled, no scale line, but the currency is printed.
    header = "ائمة الربح أو الخسارة\nللفترة المنتهية في ٣١ مارس ٢٠٢٣م\nريال سعودي"
    body = "ايرادات ١,٢٩٤,٨٢١,٠٠٢\n" + numbers_block(rows=12, label="بند") + "\nإجمالي الربح"
    score = score_page(text_page(header, body), BOOK)
    assert "scale_or_currency" in score.structure
    assert score.is_candidate(INCOME)


def test_a_comprehensive_income_statement_with_revenue_is_also_income() -> None:
    # Herfy: the income statement is titled only "statement of comprehensive income".
    header = "قائمة الدخل الشامل\nللسنة المنتهية في 31 ديسمبر 2022\nإيضاح 2022 2021"
    body = (
        "الإيرادات 1,319,092,436 1,243,838,271\nتكلفة الإيرادات (901,263,698) (936,834,428)\n"
        + numbers_block(label="بند")
    )
    score = score_page(text_page(header, body), BOOK)
    assert score.is_candidate(COMPREHENSIVE)
    assert score.is_candidate(INCOME)


def test_a_comprehensive_income_statement_without_revenue_is_not_income() -> None:
    header = "Statement of comprehensive income\nNote 2025 2024\nSAR '000"
    body = "Profit for the year 1,234 1,100\nOther comprehensive income\n" + numbers_block()
    assert INCOME not in score_page(text_page(header, body), BOOK).title_types


def test_a_title_whose_first_word_ocr_garbled_is_still_a_title() -> None:
    # Herfy p7: "قائمة" read as "خاامة".
    header = (
        "شركة هرفى للخدمات الغذائية\nخاامة المركز المالى\nكما في 31 ديسمير 2022\nإيضاح 2022 2021"
    )
    score = score_page(text_page(header, numbers_block(label="بند")), BOOK)
    assert BALANCE in score.title_types


def test_notes_headers_with_a_qualifier_are_still_notes() -> None:
    # Juhayna: "Notes to the Standalone Financial Statements", then a note titled
    # "Income Statement" over a table of numbers.
    header = (
        "Juhayna Food Industries Company (S.A.E.)\n"
        "Notes to the Standalone Financial Statements for the Financial Year Ended 31 December 2025\n"
        "Income Statement\nAmount in EGP 2025 2024"
    )
    score = score_page(text_page(header, numbers_block()), BOOK)
    assert "notes" in score.negatives
    assert not score.is_candidate(INCOME)


def test_arabic_notes_headers_with_a_qualifier_are_still_notes() -> None:
    header = "إيضاحات حول القوائم المالية المستقلة المجمعة\nللسنة المنتهية في 31 ديسمبر 2025\nقائمة الدخل"
    assert "notes" in score_page(text_page(header, numbers_block(label="بند")), BOOK).negatives
