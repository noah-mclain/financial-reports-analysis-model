"""Bank, insurer and other financial companies, read from their statement pages."""

from support import numbers_block, text_page

from fra_core.schemas import PageMode, StatementType
from fra_ingest.industry import detect_industry, industry_decision, load_industry_book
from fra_ingest.results import PageText, StatementRange

BOOK = load_industry_book()


def on_statement_pages(*bodies: str) -> tuple[list[PageText], list[StatementRange]]:
    pages = [
        text_page("Statement of financial position", body + "\n" + numbers_block(), page_no=n)
        for n, body in enumerate(bodies, start=1)
    ]
    ranges = [
        StatementRange(
            type=StatementType.BALANCE, first_page=1, last_page=len(pages), score=8.0, rank=1
        )
    ]
    return pages, ranges


def test_a_bank() -> None:
    pages, ranges = on_statement_pages(
        "Cash and balances with the central bank\nLoans and advances to customers\nDeposits from customers"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert signal.kind == "bank"
    assert signal.subkind is None
    assert (1, "deposits from customers") in signal.evidence


def test_an_arabic_bank() -> None:
    pages, ranges = on_statement_pages("أرصدة لدى البنك المركزي\nقروض وسلف للعملاء\nودائع العملاء")
    assert detect_industry(pages, ranges, BOOK).kind == "bank"


def test_an_insurer() -> None:
    pages, ranges = on_statement_pages(
        "Insurance contract liabilities\nReinsurance contract assets\nInsurance revenue"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "insurer"


def test_an_arabic_insurer() -> None:
    pages, ranges = on_statement_pages(
        "التزامات عقود التأمين\nموجودات عقود إعادة التأمين\nإيرادات التأمين"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "insurer"


def test_an_exchange_operator() -> None:
    pages, ranges = on_statement_pages(
        "Trading commission income\nListing fees\nClearing and settlement fees"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "exchange_operator")


def test_a_brokerage() -> None:
    pages, ranges = on_statement_pages(
        "Brokerage commission income\nMargin lending to clients\nClients' money"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "brokerage")


def test_an_investment_holding() -> None:
    pages, ranges = on_statement_pages(
        "Net gains on investments at fair value through profit or loss\nDividend income from investees\nPrivate equity investments"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "investment_holding")


def test_a_consumer_finance_company() -> None:
    pages, ranges = on_statement_pages(
        "Income from Islamic financing contracts\nNet investment in finance receivables\nConsumer finance receivables"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "consumer_finance")


def test_an_arabic_consumer_finance_company_with_reversed_word_order() -> None:
    # As pypdfium2 extracts most Arabic filings: words spelled right, line order reversed.
    pages, ranges = on_statement_pages("اإلسالمي التمويل عقود من إيرادات\nاالستهالكي التمويل مدينو")
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "consumer_finance")


def test_an_asset_manager() -> None:
    pages, ranges = on_statement_pages(
        "Fund management fees\nAssets under management\nSubscription fees"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "asset_manager")


def test_ordinary_company_wording_stays_corporate() -> None:
    pages, ranges = on_statement_pages(
        "Cash and bank balances\nPrepaid insurance\nBank borrowings\nInvestments in associates"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "corporate"


def test_cues_outside_statement_pages_do_not_decide_when_statements_were_found() -> None:
    pages, ranges = on_statement_pages("Revenue\nCost of sales")
    pages.append(text_page("Treasury", "Deposits from customers\nNet interest income", page_no=2))
    assert detect_industry(pages, ranges, BOOK).kind == "corporate"


def test_no_readable_text_is_unknown() -> None:
    pages = [text_page("", "", page_no=1)]
    assert detect_industry(pages, [], BOOK).kind == "unknown"


def test_bank_wording_from_gulf_filings() -> None:
    # Arab Banking Corporation: deposits from banks and certificates of deposit, with the
    # lam-alef ligature split the wrong way in العمالء.
    pages, ranges = on_statement_pages("ودائع العمالء\nودائع البنوك\nشهادات إيداع")
    assert detect_industry(pages, ranges, BOOK).kind == "bank"


def test_takaful_is_insurance() -> None:
    pages, ranges = on_statement_pages(
        "موجودات عقود التكافل\nمطلوبات عقود التكافل\nإيرادات التكافل"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "insurer"


def test_the_whole_document_decides_when_statement_pages_hold_no_cues() -> None:
    # Orient Takaful: a damaged text layer led the locator to a notes page, but the filing
    # speaks of takaful contracts throughout.
    pages, ranges = on_statement_pages("Revenue\nCost of sales")
    pages += [
        text_page(
            "Notes",
            "Takaful contract liabilities\nInsurance revenue\nReinsurance contract assets",
            page_no=n,
        )
        for n in range(2, 5)
    ]
    assert detect_industry(pages, ranges, BOOK).kind == "insurer"


def test_one_phrase_repeated_through_a_report_is_not_a_verdict() -> None:
    # Saudi Energy, a utility, holds "deposits from customers" on many pages; Egypt Kuwait
    # Holding mentions a consumer finance subsidiary throughout.
    pages, ranges = on_statement_pages("Revenue\nCost of sales")
    pages += [
        text_page("Notes", "Deposits from customers\nConsumer finance", page_no=n)
        for n in range(2, 12)
    ]
    assert detect_industry(pages, ranges, BOOK).kind == "corporate"


def test_a_mostly_unread_document_is_unknown() -> None:
    # A scanned bank on a machine without OCR: a text cover page is not enough to call it
    # corporate.
    cover = text_page("Annual report 2025", "Chairman's statement " * 10, page_no=1)
    unread = [
        PageText(
            page_no=n,
            mode=PageMode.IMAGE,
            source=None,
            char_count=0,
            width_pt=595,
            height_pt=842,
            flags=["ocr_unavailable"],
        )
        for n in range(2, 60)
    ]
    assert detect_industry([cover, *unread], [], BOOK).kind == "unknown"


def test_evidence_lists_only_the_winning_kind() -> None:
    pages, ranges = on_statement_pages(
        "Deposits from customers\nLoans and advances to customers\nBalances with the central bank\n"
        "Brokerage commission"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert signal.kind == "bank"
    assert all("brokerage" not in cue for _, cue in signal.evidence)


def test_a_tie_goes_to_the_kind_listed_first() -> None:
    # Equal evidence for bank (3 + 3) and insurer (3 + 3): bank is listed first in the cues.
    pages, ranges = on_statement_pages(
        "Deposits from customers\nLoans and advances to customers\n"
        "Insurance contract liabilities\nReinsurance contract assets"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "bank"


def test_the_signal_counts_distinct_cues_before_the_evidence_is_cut() -> None:
    # One phrase on 15 statement pages: the evidence shows 12 of them, but it is one cue.
    pages, ranges = on_statement_pages(*["Deposits from customers"] * 15)
    signal = detect_industry(pages, ranges, BOOK)
    assert len(signal.evidence) == 12
    assert signal.distinct_cues == 1
    pages, ranges = on_statement_pages(
        "Deposits from customers\nLoans and advances to customers\n"
        "Balances with the central bank\nDeposits from customers"
    )
    assert detect_industry(pages, ranges, BOOK).distinct_cues == 3


def test_a_consumer_finance_company_named_by_its_income_lines() -> None:
    # Contact Financial (fit): securitized portfolios and financing income, no bank vocabulary.
    pages, ranges = on_statement_pages(
        "Revenue from portfolio transfer\nOff balance sheet portfolio management fee\n"
        "Securitization surplus\nIncome from financing activities"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "consumer_finance")


def test_an_arabic_consumer_finance_company_named_by_its_income_lines() -> None:
    pages, ranges = on_statement_pages(
        "ناتج إحالة محافظ حقوق مالية\nأتعاب المحافظ المدارة\nإيرادات عوائد الأنشطة التمويلية"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "consumer_finance")


def test_a_licensed_asset_manager_is_read_from_its_notes() -> None:
    # Musharaka Capital (fit): ordinary-looking statements; its Arabic notes name the licensed
    # securities business, asset management services, custody and subscription fees.
    pages, ranges = on_statement_pages("Revenue from service contracts with customers")
    pages += [
        text_page("Notes", body, page_no=n)
        for n, body in enumerate(
            [
                "تقوم الشركة بأعمال الأوراق المالية بموجب الترخيص",
                "يتم إثبات أتعاب خدمات إدارة الموجودات عند تقديم الخدمة",
                "يتم إثبات رسوم الحفظ مقدما",
                "يتم إثبات أتعاب الاشتراك في الصناديق الاستثمارية",
            ],
            start=2,
        )
    ]
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "asset_manager")


def test_an_investment_company_whose_income_is_investment_income() -> None:
    # Coast Investment (validation, recorded in validation_uses.yaml): no revenue line, only
    # net investment income, fees and associates.
    pages, ranges = on_statement_pages(
        "Net investment income\nManagement fees\nShare of results of associates"
    )
    pages.append(text_page("Notes", "Private equity investments", page_no=2))
    signal = detect_industry(pages, ranges, BOOK)
    # Two investment cues are too few for a verdict, but close enough to hold for review.
    assert (signal.kind, signal.score, signal.distinct_cues) == ("corporate", 4.0, 2)
    decision = industry_decision(signal)
    assert decision is not None
    assert (decision.outcome, decision.code) == ("needs_review", "near_threshold")


def test_management_fees_alone_do_not_mark_a_group_as_financial() -> None:
    # Qalaa Holdings (fit, corporate) reports management fees from its platform companies.
    pages, ranges = on_statement_pages("Revenue\nCost of sales\nManagement fees")
    assert detect_industry(pages, ranges, BOOK).score == 0.0


def test_a_property_developer_that_securitizes_receivables_stays_corporate() -> None:
    # Madinet Nasr (fit, corporate) securitizes receivables and manages property assets.
    pages, ranges = on_statement_pages("Revenue from sale of units\nCost of sales")
    pages += [
        text_page(
            "Notes",
            "Securitization of receivables\nProperty and asset management services",
            page_no=n,
        )
        for n in range(2, 6)
    ]
    signal = detect_industry(pages, ranges, BOOK)
    assert signal.kind == "corporate"
    assert industry_decision(signal) is None
