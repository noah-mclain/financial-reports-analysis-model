"""Which figures would be shown as confident, and which of those are wrong (D20)."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from harness.confidence import OUTCOMES, cell_outcome, confidence_part, statement_outcomes
from harness.expected import ExpectedFile, ExpectedStatement
from harness.extraction import judge_cells
from harness.gates import document_figures, gate_report, lines
from test_extraction_harness import P, extracted_of, item, row, statement_of
from test_gates import INDEX, TAXONOMY, TYPES, doc

from fra_core.schemas import LineItem, Period, PeriodKind, Statement, StatementType, TextSource
from fra_ingest.results import StructureResult
from fra_ingest.review import StatementReview, review_statement
from fra_ingest.table_checks import check_identity

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
    a = report["strata"]["annual"]["reported_languages"]["ar"]["gate_a"]
    assert a["figures"]["checked"]["scanned_ar"]["share"] == 0.7
    assert a["figures"]["checked"]["scanned_ar"]["met"] is None  # D20: no accuracy bar
    assert (
        report["strata"]["annual"]["gate_a"]["figures"]["checked"]["scanned_en"]["met"] is False
    )  # 97% is below 98%
    assert a["confident"]["checked scanned ar"]["met"] is None
    printed = "\n".join(lines(report))
    assert "A figures checked scanned_ar: 70 of 100 cells" in printed
    assert "reported, no accuracy bar (D21)" in printed
    assert (
        "A confident checked scanned ar: 0 wrong shown as confident of 100 cells reported"
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
    assert "reported only (D21)" in printed
    assert "confident wrong: a balance 'Cash' 2025-12-31: expected 5, read 6" in printed


def test_an_unreadable_document_makes_the_gated_confidence_part_not_met() -> None:
    documents = [
        with_language(
            doc("a", "annual", figures=[figure("checked", "scanned", right_held=10)]), "ar"
        )
    ]
    report = gate_report(
        documents,
        pairs=[],
        errors=[{"id": "gone", "language": "ar", "reason": "unreadable_pdf", "detail": ""}],
    )
    assert (
        report["strata"]["annual"]["reported_languages"]["ar"]["gate_a"]["confident"][
            "checked scanned ar"
        ]["met"]
        is None
    )


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


def safety_fixture() -> tuple[ExpectedFile, Statement]:
    rows = [row("Total assets", "10"), row("Total liabilities", "4"), row("Total equity", "6")]
    expected = ExpectedFile(
        id="d",
        sha256="b" * 64,
        status="checked",
        checked_by=["a", "b"],
        statements=[statement_of(rows).model_copy(update={"page_mode": "scanned"})],
    )
    extracted = extracted_of([item(i, r.label, str(r.values[P.key])) for i, r in enumerate(rows)])
    return expected, extracted.model_copy(update={"periods": [P]})


def safety_report(
    expected: ExpectedFile | None, statements: list[Statement], status: str = "passed"
) -> tuple[dict[str, Any], dict[str, Any]]:
    checks = {s.id: check_identity(s, INDEX) for s in statements}
    # Synthetic passed reviews exercise defensive coverage, including duplicate ids.
    reviews = [review_of(s) for s in statements]
    if status != "passed":
        reviews = [r.model_copy(update={"status": status}) for r in reviews]
    else:
        assert all(r.status == "passed" for r in reviews)
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=statements,
        reviews=reviews,
    )
    before = result.model_dump()
    entry = {"id": "d", "language": "ar", "scale": 1000, "currency": "SAR", "text_layer": "scanned"}
    document = document_figures(entry, result, checks, expected, INDEX, TAXONOMY, TYPES)
    report = gate_report([document], pairs=[])
    assert result.model_dump() == before
    return document, report


def safety_block(report: dict[str, Any], status: str = "checked") -> dict[str, Any]:
    return report["strata"]["total"]["reported_languages"]["ar"]["gate_a"]["confident"][
        f"{status} scanned ar"
    ]  # type: ignore[no-any-return]


def with_cell_source(statement: Statement, source: TextSource) -> Statement:
    return statement.model_copy(
        update={
            "line_items": [
                line.model_copy(
                    update={
                        "cells": [
                            cell.model_copy(
                                update={
                                    "provenance": cell.provenance.model_copy(
                                        update={"source": source}
                                    )
                                }
                            )
                            for cell in line.cells
                        ]
                    }
                )
                for line in statement.line_items
            ]
        }
    )


@pytest.mark.parametrize(
    "text_layer,source,duplicate_mode",
    [
        ("digital", TextSource.OCR, "scanned"),
        ("digital", TextSource.TEXT, "digital"),
        ("mixed", TextSource.TEXT, "digital"),
        ("mixed", TextSource.OCR, "scanned"),
        ("scanned", TextSource.TEXT, "digital"),
    ],
)
@pytest.mark.parametrize(
    "reference_case", ["duplicate", "additional_type", "missing_reference", "no_reference"]
)
def test_unassessed_statement_coverage_uses_its_own_mode(
    text_layer: str, source: TextSource, duplicate_mode: str, reference_case: str
) -> None:
    expected, primary = safety_fixture()
    expected = expected.model_copy(
        update={"statements": [expected.statements[0].model_copy(update={"page_mode": "digital"})]}
    )
    duplicate = with_cell_source(primary, source).model_copy(
        update={
            "id": "duplicate",
            "type": StatementType.INCOME if reference_case == "additional_type" else primary.type,
        }
    )
    assessed_cells = 3
    status = "checked"
    if reference_case == "missing_reference":
        expected = expected.model_copy(update={"statements": []})
        assessed_cells = 0
    elif reference_case == "no_reference":
        expected = None
        assessed_cells = 0
        status = "unreferenced"
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[primary, duplicate],
        reviews=[review_of(primary), review_of(duplicate)],
    )
    entry = {
        "id": "d",
        "language": "ar",
        "scale": 1000,
        "currency": "SAR",
        "text_layer": text_layer,
    }
    document = document_figures(entry, result, {}, expected, INDEX, TAXONOMY, TYPES)
    assert [c["page_mode"] for c in document["output_coverage"]] == ["digital", duplicate_mode]
    report = gate_report([document], pairs=[])
    for stratum in ("unknown", "total"):
        gate = report["strata"][stratum]["reported_languages"]["ar"]["gate_a"]
        if assessed_cells:
            assert set(gate["figures"]["checked"]) == {"digital"}
            assert gate["figures"]["checked"]["digital"]["cells"] == 3
            assert gate["figures"]["checked"]["digital"]["right"] == 3
        else:
            assert gate["figures"]["checked"] == {}
        digital = gate["confident"][f"{status} digital ar"]
        assert (digital["cells"], digital["accepted_share"]) == (
            assessed_cells,
            1.0 if assessed_cells else None,
        )
        assert digital["assessed_numeric"] == assessed_cells and digital["met"] is None
        if duplicate_mode == "scanned":
            scanned = gate["confident"][f"{status} scanned ar"]
            assert digital["unassessed_numeric"] == 3 - assessed_cells
            assert (scanned["cells"], scanned["accepted_share"]) == (0, None)
            assert scanned["unassessed_numeric"] == scanned["unassessed_confident"] == 3
            assert {c["statement_position"] for c in scanned["unassessed"]} == {1}
            assert scanned["wrong_confident"] == 0 and scanned["met"] is None
        else:
            assert set(gate["confident"]) == {f"{status} digital ar"}
            assert (
                digital["unassessed_numeric"]
                == digital["unassessed_confident"]
                == 6 - assessed_cells
            )
        assert sum(c["emitted_numeric"] for c in gate["confident"].values()) == 6


@pytest.mark.parametrize("empty_rows", [False, True])
def test_cell_free_unreferenced_output_cannot_certify_safety(empty_rows: bool) -> None:
    extracted = extracted_of([item(0, "Empty", None)] if empty_rows else [])
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[extracted],
        reviews=[review_of(extracted)],
    )
    entry = {"id": "d", "language": "ar", "scale": 1000, "currency": "SAR", "text_layer": "mixed"}
    document = document_figures(entry, result, {}, None, INDEX, TAXONOMY, TYPES)
    assert document["output_coverage"][0]["page_mode"] == "digital"
    report = gate_report([document], pairs=[])
    block = report["strata"]["total"]["reported_languages"]["ar"]["gate_a"]["confident"][
        "unreferenced digital ar"
    ]
    assert block["emitted_numeric"] == block["unassessed_confident"] == block["cells"] == 0
    assert block["accepted_share"] is None and block["met"] is None
    assert block["reason"] == "no confirmed expected figures; safety is not measured"


@pytest.mark.parametrize("extra", ["row", "duplicate_row", "same_object", "cell", "duplicate_cell"])
def test_confident_unassessed_output_withholds_d20_without_changing_accuracy(extra: str) -> None:
    expected, extracted = safety_fixture()
    if extra in {"row", "duplicate_row", "same_object"}:
        addition = item(0, "Invented" if extra == "row" else "Total assets", "999")
        if extra == "same_object":
            addition = extracted.line_items[0]
        extracted = extracted.model_copy(update={"line_items": [*extracted.line_items, addition]})
    else:
        line = extracted.line_items[0]
        key = P.key if extra == "duplicate_cell" else "FY2025"
        addition_cell = line.cells[0].model_copy(
            update={"period_key": key, "reported": Decimal("999")}
        )
        changed = line.model_copy(update={"cells": [*line.cells, addition_cell]})
        extracted = extracted.model_copy(
            update={"line_items": [changed, *extracted.line_items[1:]]}
        )
        if extra == "cell":
            extra_period = Period(
                key="FY2025", end_date=P.end_date, kind=PeriodKind.DURATION, months=12
            )
            extracted = extracted.model_copy(update={"periods": [P, extra_period]})
    document, report = safety_report(expected, [extracted])
    block = safety_block(report)
    assert block["met"] is None
    assert block["wrong_confident"] == 0  # unknown is not proven wrong
    assert (block["cells"], block["accepted_share"]) == (3, 1.0)
    assert (
        block["emitted_numeric"],
        block["assessed_numeric"],
        block["unassessed_numeric"],
        block["unassessed_confident"],
    ) == (4, 3, 1, 1)
    detail = block["unassessed"][0]
    assert detail["confident"] is True and detail["id"] == "d"
    assert detail["statement_position"] == 0
    assert "unassessed" in block["reason"]
    if extra == "row":
        assert document["identity"] == "ok"
        assert (
            review_statement(extracted, check_identity(extracted, INDEX), primary=True).status
            == "passed"
        )
    assert document["figures"][0]["right"] == 3
    printed = "\n".join(lines(report))
    assert "unassessed" in printed
    if extra != "same_object":
        assert "999" in printed


@pytest.mark.parametrize(
    "held,flag", [(True, None), (False, "digit_suspect"), (False, "label_merged")]
)
def test_unassessed_output_discloses_confidence_using_the_own_cell_flag(
    held: bool, flag: str | None
) -> None:
    expected, extracted = safety_fixture()
    addition = item(4, "Invented", "999")
    if flag:
        addition = flagged(addition, flag)
    extracted = extracted.model_copy(update={"line_items": [*extracted.line_items, addition]})
    # Passed plus a critical flag is a defensive case: production review would hold it.
    checks = check_identity(extracted, INDEX)
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[extracted],
        reviews=[review_of(extracted, "needs_review" if held else "passed")],
    )
    entry = {"id": "d", "language": "ar", "scale": 1000, "currency": "SAR", "text_layer": "scanned"}
    document = document_figures(
        entry, result, {extracted.id: checks}, expected, INDEX, TAXONOMY, TYPES
    )
    block = safety_block(gate_report([document], pairs=[]))
    confident = not held and flag != "digit_suspect"
    assert block["met"] is None
    assert block["unassessed_numeric"] == 1
    assert block["unassessed_confident"] == int(confident)
    assert block["unassessed"][0]["confident"] is confident


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_statement",
        "same_statement_object",
        "additional_type",
        "missing_reference",
        "no_reference",
        "unconfirmed",
    ],
)
def test_output_outside_confirmed_gold_cannot_vanish_from_confidence_coverage(case: str) -> None:
    expected, extracted = safety_fixture()
    if case != "unconfirmed":
        # These cases assert D20's safety block for additional or unreferenced OCR output.
        extracted = with_cell_source(extracted, TextSource.OCR)
    statements = [extracted]
    status = "checked"
    if case in {"duplicate_statement", "same_statement_object", "additional_type"}:
        addition = (
            extracted
            if case == "same_statement_object"
            else extracted.model_copy(
                update={
                    "id": "extra",
                    "type": StatementType.INCOME
                    if case == "additional_type"
                    else StatementType.BALANCE,
                }
            )
        )
        statements.append(addition)
    elif case == "missing_reference":
        expected = expected.model_copy(update={"statements": []})
    elif case == "no_reference":
        expected = None
        status = "unreferenced"
    else:
        e = expected.statements[0]
        e = e.model_copy(
            update={"rows": [e.rows[0].model_copy(update={"unconfirmed": [P.key]}), *e.rows[1:]]}
        )
        expected = expected.model_copy(update={"status": "draft", "statements": [e]})
        status = "draft"
    _, report = safety_report(expected, statements)
    block = safety_block(report, status)
    assert block["unassessed_confident"] > 0
    assert block["wrong_confident"] == 0
    assert block["met"] is None
    if case == "no_reference":
        assert "reported only (D21)" in "\n".join(lines(report))
    if case == "same_statement_object":
        assert {d["statement_position"] for d in block["unassessed"]} == {1}


def test_clean_matching_output_retains_d20_pass_and_full_emitted_coverage() -> None:
    expected, extracted = safety_fixture()
    _, report = safety_report(expected, [extracted])
    block = safety_block(report)
    assert block["met"] is None
    assert (block["emitted_numeric"], block["assessed_numeric"], block["unassessed_numeric"]) == (
        3,
        3,
        0,
    )


def test_conflicting_reviews_for_duplicate_statement_ids_fail_loudly() -> None:
    expected, extracted = safety_fixture()
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[extracted, extracted],
        reviews=[review_of(extracted), review_of(extracted, "needs_review")],
    )
    entry = {"id": "d", "language": "ar", "scale": 1000, "currency": "SAR", "text_layer": "scanned"}
    with pytest.raises(ValueError, match="ambiguous reviews"):
        document_figures(entry, result, {}, expected, INDEX, TAXONOMY, TYPES)


@pytest.mark.parametrize("months,stratum", [(12, "annual"), (6, "interim")])
def test_unassessed_output_stays_in_its_period_stratum(months: int, stratum: str) -> None:
    expected, extracted = safety_fixture()
    duration = Period(key="flow", end_date=P.end_date, kind=PeriodKind.DURATION, months=months)
    extracted = extracted.model_copy(
        update={
            "periods": [P, duration],
            "line_items": [*extracted.line_items, item(4, "Invented", "999")],
        }
    )
    _, report = safety_report(expected, [extracted])
    own = report["strata"][stratum]["reported_languages"]["ar"]["gate_a"]["confident"][
        "checked scanned ar"
    ]
    assert own["met"] is None and own["unassessed_confident"] == 1
    assert safety_block(report) == own


@pytest.mark.parametrize(
    "status,mode,language",
    [("draft", "scanned", "ar"), ("checked", "digital", "ar"), ("checked", "scanned", "en")],
)
def test_unassessed_output_does_not_gate_other_strata(
    status: str, mode: str, language: str
) -> None:
    expected, extracted = safety_fixture()
    expected = expected.model_copy(
        update={
            "status": status,
            "statements": [expected.statements[0].model_copy(update={"page_mode": mode})],
        }
    )
    extracted = extracted.model_copy(
        update={"line_items": [*extracted.line_items, item(4, "Invented", "999")]}
    )
    document, _ = safety_report(expected, [extracted])
    report = gate_report([{**document, "language": language}], pairs=[])
    block = report["strata"]["total"]["reported_languages"][language]["gate_a"]["confident"][
        f"{status} {mode} {language}"
    ]
    assert block["met"] is None and block["unassessed_confident"] == 1


@pytest.mark.parametrize("blank", [False, True])
@pytest.mark.parametrize("change", ["date", "kind", "months", "missing", "exact"])
def test_period_semantics_reach_the_confidence_gate(change: str, blank: bool) -> None:
    expected, extracted = safety_fixture()
    period = Period(key="FY2025", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
    expected = expected.model_copy(
        update={
            "statements": [
                expected.statements[0].model_copy(
                    update={
                        "periods": [period],
                        "rows": [
                            r.model_copy(
                                update={
                                    "values": {
                                        period.key: None if blank and i == 0 else r.values[P.key]
                                    }
                                }
                            )
                            for i, r in enumerate(expected.statements[0].rows)
                        ],
                    }
                )
            ]
        }
    )
    read_period = {
        "date": period.model_copy(update={"end_date": date(2025, 6, 30)}),
        "kind": Period(key=period.key, end_date=period.end_date, kind=PeriodKind.INSTANT),
        "months": period.model_copy(update={"months": 6}),
        "missing": None,
        "exact": period,
    }[change]
    extracted = extracted.model_copy(
        update={
            "periods": [read_period] if read_period else [],
            "line_items": [
                r.model_copy(
                    update={
                        "cells": [r.cells[0].model_copy(update={"period_key": period.key})]
                        if read_period
                        else []
                    }
                )
                for r in extracted.line_items
            ],
        }
    )
    if blank and read_period:
        first = extracted.line_items[0]
        first = first.model_copy(
            update={"cells": [first.cells[0].model_copy(update={"reported": None, "raw_text": ""})]}
        )
        extracted = extracted.model_copy(update={"line_items": [first, *extracted.line_items[1:]]})
    # Review alone cannot prove period semantics: keep passed review to test the gate's judgement.
    result = StructureResult(
        version="v",
        sha256="a" * 64,
        convert_version="c",
        settings_hash="h",
        statements=[extracted],
        reviews=[review_of(extracted)],
    )
    entry = {"id": "d", "language": "ar", "scale": 1000, "currency": "SAR", "text_layer": "scanned"}
    document = document_figures(entry, result, {}, expected, INDEX, TAXONOMY, TYPES)
    block = safety_block(gate_report([document], pairs=[]))
    assert block["met"] is None
    assert block["wrong_confident"] == (0 if change == "exact" else 3)
