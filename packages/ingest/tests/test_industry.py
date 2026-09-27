"""Bank, insurer and other financial companies, read from their statement pages."""

from support import numbers_block, text_page

from fra_core.schemas import StatementType
from fra_ingest.industry import detect_industry, load_industry_book
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
