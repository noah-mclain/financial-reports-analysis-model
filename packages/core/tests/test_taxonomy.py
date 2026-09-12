"""Taxonomy loads, is internally consistent, and resolves golden set spellings."""

from pathlib import Path

import pytest
import yaml

from fra_core.schemas.statement import StatementType
from fra_core.taxonomy import load_taxonomy
from fra_core.taxonomy.loader import Taxonomy


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return load_taxonomy()


def test_loads_and_validates(taxonomy: Taxonomy) -> None:
    assert taxonomy.version == 1
    assert len(taxonomy.items) >= 40


def test_critical_items_are_present(taxonomy: Taxonomy) -> None:
    """These gate the pipeline: without them a summary would describe an incomplete statement."""
    expected = {
        "revenue",
        "net_income",
        "total_assets",
        "total_equity",
        "total_current_assets",
        "total_current_liabilities",
        "total_liabilities_and_equity",
    }
    assert expected <= set(taxonomy.critical_ids())


@pytest.mark.parametrize(
    ("label", "statement", "expected"),
    [
        ("Total assets", StatementType.BALANCE, "total_assets"),
        ("TOTAL ASSETS", StatementType.BALANCE, "total_assets"),
        ("إجمالي الأصول", StatementType.BALANCE, "total_assets"),  # Egyptian wording
        ("مجموع الموجودات", StatementType.BALANCE, "total_assets"),  # Gulf wording
        ("اجمالي الاصول", StatementType.BALANCE, "total_assets"),  # hamza dropped
        ("Right-of-use assets", StatementType.BALANCE, "right_of_use_assets"),
        ("Right of use assets", StatementType.BALANCE, "right_of_use_assets"),
        ("صافي الربح", StatementType.INCOME, "net_income"),
        ("Revenue from contracts with customers", StatementType.INCOME, "revenue"),
        ("المخزون", StatementType.BALANCE, "inventories"),
    ],
)
def test_lookup_resolves_printed_labels(
    taxonomy: Taxonomy, label: str, statement: StatementType, expected: str
) -> None:
    item = taxonomy.lookup(label, statement)
    assert item is not None
    assert item.id == expected


def test_lookup_is_scoped_to_the_statement(taxonomy: Taxonomy) -> None:
    """The same words name a share of profit in one statement and a slice of equity in another."""
    in_income = taxonomy.lookup("non-controlling interests", StatementType.INCOME)
    in_balance = taxonomy.lookup("non-controlling interests", StatementType.BALANCE)
    assert in_income is not None and in_income.id == "net_income_attributable_nci"
    assert in_balance is not None and in_balance.id == "non_controlling_interests"


def test_unknown_label_returns_none(taxonomy: Taxonomy) -> None:
    assert taxonomy.lookup("Bunker adjustment surcharge", StatementType.BALANCE) is None


def test_wrong_statement_does_not_match(taxonomy: Taxonomy) -> None:
    assert taxonomy.lookup("Total assets", StatementType.INCOME) is None


def test_every_item_has_both_languages(taxonomy: Taxonomy) -> None:
    """Arabic aliases are required, same as English."""
    missing = [i.id for i in taxonomy.items if not i.aliases_for("en") or not i.aliases_for("ar")]
    assert missing == []


def test_signs_are_valid(taxonomy: Taxonomy) -> None:
    assert all(i.natural_sign in ("+", "-") for i in taxonomy.items)


def test_duplicate_alias_within_a_statement_is_rejected(tmp_path: Path) -> None:
    """An ambiguous taxonomy must fail at load, not map a label at random later."""
    path = tmp_path / "bad.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "items": [
                    {
                        "id": "a",
                        "statement": "balance",
                        "aliases": {"en": ["total assets"], "ar": ["إجمالي الأصول"]},
                    },
                    {
                        "id": "b",
                        "statement": "balance",
                        "aliases": {"en": ["Total Assets"], "ar": ["مجموع الموجودات"]},
                    },
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="maps to both"):
        load_taxonomy(path)


def test_item_without_arabic_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "no-arabic.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "items": [{"id": "a", "statement": "balance", "aliases": {"en": ["cash"]}}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="needs at least one ar alias"):
        load_taxonomy(path)


def test_unsupported_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "v2.yaml"
    path.write_text(yaml.safe_dump({"version": 2, "items": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="not supported"):
        load_taxonomy(path)
