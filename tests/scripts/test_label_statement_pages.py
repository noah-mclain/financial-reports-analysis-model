"""Parsing and writing statement page labels."""

import pytest

from label_statement_pages import (
    apply_to_manifest,
    check_labels,
    disagreements,
    manifest_block,
    page_number,
    parse_ranges,
    request_allowed,
)

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


# Hardening of the local labelling server.


@pytest.mark.parametrize(
    ("origin", "content_type", "allowed"),
    [
        ("http://127.0.0.1:8765", "application/json", True),
        ("http://localhost:8765", "application/json", True),
        (None, "application/json", True),  # not a browser, so not a cross-site request
        ("https://evil.example", "application/json", False),
        ("http://127.0.0.1:8765", "text/plain", False),  # a simple request needs no preflight
        ("http://127.0.0.1:9999", "application/json", False),
    ],
)
def test_only_the_sheet_itself_may_save_labels(
    origin: str | None, content_type: str, allowed: bool
) -> None:
    assert request_allowed(origin, content_type, port=8765) is allowed


@pytest.mark.parametrize(("text", "expected"), [("1", 1), ("62", 62)])
def test_page_numbers_are_checked(text: str, expected: int) -> None:
    assert page_number(text, page_count=62) == expected


@pytest.mark.parametrize("text", ["0", "63", "-1", "x", ""])
def test_bad_page_numbers_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        page_number(text, page_count=62)


def test_labels_beyond_the_document_are_refused() -> None:
    with pytest.raises(ValueError, match="page 70"):
        check_labels({"financial_position": [(5, 5)], "cash_flows": [(9, 70)]}, page_count=62)
    check_labels({"financial_position": [(5, 5)]}, page_count=62)
