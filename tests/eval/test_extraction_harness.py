"""The extraction eval's arithmetic (spec 12, Scoring)."""

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from harness.expected import ExpectedFile, ExpectedRow, ExpectedStatement
from harness.extraction import (
    OUT,
    OcrTiming,
    align_rows,
    breakdown_lines,
    convert_timing,
    engine_report,
    engine_root,
    remove_artifacts,
    report,
    scanned_breakdown,
    score_statement,
    split_cold_start,
    tessdata_digests,
    wilson_interval,
)

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.config import REPO_ROOT
from fra_ingest.results import ConvertResult, RangeConversion

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
OTHER = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)
SHA = "a" * 64


def row(label: str, value: str, unconfirmed: bool = False) -> ExpectedRow:
    return ExpectedRow(
        label=label, values={P.key: Decimal(value)}, unconfirmed=[P.key] if unconfirmed else []
    )


def item(n: int, label: str, value: str | None, period: Period = P) -> LineItem:
    cells = (
        []
        if value is None
        else [
            Cell(
                period_key=period.key,
                reported=Decimal(value),
                raw_text=value,
                provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=1),
            )
        ]
    )
    return LineItem(id=f"r{n}", raw_label=label, cells=cells)


def statement_of(rows: list[ExpectedRow]) -> ExpectedStatement:
    return ExpectedStatement(
        type=StatementType.BALANCE,
        pages=[1],
        page_mode="digital",
        scale=1000,
        currency="SAR",
        periods=[P],
        rows=rows,
    )


def extracted_of(items: list[LineItem], period: Period = P) -> Statement:
    return Statement(
        id="s",
        document_sha256=SHA,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[period],
        line_items=items,
    )


def file_of(statement: ExpectedStatement, status: str) -> ExpectedFile:
    readers = ["first reader", "second reader"] if status == "checked" else []
    return ExpectedFile.model_validate(
        {
            "id": "doc",
            "sha256": SHA,
            "status": status,
            "checked_by": readers,
            "statements": [statement],
        }
    )


def test_rows_align_by_label_or_by_a_shared_value() -> None:
    expected = [row("Inventories", "10"), row("Cash", "5"), row("Total", "15")]
    items = [
        item(1, "lnventories", "10"),
        item(2, "Notes heading", None),
        item(3, "Cash", "6"),
        item(4, "Total", "15"),
    ]
    assert align_rows(expected, items) == [0, 2, 3]


def test_a_misread_a_missing_row_and_an_extra_row_are_scored() -> None:
    expected = statement_of(
        [row("Inventories", "10"), row("Cash", "5"), row("Receivables", "7"), row("Total", "22")]
    )
    extracted = extracted_of(
        [
            item(1, "Inventories", "10"),
            item(2, "Cash", "6"),
            item(3, "Other", "99"),
            item(4, "Total", "22"),
        ]
    )
    score = score_statement(expected, extracted)
    assert (score.cells, score.right, score.extra_rows) == (4, 2, 1)


def test_a_wrong_sign_counts_against_sign_accuracy_and_cell_accuracy() -> None:
    score = score_statement(
        statement_of([row("Cost", "-60")]), extracted_of([item(1, "Cost", "60")])
    )
    assert (score.cells, score.right, score.sign_cells, score.sign_right) == (1, 0, 1, 0)


def test_periods_are_scored_by_key_date_kind_and_length() -> None:
    score = score_statement(
        statement_of([row("Cash", "5")]), extracted_of([item(1, "Cash", "5", OTHER)], OTHER)
    )
    assert (score.periods, score.periods_right) == (1, 0)
    same = score_statement(statement_of([row("Cash", "5")]), extracted_of([item(1, "Cash", "5")]))
    assert (same.periods, same.periods_right) == (1, 1)


def test_scale_and_currency_are_scored() -> None:
    wrong = extracted_of([item(1, "Cash", "5")]).model_copy(update={"scale": 1})
    score = score_statement(statement_of([row("Cash", "5")]), wrong)
    assert (score.metadata, score.metadata_right) == (2, 1)


def test_unconfirmed_cells_are_counted_apart() -> None:
    score = score_statement(
        statement_of([row("Cash", "5", unconfirmed=True), row("Total", "5")]),
        extracted_of([item(1, "Cash", "9"), item(2, "Total", "5")]),
    )
    assert (score.cells, score.right, score.unconfirmed) == (1, 1, 1)


def test_a_statement_that_was_not_extracted_misses_every_cell() -> None:
    score = score_statement(statement_of([row("Cash", "5"), row("Total", "5")]), None)
    assert (score.cells, score.right, score.periods, score.periods_right) == (2, 0, 1, 0)


def test_drafts_stay_out_of_the_gate_and_a_checked_miss_fails_it() -> None:
    wrong = extracted_of([item(1, "Cash", "9"), item(2, "Total", "5")])
    expected = statement_of([row("Cash", "5"), row("Total", "5")])
    lines, code = report([(file_of(expected, "draft"), {StatementType.BALANCE: wrong})])
    text = "\n".join(lines)
    assert code == 0 and "G1 not measured" in text and "provisional" in text
    assert "cells 2  right 1" in text
    lines, code = report([(file_of(expected, "checked"), {StatementType.BALANCE: wrong})])
    assert code == 1
    assert "digital  cells 2  right 1   50.00%  below 99.50%" in "\n".join(lines)


def test_a_checked_file_that_meets_every_threshold_passes() -> None:
    right = extracted_of([item(1, "Cash", "5"), item(2, "Total", "5")])
    expected = statement_of([row("Cash", "5"), row("Total", "5")])
    lines, code = report([(file_of(expected, "checked"), {StatementType.BALANCE: right})])
    assert code == 0 and "digital  cells 2  right 2  100.00%  meets 99.50%" in "\n".join(lines)


def test_a_right_figure_on_the_wrong_label_is_not_right() -> None:
    expected = statement_of(
        [
            row("Non-controlling interests", "102"),
            row("Total equity", "4157"),
            row("Borrowings", "2282"),
        ]
    )
    shifted = extracted_of(
        [item(1, "Total equity", "102"), item(2, "", "4157"), item(3, "Liabilities", "2282")]
    )
    score = score_statement(expected, shifted)
    assert (score.cells, score.right, score.mislabelled) == (3, 0, 3)
    assert {w["read"] for w in score.wrong} == {"102", "4157", "2282"}


def test_a_label_ocr_garbled_or_merged_with_a_heading_still_agrees() -> None:
    expected = statement_of([row("Inventories", "10"), row("Paid-up share capital", "140")])
    noisy = extracted_of(
        [
            item(1, "lnventorles", "10"),
            item(2, "Equity and liabilities Equity Paid-up share capltal", "140"),
        ]
    )
    score = score_statement(expected, noisy)
    assert (score.cells, score.right, score.mislabelled) == (2, 2, 0)


def test_a_figure_read_where_nothing_is_printed_is_wrong() -> None:
    blank = ExpectedRow(label="Treasury shares", values={P.key: None})
    invented = score_statement(
        statement_of([blank]), extracted_of([item(1, "Treasury shares", "7")])
    )
    assert (invented.cells, invented.right) == (1, 0)
    empty = extracted_of([item(1, "Treasury shares", None)])
    assert score_statement(statement_of([blank]), empty).cells == 1
    assert score_statement(statement_of([blank]), empty).right == 1


def test_extra_rows_are_printed_for_drafts_too() -> None:
    extra = extracted_of([item(1, "Cash", "5"), item(2, "Invented", "99")])
    lines, _ = report(
        [(file_of(statement_of([row("Cash", "5")]), "draft"), {StatementType.BALANCE: extra})]
    )
    assert "extra rows 1" in "\n".join(lines)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Total equity", "Total liabilities"),
        ("Total current liabilities", "Total non-current liabilities"),
        ("Trade receivables", "Trade payables"),
        ("Finance income", "Finance costs"),
        ("Revenue", "Selling and Distribution Expenses"),
    ],
)
def test_two_rows_with_swapped_values_are_both_wrong(first: str, second: str) -> None:
    expected = statement_of([row(first, "40"), row(second, "60")])
    swapped = extracted_of([item(1, second, "40"), item(2, first, "60")])
    score = score_statement(expected, swapped)
    assert (score.cells, score.right, score.mislabelled) == (2, 0, 2)


@pytest.mark.parametrize(
    ("expected", "read"),
    [
        ("Inventories", "lnventorles"),
        ("Paid-up share capital", "Equity and liabilities Equity Paid-up share capltal"),
        ("رأس المال المدفوع", "حقوق الملكية والإلتزامات دقوق الملكية رأس العال المدفوع"),
        ("احتياطي قانوني", "احتيالي فاتوني"),
        ("نقدية وأرصدة لدى البنوك", "نقدية و أرصدة لدى الينوك"),
        ("ربح السنة", "ربحالسنة"),
        ("", "anything"),
    ],
)
def test_a_noisy_or_merged_label_keeps_its_figure_right(expected: str, read: str) -> None:
    others = [row("Total equity and liabilities", "900"), row("Statutory reserve", "70")]
    statement = statement_of([row(expected, "140"), *others])
    extracted = extracted_of(
        [
            item(1, read, "140"),
            item(2, "Total equity and liabilities", "900"),
            item(3, "Statutory reserve", "70"),
        ]
    )
    score = score_statement(statement, extracted)
    assert (score.cells, score.right, score.mislabelled) == (3, 3, 0)


def test_a_figure_on_a_row_with_no_label_is_wrong() -> None:
    score = score_statement(
        statement_of([row("Total equity", "40")]), extracted_of([item(1, "", "40")])
    )
    assert (score.cells, score.right, score.mislabelled) == (1, 0, 1)


def test_blank_cells_of_a_statement_that_was_not_extracted_are_misses() -> None:
    blank = ExpectedRow(label="Treasury shares", values={P.key: None})
    score = score_statement(statement_of([blank, row("Total", "5")]), None)
    assert (score.cells, score.right) == (2, 0)


CASH = [row("Cash", "10"), row("Cash and cash equivalents", "55"), row("Total equity", "40")]


def test_a_figure_under_a_longer_label_that_contains_its_own_is_wrong() -> None:
    shifted = extracted_of(
        [
            item(1, "Cash and cash equivalemts", "10"),
            item(2, "Cash and cash equivalents", "55"),
            item(3, "Total equity", "40"),
        ]
    )
    score = score_statement(statement_of(CASH), shifted)
    assert score.mislabelled == 1 and score.wrong[0]["label"] == "Cash"


def test_a_noisy_long_label_is_not_taken_for_a_short_one_it_contains() -> None:
    noisy = extracted_of(
        [
            item(1, "Cash", "10"),
            item(2, "Cash and cosh equivalemts", "55"),
            item(3, "Total equlty", "40"),
        ]
    )
    score = score_statement(statement_of(CASH), noisy)
    assert (score.cells, score.right, score.mislabelled) == (3, 3, 0)


def test_a_cell_holding_two_rows_labels_keeps_both_figures_right() -> None:
    expected = statement_of([row("Total equity", "40"), row("Non-controlling interests", "7")])
    merged = "Total equity Non-controlling interests"
    extracted = extracted_of([item(1, merged, "40"), item(2, merged, "7")])
    score = score_statement(expected, extracted)
    assert (score.cells, score.right, score.mislabelled) == (2, 2, 0)


def test_a_heading_merged_before_a_garbled_label_keeps_its_figure_right() -> None:
    expected = statement_of(
        [
            row("Provisions", "99"),
            row("Bank overdraft", "80"),
            row("Total current liabilities", "179"),
        ]
    )
    extracted = extracted_of(
        [
            item(1, "Current liabilities rovisions", "99"),
            item(2, "Bank overdraft", "80"),
            item(3, "Total current liabilities", "179"),
        ]
    )
    score = score_statement(expected, extracted)
    assert (score.cells, score.right, score.mislabelled) == (3, 3, 0)


def test_a_figure_under_a_section_heading_alone_is_wrong() -> None:
    expected = statement_of([row("Borrowings", "2282"), row("Total liabilities", "9000")])
    extracted = extracted_of(
        [
            item(1, "Liabilities Non-current liabilities", "2282"),
            item(2, "Total liabilities", "9000"),
        ]
    )
    score = score_statement(expected, extracted)
    assert (score.right, score.mislabelled) == (1, 1)


def scanned_file(document_id: str, status: str, mode: str) -> ExpectedFile:
    statement = statement_of([row("Cash", "5"), row("Total", "15")]).model_copy(
        update={"page_mode": mode}
    )
    return file_of(statement, status).model_copy(update={"id": document_id})


def entry(language: str, issuer: str, text_layer: str = "scanned") -> dict[str, str]:
    return {"language": language, "issuer": issuer, "text_layer": text_layer}


RIGHT = extracted_of([item(1, "Cash", "5"), item(2, "Total", "15")])
HALF = extracted_of([item(1, "Cash", "5"), item(2, "Total", "16")])
DOCUMENTS = {
    "ar-doc": entry("ar", "Edita"),
    "ar-doc-2": entry("ar", "Naba"),
    "en-doc": entry("en", "Almarai"),
    "en-text": entry("en", "Almarai", "digital"),
    "ar-draft": entry("ar", "Edita"),
}


def breakdown(
    files: list[tuple[ExpectedFile, dict[StatementType, Statement]]],
    timings: dict[str, OcrTiming] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = scanned_breakdown(files, DOCUMENTS, timings or {})
    return {(r["status"], r["language"]): r for r in rows}


def test_scanned_cells_are_counted_per_language_and_status_without_digital_ones() -> None:
    found = breakdown(
        [
            (scanned_file("ar-doc", "checked", "scanned"), {StatementType.BALANCE: HALF}),
            (scanned_file("en-doc", "checked", "scanned"), {StatementType.BALANCE: RIGHT}),
            (scanned_file("en-text", "checked", "digital"), {StatementType.BALANCE: RIGHT}),
            (scanned_file("ar-draft", "draft", "scanned"), {StatementType.BALANCE: RIGHT}),
        ]
    )
    assert {k: (r["cells"], r["right"]) for k, r in found.items()} == {
        ("checked", "ar"): (2, 1),
        ("checked", "en"): (2, 2),
        ("draft", "ar"): (2, 2),
    }


def test_two_documents_of_one_language_are_one_row_with_both_counted() -> None:
    found = breakdown(
        [
            (scanned_file("ar-doc", "checked", "scanned"), {StatementType.BALANCE: HALF}),
            (scanned_file("ar-doc-2", "checked", "scanned"), {StatementType.BALANCE: RIGHT}),
        ]
    )
    assert list(found) == [("checked", "ar")]
    row_ = found[("checked", "ar")]
    assert (row_["cells"], row_["right"], row_["documents"], row_["issuers"]) == (4, 3, 2, 2)
    assert row_["cells_by_type"] == {"balance": 4}


def test_a_language_with_no_scanned_statement_has_no_row() -> None:
    found = breakdown([(scanned_file("en-text", "checked", "digital"), {})])
    assert found == {}


def test_a_document_missing_from_the_manifest_is_named() -> None:
    with pytest.raises(ValueError, match="ghost"):
        scanned_breakdown([(scanned_file("ghost", "checked", "scanned"), {})], DOCUMENTS, {})


def test_a_missing_statement_is_told_apart_from_a_misread_one() -> None:
    found = breakdown(
        [
            (scanned_file("ar-doc", "checked", "scanned"), {}),
            (scanned_file("ar-doc-2", "checked", "scanned"), {StatementType.BALANCE: HALF}),
        ]
    )
    row_ = found[("checked", "ar")]
    assert (row_["statements"], row_["statements_found"]) == (2, 1)
    assert row_["cells"] == 4
    assert (row_["cells_wrong_statement_missing"], row_["cells_wrong_other"]) == (2, 1)


def test_the_value_only_accuracy_forgives_a_figure_under_the_wrong_label() -> None:
    under_other_label = extracted_of([item(1, "Receivables", "5"), item(2, "Total", "15")])
    [files] = [
        [(scanned_file("ar-doc", "checked", "scanned"), {StatementType.BALANCE: under_other_label})]
    ]
    row_ = breakdown(files)[("checked", "ar")]
    assert row_["accuracy"] < row_["value_only_accuracy"] == 1.0


def test_unconfirmed_cells_are_counted_apart_and_not_scored() -> None:
    expected = scanned_file("ar-doc", "draft", "scanned")
    statement = expected.statements[0]
    rows = [statement.rows[0].model_copy(update={"unconfirmed": [P.key]}), statement.rows[1]]
    expected = expected.model_copy(
        update={"statements": [statement.model_copy(update={"rows": rows})]}
    )
    row_ = breakdown([(expected, {StatementType.BALANCE: RIGHT})])[("draft", "ar")]
    assert (row_["cells"], row_["unconfirmed"]) == (1, 1)


def test_the_interval_brackets_the_share_and_narrows_with_more_cells() -> None:
    low, high = wilson_interval(95, 100)
    assert low < 0.95 < high
    assert (low, high) == pytest.approx((0.8882, 0.9785), abs=1e-3)
    assert wilson_interval(950, 1000)[0] > low
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_a_document_that_is_not_fully_scanned_is_named() -> None:
    documents = {**DOCUMENTS, "ar-doc": entry("ar", "Edita", "mixed")}
    [row_] = scanned_breakdown([(scanned_file("ar-doc", "checked", "scanned"), {})], documents, {})
    assert row_["not_fully_scanned"] == ["ar-doc"]


def test_a_draft_file_does_not_count_towards_g1() -> None:
    lines, code = report([(scanned_file("ar-draft", "draft", "scanned"), {})])
    assert code == 0
    assert "checked files: 0" in lines
    assert "G1 not measured: no expected file is checked yet" in lines


def test_the_first_ocr_call_is_set_apart_and_the_rest_averaged_per_call() -> None:
    timings = {
        "ar-doc": OcrTiming((9.0, 1.0, 1.0), 10.0, 5),
        "ar-doc-2": OcrTiming((2.0,), 20.0, 5),
    }
    cold, rest = split_cold_start(["ar-doc", "ar-doc-2"], timings)
    assert cold == 9.0
    files = [
        (scanned_file("ar-doc", "checked", "scanned"), {}),
        (scanned_file("ar-doc-2", "checked", "scanned"), {}),
    ]
    [row_] = scanned_breakdown(files, DOCUMENTS, rest)
    assert row_["ocr_calls"] == 3
    assert row_["read_seconds_per_call"] == pytest.approx(4 / 3)
    assert row_["convert_seconds_per_page"] == pytest.approx(3.0)


def test_a_run_without_measured_calls_has_no_cold_start() -> None:
    cold, rest = split_cold_start(["ar-doc"], {"ar-doc": OcrTiming(None, None, 0)})
    assert cold is None
    assert rest == {"ar-doc": OcrTiming(None, None, 0)}


def test_a_timing_that_came_from_a_cache_is_not_applicable_never_zero() -> None:
    files = [
        (scanned_file("ar-doc", "checked", "scanned"), {}),
        (scanned_file("ar-doc-2", "checked", "scanned"), {}),
    ]
    cached = {"ar-doc": OcrTiming((1.0,), 10.0, 5), "ar-doc-2": OcrTiming(None, None, 0)}
    [row_] = scanned_breakdown(files, DOCUMENTS, cached)
    assert row_["ocr_calls"] is None
    assert row_["read_seconds_per_call"] is None
    assert row_["convert_seconds_per_page"] is None
    text = "\n".join(breakdown_lines([row_], None))
    assert "0.00 s" not in text
    assert "n/a" in text
    assert "convert, including layout and tables" in text


def test_convert_time_leaves_out_the_model_load_and_skipped_ranges() -> None:
    ranges = [
        RangeConversion(
            first_page=5, last_page=7, ocr="full_page", ocr_language="ar-SA", status="ok"
        ),
        RangeConversion(
            first_page=9, last_page=9, ocr="skipped", ocr_language=None, status="skipped"
        ),
    ]
    converted = ConvertResult(
        version="1",
        sha256=SHA,
        locate_version="3",
        docling_version="2",
        device="cpu",
        settings_hash="x",
        ranges=ranges,
        timings={"models": 40.0, "convert": 30.0},
    )
    assert convert_timing(converted) == (30.0, 3)


def test_each_run_has_its_own_artifact_root_and_report(tmp_path: Path) -> None:
    assert engine_root(tmp_path / "artifacts", "tesseract-psm6") == (
        tmp_path / "artifacts-tesseract-psm6"
    )
    assert engine_report("tesseract-psm6") == OUT / "extraction-golden-tesseract-psm6.json"


def test_only_a_runs_own_artifact_root_may_be_deleted(tmp_path: Path) -> None:
    for root in (REPO_ROOT / "var" / "artifacts", tmp_path / "artifacts-x", REPO_ROOT / "var"):
        with pytest.raises(ValueError, match="refusing"):
            remove_artifacts(root)


def test_a_delete_that_fails_is_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(path: object) -> None:
        raise PermissionError(str(path))

    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr("harness.extraction.shutil.rmtree", refuse)
    with pytest.raises(PermissionError):
        remove_artifacts(REPO_ROOT / "var" / "artifacts-test-label")


def test_the_traineddata_files_are_hashed_from_the_directory_tesseract_lists(
    tmp_path: Path,
) -> None:
    data = tmp_path / "tessdata"
    data.mkdir()
    (data / "ara.traineddata").write_bytes(b"arabic")
    (data / "eng.traineddata").write_bytes(b"english")
    script = tmp_path / "tesseract"
    script.write_text(
        f"#!/bin/sh\nprintf 'List of available languages in \"{data}/\" (2):\\nara\\neng\\n'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    digests = tessdata_digests(str(script), ("ara", "eng"))
    assert digests["ara"] == hashlib.sha256(b"arabic").hexdigest()
    assert digests["eng"] == hashlib.sha256(b"english").hexdigest()
