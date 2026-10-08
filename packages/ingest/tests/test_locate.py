"""The assembled locate result and its flags."""

from pathlib import Path

from support import FakeOcr, make_blank_pdf, numbers_block, text_page

from fra_ingest.config import IngestConfig
from fra_ingest.industry import industry_decision
from fra_ingest.locate import locate
from fra_ingest.results import LocateResult, PageText
from fra_ingest.stage import locate_pdf

SHA = "0" * 64
BALANCE_HEADER = "Statement of financial position\nNote 2025 2024\nEGP '000"
INCOME_HEADER = "Statement of profit or loss\nNote 2025 2024\nEGP '000"


def filler(page_no: int) -> PageText:
    return text_page("Chairman's letter", "We had a good year. " * 20, page_no=page_no)


def document(*pages: PageText) -> list[PageText]:
    return list(pages)


def test_a_simple_filing() -> None:
    pages = [
        filler(1),
        filler(2),
        text_page(BALANCE_HEADER, numbers_block(), page_no=3),
        text_page(INCOME_HEADER, numbers_block(), page_no=4),
    ] + [filler(n) for n in range(5, 21)]
    result = locate(pages, IngestConfig(), sha256=SHA, filename="x.pdf")
    assert result.convert_ranges == [(2, 5)]
    assert result.flags == ["statement_not_found:comprehensive_income"]
    assert result.document.page_count == 20
    assert result.industry.kind == "corporate"
    assert result.candidate_share == 4 / 20


def test_nothing_found() -> None:
    result = locate([filler(n) for n in range(1, 6)], IngestConfig(), sha256=SHA, filename="x.pdf")
    assert result.convert_ranges == []
    assert "no_statements_found" in result.flags
    assert "statement_not_found:balance" in result.flags


def test_low_selectivity() -> None:
    pages = [text_page(BALANCE_HEADER, numbers_block(), page_no=n) for n in (1, 3, 5, 7)]
    pages += [filler(n) for n in (2, 4, 6, 8)]
    result = locate(
        sorted(pages, key=lambda p: p.page_no), IngestConfig(), sha256=SHA, filename="x.pdf"
    )
    assert "low_selectivity" in result.flags


def test_a_scanned_filing_without_an_engine_is_visibly_unread(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    result = locate_pdf(make_blank_pdf(tmp_path / "scan.pdf", pages=4), config, None)
    assert "image_pages_not_read:4" in result.flags
    assert "no_statements_found" in result.flags
    assert result.industry.kind == "unknown"


def test_locate_json_round_trips(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    result = locate_pdf(make_blank_pdf(tmp_path / "scan.pdf", pages=2), config, FakeOcr())
    written = tmp_path / "artifacts" / result.document.sha256 / "locate.json"
    assert LocateResult.model_validate_json(written.read_text(encoding="utf-8")) == result
    assert {"read", "ocr", "score"} <= result.timings.keys()


def test_a_bank_is_flagged() -> None:
    body = (
        numbers_block()
        + "\nDeposits from customers\nLoans and advances to customers\nBalances with the central bank"
    )
    pages = [
        text_page(BALANCE_HEADER, body, page_no=1),
        text_page(INCOME_HEADER, numbers_block(), page_no=2),
    ]
    result = locate(pages, IngestConfig(), sha256=SHA, filename="bank.pdf")
    assert "likely_bank" in result.flags


def test_failed_ocr_pages_are_reported(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    result = locate_pdf(make_blank_pdf(tmp_path / "scan.pdf", pages=3), config, FakeOcr(fail=True))
    assert "ocr_failed_pages:3" in result.flags


def test_a_run_from_cache_reports_no_ocr_time(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    pdf = make_blank_pdf(tmp_path / "scan.pdf", pages=2)
    locate_pdf(pdf, config, FakeOcr())
    again = locate_pdf(pdf, config, FakeOcr())
    assert again.timings["ocr"] == 0.0
    assert again.timings["text"] >= 0.0


def test_a_weak_investment_pair_is_held_after_statement_location() -> None:
    pages = [
        text_page(BALANCE_HEADER, "Investment securities\n" + numbers_block(), page_no=1),
        text_page(INCOME_HEADER, "Investment income\n" + numbers_block(), page_no=2),
    ]
    result = locate(pages, IngestConfig(), sha256=SHA, filename="investment.pdf")
    assert result.ranges
    decision = industry_decision(result.industry)
    assert decision is not None
    assert (decision.outcome, decision.code) == ("needs_review", "near_threshold")
