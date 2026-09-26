"""Split rules for the corpus: nothing from a golden issuer or a shared issuer leaks across pools."""

from typing import Any

import pytest

from corpus import check, issuer_key, text_layer

GOLDEN = {issuer_key(n) for n in ["Almarai Company", "Juhayna Food Industries"]}


def doc(doc_id: str, issuer: str, pool: str, url: str | None = None) -> dict[str, Any]:
    return {
        "id": doc_id,
        "issuer": issuer,
        "pool": pool,
        "role": "corporate",
        "url": url or f"https://example.com/{doc_id}.pdf",
    }


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Almarai Company", "ALMARAI CO."),
        ("Savola Group", "The Savola Group Company"),
        ("Juhayna Food Industries", "Juhayna Food Industries S.A.E"),
    ],
)
def test_issuer_key_ignores_legal_suffixes(a: str, b: str) -> None:
    assert issuer_key(a) == issuer_key(b)


def test_distinct_issuers_stay_distinct() -> None:
    assert issuer_key("Orascom Construction") != issuer_key("Orascom Investment Holding")


def test_clean_split_passes() -> None:
    report = check(
        [doc("a", "Savola Group", "blind"), doc("b", "Jarir Marketing", "train")], GOLDEN
    )
    assert report.errors == []


def test_golden_issuer_outside_dev_is_an_error() -> None:
    report = check([doc("x", "Almarai Company", "blind")], GOLDEN)
    assert report.existing == ["x"]
    assert any("belongs in dev" in e for e in report.errors)


def test_golden_issuer_in_dev_is_marked_existing() -> None:
    report = check([doc("x", "ALMARAI CO.", "dev")], GOLDEN)
    assert report.existing == ["x"]
    assert report.errors == []


def test_issuer_spanning_pools_is_an_error() -> None:
    report = check(
        [doc("en", "Jarir Marketing", "train"), doc("ar", "Jarir Marketing", "model_test")], GOLDEN
    )
    assert any("spans pools" in e for e in report.errors)


def test_duplicate_id_and_url_are_errors() -> None:
    url = "https://example.com/same.pdf"
    report = check(
        [doc("a", "Savola Group", "blind", url), doc("a", "Savola Group", "blind", url)], GOLDEN
    )
    assert any("duplicate id" in e for e in report.errors)
    assert any("same url" in e for e in report.errors)


@pytest.mark.parametrize(
    ("chars", "layer"),
    [
        ([900, 1200, 800], "digital"),
        ([0, 3, 12], "scanned"),
        ([0, 1100, 950], "mixed"),  # image cover, text body
    ],
)
def test_text_layer_is_decided_per_page(chars: list[int], layer: str) -> None:
    assert text_layer(chars) == layer


def test_candidates_file_obeys_the_rules() -> None:
    from corpus import CANDIDATES, golden_index, load_yaml

    golden_issuers, _ = golden_index()
    report = check(load_yaml(CANDIDATES)["documents"], golden_issuers)
    assert report.errors == []
