"""The failure classes of unmapped and ambiguous rows, and their summary over fit documents."""

import pytest
from harness.unmapped import FAILURE_CLASSES, failure_class, flagged_rows, summarize
from test_mapping_harness import INDEX, item, statement

from fra_core.schemas import StatementType

BALANCE = StatementType.BALANCE


def flagged(label: str, evidence: str, flag: str = "unmapped"):  # type: ignore[no-untyped-def]
    return item(1, label, "10", flag=flag, evidence=evidence)


@pytest.mark.parametrize(
    ("label", "evidence", "flag", "expected"),
    [
        ("Some odd row", "no_alias_no_anchor", "unmapped", "no_alias_no_anchor"),
        ("   ", "no_alias_no_anchor", "unmapped", "blank_label"),
        ("(12) - 3.", "no_alias_no_anchor", "unmapped", "no_letters"),
        ("Total assets as restated", "no_alias_no_anchor", "unmapped", "near_alias_prefix"),
        ("Cash", "alias_multiple:a|b", "ambiguous", "alias_multiple"),
        ("Equity", "anchor_conflict:alias=a|section_heading=b", "ambiguous", "anchor_conflict"),
        (
            "Total assets",
            "alias_total_unconfirmed:total_assets",
            "ambiguous",
            "alias_total_unconfirmed",
        ),
        ("Total assets", "repeat_of:r1:total_assets", "ambiguous", "repeat_of"),
        ("Total assets", "duplicate:total_assets", "ambiguous", "duplicate"),
    ],
)
def test_each_flagged_row_has_one_class_decided_from_the_mapper_evidence_and_the_label(
    label: str, evidence: str, flag: str, expected: str
) -> None:
    assert failure_class(flagged(label, evidence, flag), BALANCE, INDEX) == expected
    assert expected in FAILURE_CLASSES


def test_an_evidence_code_the_classes_do_not_know_is_an_error_that_names_it() -> None:
    with pytest.raises(ValueError, match="brand_new_code"):
        failure_class(flagged("x", "brand_new_code:1"), BALANCE, INDEX)


def test_a_flag_with_no_evidence_is_an_error() -> None:
    with pytest.raises(ValueError, match="no evidence"):
        failure_class(item(1, "x", "1", flag="unmapped"), BALANCE, INDEX)


def test_only_valued_flagged_rows_are_listed_and_mapped_rows_are_not() -> None:
    mapped = item(2, "Total assets", "5", canonical="total_assets", evidence="alias")
    st = statement([flagged("Mystery", "no_alias_no_anchor"), mapped])
    rows = flagged_rows(st, INDEX)
    assert rows == [{"statement": "balance", "label": "Mystery", "class": "no_alias_no_anchor"}]


def rows(*labels_classes: tuple[str, str]) -> list[dict[str, str]]:
    return [{"statement": "balance", "label": lab, "class": c} for lab, c in labels_classes]


def test_summary_counts_per_class_language_and_period_kind_with_top_labels() -> None:
    documents = [
        {
            "id": "a",
            "language": "en",
            "period": "annual",
            "flagged": rows(("Mystery", "no_alias_no_anchor"), ("MYSTERY ", "no_alias_no_anchor")),
        },
        {
            "id": "b",
            "language": "ar",
            "period": "interim",
            "flagged": rows(("Mystery", "no_alias_no_anchor"), ("Total assets", "duplicate")),
        },
        {"id": "c", "language": "en", "period": "interim", "flagged": []},
    ]
    result = summarize(documents, top=1)
    mystery = result["classes"]["no_alias_no_anchor"]
    assert mystery["count"] == 3
    assert mystery["by_language"] == {"en": 2, "ar": 1}
    assert mystery["by_period_kind"] == {"annual": 2, "interim": 1}
    assert mystery["top_labels"] == [["mystery", 3]]
    assert result["classes"]["duplicate"]["count"] == 1
    assert result["documents"] == {
        "total": 3,
        "by_language": {"en": 2, "ar": 1},
        "by_period_kind": {"annual": 1, "interim": 2},
        "with_flagged_rows": 2,
    }


def test_every_class_is_listed_even_at_zero() -> None:
    result = summarize([], top=5)
    assert set(result["classes"]) == set(FAILURE_CLASSES)
    assert all(c["count"] == 0 for c in result["classes"].values())


def test_an_empty_period_kind_is_named_with_zero_not_left_out() -> None:
    documents = [{"id": "a", "language": "en", "period": "annual", "flagged": []}]
    result = summarize(documents, top=1)
    assert result["documents"]["by_period_kind"] == {"annual": 1, "interim": 0}
    assert result["classes"]["duplicate"]["by_period_kind"] == {"annual": 0, "interim": 0}
