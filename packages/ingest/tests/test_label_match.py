"""Label matching that ignores lost word boundaries (spec 11, Data flow step 3)."""

from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import load_taxonomy
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
