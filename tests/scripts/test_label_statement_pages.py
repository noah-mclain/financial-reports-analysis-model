"""Parsing and writing statement page labels."""

import pytest

from label_statement_pages import apply_to_manifest, disagreements, manifest_block, parse_ranges

MANIFEST = """documents:
  - id: juhayna-2025-ar-standalone
    file: documents/juhayna-2025-ar-standalone.pdf
    statement_pages: null
    traits: [scanned]

  - id: juhayna-2025-ar-consolidated
    statement_pages: null
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", [(5, 5)]),
        ("5-6", [(5, 6)]),
        ("5-6, 9", [(5, 6), (9, 9)]),
        ("", []),
        (" 7 - 8 ", [(7, 8)]),
    ],
)
def test_parse_ranges(text: str, expected: list[tuple[int, int]]) -> None:
    assert parse_ranges(text) == expected


@pytest.mark.parametrize("text", ["6-5", "x", "0"])
def test_bad_ranges_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_ranges(text)


def test_manifest_block_writes_single_and_multiple_ranges() -> None:
    block = manifest_block({"financial_position": [(5, 5)], "profit_or_loss": [(6, 6), (20, 20)]})
    assert block == [
        "    statement_pages:  # labelled blind from the OCR sheet, then reconciled",
        "      financial_position: [5, 5]",
        "      profit_or_loss: [[6, 6], [20, 20]]",
    ]


def test_apply_replaces_only_that_document() -> None:
    updated = apply_to_manifest(
        MANIFEST, "juhayna-2025-ar-standalone", {"financial_position": [(4, 4)]}
    )
    assert "      financial_position: [4, 4]\n    traits: [scanned]" in updated
    assert updated.count("statement_pages: null") == 1


def test_apply_refuses_a_document_that_is_already_labelled() -> None:
    once = apply_to_manifest(
        MANIFEST, "juhayna-2025-ar-standalone", {"financial_position": [(4, 4)]}
    )
    with pytest.raises(ValueError, match="already"):
        apply_to_manifest(once, "juhayna-2025-ar-standalone", {"financial_position": [(4, 4)]})


def test_disagreements() -> None:
    assert disagreements({4, 5}, {5, 6}) == ([6], [4])  # (suggested only, marked only)
