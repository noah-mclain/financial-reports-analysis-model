"""Phrase matching on normalized text, and reading visual-order Arabic."""

from fra_ingest.text_match import PhraseIndex, reading_variants


def test_phrases_match_whole_words_after_normalization() -> None:
    index = PhraseIndex.build({"balance": ["Statement of Financial Position", "إجمالي الأصول"]})
    assert index.find(["CONSOLIDATED STATEMENT OF FINANCIAL POSITION"]) == {
        "statement of financial position": frozenset({"balance"})
    }
    # Keys are canonical forms: normalize_label, then lam-alef folded.
    assert index.find(["اجمالي الأصول"])  # hamza forms fold together
    assert not index.find(["statement of financial positions"])


def test_the_longest_phrase_wins_and_consumes_its_words() -> None:
    index = PhraseIndex.build(
        {"income": ["قائمة الدخل"], "comprehensive_income": ["قائمة الدخل الشامل"]}
    )
    assert set(index.find(["قائمة الدخل الشامل الموحدة"]).values()) == {
        frozenset({"comprehensive_income"})
    }


def test_a_phrase_listed_under_two_groups_reports_both() -> None:
    combined = "statement of profit or loss and other comprehensive income"
    index = PhraseIndex.build({"income": [combined], "comprehensive_income": [combined]})
    assert index.find([combined]) == {combined: frozenset({"income", "comprehensive_income"})}


def test_visual_arabic_is_read_in_both_word_orders() -> None:
    variants = reading_variants("ةدحوملا يلاملا زكرملا ةمئاق\nتاحاضيإ ۳۱ ربمسيد م٢٠٢٥", True)
    assert "قائمة المركز المالي الموحدة" in variants[1]
    assert "إيضاحات ۳۱ ديسمبر م٢٠٢٥" in variants[0]


def test_arabic_lines_are_also_read_with_their_words_reversed() -> None:
    variants = reading_variants("المالي المركز قائمة\n2025 2024", False)
    assert variants[0] == "المالي المركز قائمة\n2025 2024"
    assert variants[1].startswith("قائمة المركز المالي")
    index = PhraseIndex.build({"balance": ["قائمة المركز المالي"]})
    assert index.find(variants)


def test_text_without_arabic_has_one_variant() -> None:
    assert reading_variants("Statement of financial position", False) == [
        "Statement of financial position"
    ]


def test_swapped_lam_alef_ligatures_still_match() -> None:
    index = PhraseIndex.build({"comprehensive_income": ["الدخل الشامل الآخر"]})
    assert index.find(["الدخل الشامل اآلخر"])  # ligature extracted as alef-madda, lam
