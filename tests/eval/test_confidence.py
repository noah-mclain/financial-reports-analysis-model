"""Which figures would be shown as confident, and which of those are wrong (D20)."""

from decimal import Decimal
from typing import Any

import pytest
from harness.confidence import OUTCOMES, cell_outcome, confidence_part, statement_outcomes
from harness.expected import ExpectedFile, ExpectedStatement
from harness.extraction import judge_cells
from harness.gates import document_figures, gate_report, lines
from test_extraction_harness import P, extracted_of, item, row, statement_of
from test_gates import INDEX, TAXONOMY, TYPES, doc

from fra_core.schemas import LineItem, Statement, StatementType
from fra_ingest.results import StructureResult
from fra_ingest.review import StatementReview

EXPECTED = statement_of([row("Inventories", "10"), row("Cash", "5")])


def review_of(statement: Statement, status: str = "passed") -> StatementReview:
    return StatementReview(
        statement_id=statement.id,
        status=status,  # type: ignore[arg-type]
        numeric_cells=2,
        checked_cells=2,
        flagged_cells=0,
    )


def flagged(line: LineItem, flag: str) -> LineItem:
    return line.model_copy(update={"cells": [line.cells[0].model_copy(update={"flags": [flag]})]})


def outcomes(statement: Statement | None, status: str = "passed") -> list[str]:
    review = review_of(statement, status) if statement is not None else None
    return [cell_outcome(c, statement, review) for c in judge_cells(EXPECTED, statement)]


def test_a_right_figure_on_a_passed_statement_is_accepted_and_on_a_held_one_held() -> None:
    extracted = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "5")])
    assert outcomes(extracted) == ["right_accepted", "right_accepted"]
    assert outcomes(extracted, "needs_review") == ["right_held", "right_held"]


def test_a_misread_on_a_passed_statement_with_no_flag_is_confident_wrong() -> None:
    extracted = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "6")])
    assert outcomes(extracted) == ["right_accepted", "wrong_confident"]
    counts, confident = statement_outcomes(EXPECTED, extracted, review_of(extracted))
    assert counts["wrong_confident"] == 1 and counts["right_accepted"] == 1
    assert confident == [
        {
            "statement": "balance",
            "label": "Cash",
            "period": P.key,
            "expected": "5",
            "read": "6",
            "label_read": "Cash",
        }
    ]


def test_a_misread_is_caught_by_its_own_critical_flag_or_by_the_statement_hold() -> None:
    suspect = extracted_of(
        [item(1, "Inventories", "10"), flagged(item(2, "Cash", "6"), "digit_suspect")]
    )
    # A critical flag catches the cell even if the statement were passed.
    assert outcomes(suspect) == ["right_accepted", "wrong_flagged"]
    assert outcomes(suspect, "needs_review") == ["right_held", "wrong_flagged"]
    plain = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "6")])
    assert outcomes(plain, "needs_review") == ["right_held", "wrong_held"]


def test_a_warning_flag_does_not_catch_a_misread() -> None:
    warned = extracted_of(
        [item(1, "Inventories", "10"), flagged(item(2, "Cash", "6"), "label_merged")]
    )
    assert outcomes(warned) == ["right_accepted", "wrong_confident"]


def test_a_row_missing_from_a_passed_statement_is_confident_wrong() -> None:
    extracted = extracted_of([item(1, "Inventories", "10")])
    assert outcomes(extracted) == ["right_accepted", "wrong_confident"]


def test_every_figure_of_a_statement_not_extracted_is_not_extracted() -> None:
    assert outcomes(None) == ["wrong_not_extracted", "wrong_not_extracted"]
    counts, confident = statement_outcomes(EXPECTED, None, None)
    assert counts == {**dict.fromkeys(OUTCOMES, 0), "wrong_not_extracted": 2}
    assert confident == []


def test_a_statement_without_its_own_review_is_an_error() -> None:
    extracted = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "5")])
    cell = judge_cells(EXPECTED, extracted)[0]
    with pytest.raises(ValueError, match="has no review"):
        cell_outcome(cell, extracted, None)
    other = review_of(extracted.model_copy(update={"id": "other"}))
    with pytest.raises(ValueError, match="has no review"):
        cell_outcome(cell, extracted, other)


def test_unconfirmed_figures_get_no_outcome() -> None:
    expected = statement_of([row("Inventories", "10"), row("Cash", "5", unconfirmed=True)])
    extracted = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "6")])
    counts, _ = statement_outcomes(expected, extracted, review_of(extracted))
    assert sum(counts.values()) == 1 and counts["right_accepted"] == 1


# ---- summed per status, page mode and language ------------------------------------------------


def figure(status: str, mode: str, **counts: int) -> dict[str, Any]:
    confident = [
        {
            "statement": "balance",
            "label": "Cash",
            "period": P.key,
            "expected": "5",
            "read": "6",
            "label_read": "Cash",
        }
    ] * counts.get("wrong_confident", 0)
    return {
        "status": status,
        "page_mode": mode,
        "cells": sum(counts.values()),
        "right": counts.get("right_accepted", 0) + counts.get("right_held", 0),
        "outcomes": {**dict.fromkeys(OUTCOMES, 0), **counts},
        "confident_wrong": confident,
    }


def with_language(document: dict[str, Any], language: str) -> dict[str, Any]:
    return {**document, "language": language}


GATED = {("scanned", "ar"): True}


def test_only_checked_scanned_arabic_is_judged_and_one_confident_wrong_fails_it() -> None:
    documents = [
        with_language(
            doc("a", "annual", figures=[figure("checked", "scanned", right_held=8, wrong_held=2)]),
            "ar",
        ),
        with_language(
            doc(
                "b",
                "annual",
                figures=[figure("checked", "scanned", right_accepted=9, wrong_confident=1)],
            ),
            "en",
        ),
        with_language(
            doc("c", "annual", figures=[figure("draft", "scanned", wrong_confident=3)]), "ar"
        ),
    ]
    part = confidence_part(documents, GATED)
    assert set(part) == {"checked scanned ar", "checked scanned en", "draft scanned ar"}
    arabic = part["checked scanned ar"]
    assert arabic["met"] is True and arabic["cells"] == 10 and arabic["files"] == 1
    assert arabic["accepted_share"] == 0.0  # zero confident wrong only because everything is held
    assert part["checked scanned en"]["met"] is None  # reported, not gated by D20
    assert part["checked scanned en"]["confident_wrong"][0]["id"] == "b"
    assert part["draft scanned ar"]["met"] is None
    failing = [
        with_language(
            doc(
                "a",
                "annual",
                figures=[figure("checked", "scanned", right_accepted=9, wrong_confident=1)],
            ),
            "ar",
        )
    ]
    block = confidence_part(failing, GATED)["checked scanned ar"]
    assert block["met"] is False and block["accepted_share"] == 1.0


def test_a_gated_block_with_no_cells_is_not_measured() -> None:
    documents = [with_language(doc("a", "annual", figures=[figure("checked", "scanned")]), "ar")]
    block = confidence_part(documents, GATED)["checked scanned ar"]
    assert block["met"] is None and block["accepted_share"] is None


# ---- through the gate report ------------------------------------------------------------------


def test_the_gate_report_splits_scanned_figures_by_language_with_no_bar_for_arabic() -> None:
    documents = [
        with_language(
            doc(
                "a", "annual", figures=[figure("checked", "scanned", right_held=70, wrong_held=30)]
            ),
            "ar",
        ),
        with_language(
            doc(
                "b",
                "annual",
                figures=[figure("checked", "scanned", right_accepted=97, wrong_held=3)],
            ),
            "en",
        ),
    ]
    report = gate_report(documents, pairs=[])
    a = report["strata"]["annual"]["gate_a"]
    assert a["figures"]["checked"]["scanned_ar"]["share"] == 0.7
    assert a["figures"]["checked"]["scanned_ar"]["met"] is None  # D20: no accuracy bar
    assert a["figures"]["checked"]["scanned_en"]["met"] is False  # 97% is below 98%
    assert a["confident"]["checked scanned ar"]["met"] is True
    printed = "\n".join(lines(report))
    assert "A figures checked scanned_ar: 70 of 100 cells" in printed
    assert "reported, no accuracy bar (D20)" in printed
    assert (
        "A confident checked scanned ar: 0 wrong shown as confident of 100 cells met (D20)"
        in printed
    )


def test_a_confident_wrong_figure_is_named_in_the_printed_report() -> None:
    documents = [
        with_language(
            doc(
                "a",
                "annual",
                figures=[figure("checked", "scanned", right_accepted=9, wrong_confident=1)],
            ),
            "ar",
        )
    ]
    printed = "\n".join(lines(gate_report(documents, pairs=[])))
    assert "NOT met (D20)" in printed
    assert "confident wrong: a balance 'Cash' 2025-12-31: expected 5, read 6" in printed


def test_an_unreadable_document_makes_the_gated_confidence_part_not_met() -> None:
    documents = [
        with_language(
            doc("a", "annual", figures=[figure("checked", "scanned", right_held=10)]), "ar"
        )
    ]
    report = gate_report(documents, pairs=[], errors=["gone: unreadable_pdf"])
    assert report["strata"]["annual"]["gate_a"]["confident"]["checked scanned ar"]["met"] is False


def test_document_figures_carry_outcomes_from_the_statement_review() -> None:
    extracted = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "6")])
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[extracted],
        reviews=[review_of(extracted)],
    )
    expected = ExpectedFile(
        id="d",
        sha256="b" * 64,
        status="checked",
        checked_by=["a", "b"],
        statements=[
            ExpectedStatement(
                type=StatementType.BALANCE,
                pages=[1],
                page_mode="scanned",
                scale=1000,
                currency="SAR",
                periods=[P],
                rows=EXPECTED.rows,
            )
        ],
    )
    entry = {"id": "d", "language": "ar", "scale": 1000, "currency": "SAR", "text_layer": "scanned"}
    figures = document_figures(entry, result, {}, expected, INDEX, TAXONOMY, TYPES)["figures"]
    assert figures[0]["outcomes"]["wrong_confident"] == 1
    assert figures[0]["confident_wrong"][0]["read"] == str(Decimal("6"))
