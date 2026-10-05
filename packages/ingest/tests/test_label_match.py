"""Label matching that ignores lost word boundaries (spec 11, Data flow step 3)."""

from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import CanonicalItem, load_taxonomy
from fra_ingest.label_match import LabelIndex, has_subtotal_cue, squash

BALANCE = StatementType.BALANCE


def test_squash_drops_spaces_after_normalizing() -> None:
    assert squash("Total  Assets") == "totalassets"
    assert squash("إجمالي الموجودات") == squash("اجماليالموجودات")


def test_labels_match_with_or_without_word_boundaries() -> None:
    index = LabelIndex(load_taxonomy())
    for label in (
        "Total Assets",
        "إجمالي الموجودات",
        "إجماليالموجودات",
        "إ ج م ا ل ي ا ل م و ج و د ا ت",
    ):
        item = index.match(label, BALANCE)
        assert item is not None and item.id == "total_assets", label
    current = index.match("Total current assets", BALANCE)
    assert current is None or current.id != "total_assets"
    assert index.match("", BALANCE) is None


def test_hits_count_matched_labels() -> None:
    index = LabelIndex(load_taxonomy())
    assert index.hits(["Revenue", "Cost of sales", "Chairman"], StatementType.INCOME) == 2


def test_subtotal_cues_in_both_languages() -> None:
    assert has_subtotal_cue("Total current assets")
    assert has_subtotal_cue("إجمالي المطلوبات")
    assert has_subtotal_cue("مجموع حقوق الملكية")
    assert has_subtotal_cue("إجماليالموجودات")
    assert not has_subtotal_cue("Inventories")
    assert not has_subtotal_cue("مجموعة الشركات")
    assert not has_subtotal_cue("مجموعةالشركات")
    assert not has_subtotal_cue("Totality of assets")
    assert has_subtotal_cue("مجموعالموجودات")


def test_the_closing_total_matches_with_equity_named_first() -> None:
    index = LabelIndex(load_taxonomy())
    for label in ("Total equity and total liabilities", "إجمالي حقوق الملكية والالتزامات"):
        item = index.match(label, BALANCE)
        assert item is not None and item.id == "total_liabilities_and_equity", label


def ids(items: tuple[CanonicalItem, ...]) -> list[str]:
    return [i.id for i in items]


def test_an_arabic_section_heading_is_not_an_alias() -> None:
    index = LabelIndex(load_taxonomy())
    assert index.match("الموجودات المتداولة", BALANCE) is None
    assert ids(index.headings("Current assets", BALANCE)) == ["total_current_assets"]
    assert ids(index.headings("الموجودات المتداولة", BALANCE)) == ["total_current_assets"]
    assert index.headings("Total current assets", BALANCE) == ()


def test_a_heading_run_into_a_row_is_read_when_the_rest_is_an_alias() -> None:
    index = LabelIndex(load_taxonomy())
    found = index.run_in_heading("الموجوداتالمتداولةمخزون", BALANCE)
    assert ids(found) == ["total_current_assets"]
    assert ids(index.run_in_heading("Current assets Inventories", BALANCE)) == [
        "total_current_assets"
    ]


def test_a_label_that_only_contains_or_starts_with_a_heading_is_not_a_run_in() -> None:
    index = LabelIndex(load_taxonomy())
    # The rest is not an alias: a row of its own, not a heading and a row.
    assert index.run_in_heading("Non-current assets held for sale", BALANCE) == ()
    assert index.run_in_heading("Net current assets", BALANCE) == ()
    # The heading is not at the start.
    assert index.run_in_heading("Inventories current assets", BALANCE) == ()
    # No rest at all is an exact heading, not a run-in.
    assert index.run_in_heading("Current assets", BALANCE) == ()


def test_a_label_that_starts_an_alias_or_that_an_alias_starts_is_a_prefix_match() -> None:
    index = LabelIndex(load_taxonomy())
    longer = index.prefix_matches("Total assets as restated", BALANCE)
    assert "total_assets" in {i.id for i in longer}
    shorter = index.prefix_matches("Total asset", BALANCE)
    assert "total_assets" in {i.id for i in shorter}


def test_a_prefix_match_needs_a_few_letters_and_never_repeats_an_exact_match() -> None:
    index = LabelIndex(load_taxonomy())
    assert index.prefix_matches("to", BALANCE) == ()
    assert index.prefix_matches("", BALANCE) == ()
    assert index.prefix_matches("Total assets", BALANCE) == ()
    assert index.prefix_matches("Zzzzzz qqqq", BALANCE) == ()


def test_labels_in_presentation_forms_match_their_alias() -> None:
    # Some text layers store Arabic as presentation forms, and NFKC turns the joined yeh and
    # kaf into the Persian letters (U+06CC, U+06A9) instead of the Arabic ones.
    index = LabelIndex(load_taxonomy())
    cost = index.match("ﺗﻛﻠﻔﺔ اﻹﯾرادات", StatementType.INCOME)
    assert cost is not None and cost.id == "cost_of_revenue"
    assert squash("ﯾ") == squash("ي")
    assert squash("ک") == squash("ك")


def test_section_totals_written_with_the_word_majmoo_over_assets_and_obligations_match() -> None:
    # A fit document (Leaf Global, balance sheet) prints its totals as مجموع over الأصول,
    # الالتزامات or حقوق الملكية; the lexicon had that word only before الموجودات and المطلوبات.
    # The bare totals (مجموع الأصول, مجموع الالتزامات, مجموع حقوق الملكية) are not aliases: with
    # them a highlights table of Almarai's Arabic annual report outscores its balance sheet.
    index = LabelIndex(load_taxonomy())
    expected = {
        "مجموع الأصول المتداولة": "total_current_assets",
        "مجموع الأصول غير المتداولة": "total_non_current_assets",
        "مجموع الالتزامات المتداولة": "total_current_liabilities",
        "مجموع الالتزامات غير المتداولة": "total_non_current_liabilities",
        "مجموع حقوق الملكية والالتزامات": "total_liabilities_and_equity",
    }
    for label, item_id in expected.items():
        item = index.match(label, BALANCE)
        assert item is not None and item.id == item_id, label
    assert index.match("مجموع الأصول", BALANCE) is None
