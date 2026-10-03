"""Interim statements from Argaam's financial-statements listing.

    uv run python scripts/argaam_listing.py collect --market main|nomu --cache DIR \
        --report FILE [--write]
    uv run python scripts/corpus.py fetch --new
    uv run python scripts/argaam_listing.py screen --market main|nomu --cache DIR [--apply]

Argaam lists, for every company on a Saudi market, the Arabic and English edition of each
period's statements for the current year. `collect` reads that one table, keeps corporate
issuers that have an Arabic edition of the market's period (the first quarter on the main
market, the half year on Nomu), settles which of them the corpus already holds, and adds an
entry per edition to eval/corpus/candidates.yaml. `screen` then checks the downloaded files
(language, period, clean text layer) and takes out the editions that fail: they are set aside
in eval/corpus/deferred.yaml, with the reason.

The year tabs of earlier years are behind the site's subscription; only the open year is read.
"""

from __future__ import annotations

import argparse
import functools
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from itertools import chain
from pathlib import Path
from typing import Any, NamedTuple, TypeVar
from urllib.parse import parse_qs, urlsplit

import yaml

import corpus
from corpus import Politeness, assign_pool, issuer_key
from fra_core.schemas import PageMode, StatementType
from fra_ingest.config import IngestConfig, load_config
from fra_ingest.locate import TitleBook, find_ranges, load_title_book, score_page
from fra_ingest.pages import GARBLED_TEXT_LAYER, read_pages
from fra_ingest.results import PageScore, PageText, StatementRange

T = TypeVar("T")
DECISIONS = corpus.ROOT / "eval/corpus/argaam_decisions.yaml"
SITE = "https://www.argaam.com"
COUNTRY = "SA"
FISCAL_YEAR = 2026
LANGUAGES = ("ar", "en")
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


# Each month's spellings in Arabic text layers, January first.
_ARABIC_MONTHS = [
    ("يناير", "كانون الثاني"),
    ("فبراير", "شباط"),
    ("مارس",),
    ("أبريل", "إبريل", "نيسان"),
    ("مايو", "أيار"),
    ("يونيو", "يونية", "حزيران"),
    ("يوليو", "يولية", "تموز"),
    ("أغسطس", "آب"),
    ("سبتمبر", "أيلول"),
    ("أكتوبر", "تشرين الأول"),
    ("نوفمبر", "تشرين الثاني"),
    ("ديسمبر", "كانون الأول"),
]


class Period(NamedTuple):
    """The period end a listing column holds, in the year collected."""

    day: int
    month: int

    @property
    def arabic_month(self) -> tuple[str, ...]:
        return _ARABIC_MONTHS[self.month - 1]

    @property
    def label(self) -> str:
        return f"{self.day} {_MONTHS[self.month - 1].title()} {FISCAL_YEAR}"


class Market(NamedTuple):
    """A listing: Argaam's market id, the column to read and the period that column holds."""

    name: str
    market_id: int
    column: str
    period: Period

    @property
    def listing_url(self) -> str:
        return f"{SITE}/en/company/financial-pdf/{self.market_id}/{FISCAL_YEAR}?isajax=true"


# The main market reports quarters: its Q1 column holds 31 March. Nomu reports half-yearly: its
# Q2 column holds the half year, 30 June.
MAIN = Market("main", 3, "Q1", Period(31, 3))
NOMU = Market("nomu", 14, "Q2", Period(30, 6))
MARKETS = {market.name: market for market in (MAIN, NOMU)}
_EDITION_LABELS = {"Ar": "ar", "En": "en"}
_BOARD_REPORT = "Board Report"
# The company table has these columns, except that the REIT tables report halves, not Q2 and Q4.
_LAYOUTS = (
    ["Company", "Q1", "Q2", "Q3", "Q4", "Annual", "Board Report"],
    ["Company", "Q1", "First half", "Q3", "Second half", "Annual", "Board Report"],
)

# The source's sectors that hold no corporate issuer: banks, insurers, financing and brokerage
# companies, and funds.
SKIPPED_SECTORS = frozenset({"Banks", "Insurance", "Financial Services", "REITs", "ETFs", "CEFs"})
# The sectors of corporate issuers. A sector in neither set stops the run: it has to be sorted
# into one of them before its companies are collected.
CORPORATE_SECTORS = frozenset(
    {
        "Materials",
        "Food & Beverages",
        "Consumer Services",
        "Capital Goods",
        "Real Estate Mgmt & Dev't",
        "Consumer Discretionary Distribution & Retail",
        "Health Care Equipment & Svc",
        "Transportation",
        "Consumer Staples Distribution & Retail",
        "Commercial & Professional Svc",
        "Software & Services",
        "Energy",
        "Utilities",
        "Consumer Durables & Apparel",
        "Telecommunication Services",
        "Media and Entertainment",
        "Pharma, Biotech & Life Sciences",
        "Household & Personal Products",
        "Technology Hardware & Equipment",
    }
)


def is_corporate(row: ListingRow) -> bool:
    """Whether the row's company is a corporate issuer; an unknown sector is an error."""
    if row.sector in CORPORATE_SECTORS:
        return True
    if row.sector in SKIPPED_SECTORS:
        return False
    raise ValueError(
        f"{row.short_name}: sector {row.sector!r} is in neither CORPORATE_SECTORS nor "
        "SKIPPED_SECTORS; add it to one"
    )


# The clean-document rule (owner's request: cleanly written Arabic first, noisy documents later).
# A document is clean when no page has a garbled text layer and the text layer alone holds a
# page of each of these statements, as the project's own locator finds them (no OCR), and the
# pages without text before them are at most a short auditor's letter. A primary statement saved
# as an image sits before the notes page that names it, so a textless page between page 2 and the
# last located statement page is allowed only as one run of at most MAX_LETTER_PAGES pages
# ending right before the first located statement page, and only when the two statements are at
# most MAX_STATEMENT_GAP pages apart. Images of statements are a longer run (fails the first);
# a notes page matched for one statement lies far from the other (fails the second) or from the
# run (fails the first). Page 1 is excepted, a cover is often an image and is not a statement.
# Which clean editions are kept is `kept_editions`.
REQUIRED_STATEMENTS = frozenset({StatementType.BALANCE, StatementType.INCOME})
_STATEMENT_NAMES = {
    StatementType.BALANCE: "statement of financial position",
    StatementType.INCOME: "statement of profit or loss",
}
# An auditor's review letter is one or two pages and sits directly before the first statement.
MAX_LETTER_PAGES = 2
# The primary statements are printed consecutively, so a balance sheet and a profit or loss
# statement further apart than this means at least one was matched in the notes (the kept
# `train` files are at most 2 apart).
MAX_STATEMENT_GAP = 2
# A located page must carry the figures of a statement, not only its title (a letter that names
# the statements, or a table pasted as a picture). On the 66 kept files the best page of each
# statement had 20-136 numeric tokens (balance sheets 35-136, profit or loss 20-67); the auditor's
# letters that the locator also matched had 4-17 (the threshold is the smallest real
# value, 3 above the largest letter).
MIN_STATEMENT_AMOUNTS = 20

# A near match: token sets overlap at least this much, or one issuer key holds the other.
NEAR_MATCH_MIN_SCORE = 0.4
NEAR_MATCH_LIMIT = 3
# The Arabic edition must be at least this Arabic (share of Arabic among Arabic and Latin
# letters), the English edition at most this Arabic.
ARABIC_EDITION_MIN_ARABIC = 0.5
ENGLISH_EDITION_MAX_ARABIC = 0.5
# The period is read from the first pages with text, the language from every page that has text.
PERIOD_PAGES = 5
# A later period named on these first pages with text (the title pages) overrules a mention of
# the period end further on, such as a comparative in the next quarter's statements.
TITLE_PAGES = 2


class ListingRow(NamedTuple):
    short_name: str
    path: str
    arabic_name: str | None
    sector: str
    editions: dict[str, dict[str, str]]  # period column -> language -> pdf url


class _Anchor(NamedTuple):
    attrs: dict[str, str]
    text: str


@dataclass
class _Table:
    sector: str | None
    header: list[str] = field(default_factory=list)
    rows: list[list[list[_Anchor]]] = field(default_factory=list)  # row -> cell -> anchors


class _ListingHtml(HTMLParser):
    """Collects each table with the h2 heading above it: header cells and, per row, the anchors
    of every cell."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_Table] = []
        self._sector: str | None = None
        self._h2: list[str] | None = None
        self._th: list[str] | None = None
        self._anchor: tuple[dict[str, str], list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h2":
            self._h2 = []
        elif tag == "table":
            self.tables.append(_Table(self._sector))
        elif not self.tables:
            return
        elif tag == "tr":
            self.tables[-1].rows.append([])
        elif tag == "th":
            self._th = []
        elif tag == "td" and self.tables[-1].rows:
            self.tables[-1].rows[-1].append([])
        elif tag == "a" and self.tables[-1].rows and self.tables[-1].rows[-1]:
            self._anchor = ({k: v or "" for k, v in attrs}, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._h2 is not None:
            self._sector = "".join(self._h2).strip()
            self._h2 = None
        elif tag == "th" and self._th is not None:
            self.tables[-1].header.append("".join(self._th).strip())
            self._th = None
        elif tag == "a" and self._anchor is not None:
            attrs, text = self._anchor
            self.tables[-1].rows[-1][-1].append(_Anchor(attrs, "".join(text).strip()))
            self._anchor = None

    def handle_data(self, data: str) -> None:
        for sink in (self._h2, self._th, self._anchor[1] if self._anchor else None):
            if sink is not None:
                sink.append(data)


def parse_listing(html: str) -> list[ListingRow]:
    """Listing page -> one row per company. Fails when the page is not the expected table, so a
    sign-in page or a changed layout cannot pass as an empty listing."""
    parser = _ListingHtml()
    parser.feed(html)
    tables = [t for t in parser.tables if t.header]
    if not tables:
        raise ValueError("no company table in the listing page")
    rows: list[ListingRow] = []
    for table in tables:
        if table.header not in _LAYOUTS:
            raise ValueError(f"unexpected columns {table.header}, expected one of {_LAYOUTS}")
        if table.sector is None:
            raise ValueError("a company table has no sector heading above it")
        for cells in (c for c in table.rows if c):
            rows.append(_row(cells, table.sector, table.header))
    if not rows:
        raise ValueError("the company table has no rows")
    return rows


def _row(cells: list[list[_Anchor]], sector: str, header: list[str]) -> ListingRow:
    if len(cells) != len(header):
        raise ValueError(f"company row has {len(cells)} cells, expected {len(header)}")
    if len(cells[0]) != 1 or not cells[0][0].attrs.get("href", "").startswith("/en/tadawul/"):
        raise ValueError(f"company cell without a company link: {cells[0]}")
    company = cells[0][0]
    editions: dict[str, dict[str, str]] = {}
    arabic_name: str | None = None
    for period, anchors in zip(header[1:], cells[1:], strict=True):
        if period == _BOARD_REPORT:
            continue  # not statements; its links carry other labels (a report, a package)
        for anchor in anchors:
            language = _EDITION_LABELS.get(anchor.text)
            if language is None:
                raise ValueError(f"{company.text} {period}: unknown edition label {anchor.text!r}")
            url = anchor.attrs.get("href", "")
            if not url.startswith("https://"):
                raise ValueError(f"{company.text} {period} {anchor.text}: no link")
            if language in editions.setdefault(period, {}):
                raise ValueError(f"{company.text} {period}: two {anchor.text} editions")
            editions[period][language] = url
            if language == "ar" and arabic_name is None:
                arabic_name = (
                    anchor.attrs.get("relf-companyname") or None
                )  # the parser lowercases names
    return ListingRow(company.text, company.attrs["href"], arabic_name, sector, editions)


class _Heading(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self._text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h1" and "h1" in (dict(attrs).get("class") or "").split():
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._text is not None:
            self.headings.append("".join(self._text).strip())
            self._text = None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)


def parse_company_name(html: str) -> str:
    """Company page -> the full English name, which is its one `h1.h1` heading."""
    parser = _Heading()
    parser.feed(html)
    if len(parser.headings) != 1 or not parser.headings[0]:
        raise ValueError(f"expected one company heading, found {parser.headings}")
    return parser.headings[0]


def issuer_slug(name: str) -> str:
    """The id prefix of an issuer: its name without a bracketed alias, as lower-case words."""
    return "-".join(re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", "", name).lower()))


_LEGAL_FORM = re.compile(r"(\s+(co|company|corp|corporation|ltd|limited))+\.?$", re.IGNORECASE)


def issuer_name(full_name: str) -> str:
    """The name an entry carries: the source's full name without a trailing legal form, as the
    existing issuers are written."""
    return _LEGAL_FORM.sub("", full_name.strip())


def has_arabic(row: ListingRow, market: Market) -> bool:
    return "ar" in row.editions.get(market.column, {})


def pair_entries(
    row: ListingRow,
    issuer: str,
    *,
    market: Market,
    pool: str,
    role: str,
    languages: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """The candidates.yaml entries for a row's editions of the market's period, Arabic first;
    by default the editions the row has."""
    editions = row.editions.get(market.column, {})
    languages = languages or tuple(lang for lang in LANGUAGES if lang in editions)
    missing = [lang for lang in languages if lang not in editions]
    if missing or not languages:
        raise ValueError(f"{row.short_name}: no {market.column} edition in {missing or LANGUAGES}")
    return [
        {
            "id": f"{issuer_slug(issuer)}-{FISCAL_YEAR}-{lang}-interim",
            "issuer": issuer,
            "country": COUNTRY,
            "language": lang,
            "fiscal_year": FISCAL_YEAR,
            "period": "interim",
            "kind": "financial_statements",
            "url": editions[lang],
            "pool": pool,
            "role": role,
            "verified": False,
        }
        for lang in languages
    ]


_PLAIN = re.compile(r"[A-Za-z][A-Za-z0-9 _.()&'/-]*")


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _PLAIN.fullmatch(text) and text.lower() not in {
        "yes",
        "no",
        "true",
        "false",
        "null",
        "on",
        "off",
    }:
        return text
    return "'" + text.replace("'", "''") + "'"


def entry_line(doc: dict[str, Any]) -> str:
    """One candidates.yaml line in the file's own style."""
    line = "  - {" + ", ".join(f"{k}: {_scalar(v)}" for k, v in doc.items()) + "}"
    if yaml.safe_load(line.strip()) != [doc]:
        raise ValueError(f"entry does not survive a YAML round trip: {line}")
    return line


def _joined(name: str) -> str:
    return issuer_key(name).replace(" ", "")


def near_matches(
    issuer: str, others: list[str], aliases: list[str] | tuple[str, ...] = ()
) -> list[tuple[str, float, str]]:
    """The other issuers that may be the same company under another name: token-set overlap
    (Jaccard) of the issuer keys, or one key holding the other. A listing's short name (an
    alias, such as a brand or ticker name) counts when it is held by the other issuer's name.
    An identical key is an exact match, not a near one. Best first."""
    key = set(issuer_key(issuer).split())
    alias_keys = [set(issuer_key(a).split()) for a in aliases]
    found = []
    for other in others:
        other_key = set(issuer_key(other).split())
        if not key or not other_key or key == other_key:
            continue
        if _joined(issuer) == _joined(other):
            found.append((other, 1.0, "spacing"))
            continue
        score = len(key & other_key) / len(key | other_key)
        holds = key <= other_key or other_key <= key
        by_alias = any(a and a <= other_key for a in alias_keys)
        if score >= NEAR_MATCH_MIN_SCORE or holds or by_alias:
            reason = (
                "contains" if holds else "overlap" if score >= NEAR_MATCH_MIN_SCORE else "alias"
            )
            found.append((other, round(score, 2), reason))
    return sorted(found, key=lambda f: (f[2] != "alias", -f[1]))[:NEAR_MATCH_LIMIT]


URL_TOKEN = "IRAccessToken"  # the query parameter that carries the issuer's short name


def url_token_issuers(documents: list[dict[str, Any]]) -> dict[str, set[str]]:
    """The issuers of the documents by the short name their URLs carry (`?IRAccessToken=AME`),
    spelled as `_joined` spells a name. The token names the company whatever the issuer is
    called, so it finds an issuer recorded under a translated or abbreviated name."""
    found: dict[str, set[str]] = defaultdict(set)
    for doc in documents:
        for token in parse_qs(urlsplit(doc["url"]).query).get(URL_TOKEN, []):
            if _joined(token):
                found[_joined(token)].add(doc["issuer"])
    return found


_ARABIC_RANGES = ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def _letter_script(char: str) -> str | None:
    if not char.isalpha():
        return None
    if any(lo <= ord(char) <= hi for lo, hi in _ARABIC_RANGES):
        return "arabic"
    return "latin" if "LATIN" in unicodedata.name(char, "") else None


def script_share(text: str) -> float | None:
    """Arabic letters as a share of the Arabic and Latin letters; None when there are none."""
    counts = {"arabic": 0, "latin": 0}
    for char in text:
        script = _letter_script(char)
        if script:
            counts[script] += 1
    total = sum(counts.values())
    return counts["arabic"] / total if total else None


_EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_SEP = r"[\s,.\-/\u200e\u200f]*"
_YEAR = rf"(?<!\d){FISCAL_YEAR}م?(?!\d)"  # Arabic years may carry the م of "Gregorian"


@functools.cache
def _period_end(period: Period) -> re.Pattern[str]:
    """The period end as 31 March 2026 and March 31, 2026, and the same in the reversed word
    order that some Arabic text layers give; or the date in numbers."""
    month = "(?:" + "|".join([_MONTHS[period.month - 1], *period.arabic_month]) + ")"
    day = rf"(?<!\d){period.day}(?!\d)"
    return re.compile(
        "|".join(
            [
                f"{day}{_SEP}{month}{_SEP}{_YEAR}",
                f"{month}{_SEP}{day}{_SEP}{_YEAR}",
                f"{_YEAR}{_SEP}{month}{_SEP}{day}",
                f"{_YEAR}{_SEP}{day}{_SEP}{month}",
                rf"(?<!\d){period.day}[/.-]0?{period.month}[/.-]{FISCAL_YEAR}(?!\d)",
                rf"(?<!\d){FISCAL_YEAR}[/.-]0?{period.month}[/.-]{period.day}(?!\d)",
            ]
        )
    )


def shows_period_end(text: str, period: Period) -> bool:
    """Whether the text names the period end, in either language and any digit form. Other
    dates, and a day or month number elsewhere on the page, do not count."""
    return bool(_period_end(period).search(text.translate(_EASTERN_DIGITS).lower()))


_AFTER_PERIOD_WORD = (
    r"(?:\b(?:ended|ending|as\s+at|as\s+of)\b|المنتهي[ةه]?(?:\s+في)?|كما\s+في)[^0-9a-z]{0,4}"
)


@functools.cache
def _later_period(period: Period) -> re.Pattern[str]:
    months = "|".join(
        [*_MONTHS[period.month :], *chain.from_iterable(_ARABIC_MONTHS[period.month :])]
    )
    numbers = "|".join(f"0?{m}" for m in range(period.month + 1, 13))
    return re.compile(
        "|".join(
            [
                rf"{_AFTER_PERIOD_WORD}(?:\d{{1,2}}\W{{0,3}})?(?:{months})\W{{0,4}}(?:\d{{1,2}}\W{{1,3}})?{FISCAL_YEAR}",
                rf"{_AFTER_PERIOD_WORD}\d{{1,2}}[/.-](?:{numbers})[/.-]{FISCAL_YEAR}",
            ]
        )
    )


def names_later_period(text: str, period: Period) -> bool:
    """Whether the text gives a period end in 2026 after the period's month ("ended 30 June
    2026" for the first quarter, "as at September 30, 2026" for the half year), as the title
    pages of a later period do, in either language ("المنتهية في 30 يونيو 2026"). A report dated in
    that month, or a 2025 comparative, is not a later period."""
    return bool(
        _later_period(period).search(" ".join(text.translate(_EASTERN_DIGITS).lower().split()))
    )


def kept_editions(arabic: bool | None, english: bool | None) -> frozenset[str]:
    """Which clean editions of a company are kept (None: there is no such edition). A clean
    Arabic edition is kept on its own; an English edition only beside a clean Arabic one, so
    that new additions never widen the gap between the two languages."""
    if not arabic:
        return frozenset()
    return frozenset({"ar", "en"} if english else {"ar"})


class LocatedPage(NamedTuple):
    statement: StatementType
    page_no: int
    amounts: int  # numeric tokens on the page


class TextFacts(NamedTuple):
    arabic_share: float | None
    textful_pages: int
    garbled_pages: int
    shows_period: bool
    period_seen: str  # the first "ended <date>" phrase of the first pages, when there is one
    statements: frozenset[StatementType]  # statement types the locator finds on text pages
    # pages without text from the second up to the last located statement page
    image_pages_before_statements: tuple[int, ...]
    located: tuple[LocatedPage, ...]  # the page of each required statement, best of its range


_PERIOD_PHRASE = re.compile(r"(?:ended|ending)\s+[A-Za-z0-9 ,/.-]{0,24}?20\d\d", re.IGNORECASE)


def analyse_pages(pages: list[PageText], book: TitleBook, period: Period) -> TextFacts:
    """What a file's pages show: the script, pages whose text layer is noise, whether the first
    pages with text name the period, and which statements the project's locator finds on the
    text pages (pages without text count for nothing: no OCR runs)."""
    textful = [page for page in pages if page.mode is PageMode.TEXT]
    first = [page.text for page in textful[:PERIOD_PAGES]]
    seen = next(
        (m[0] for text in first if (m := _PERIOD_PHRASE.search(" ".join(text.split())))), ""
    )
    readable = [page for page in textful if GARBLED_TEXT_LAYER not in page.flags]
    scores = {score.page_no: score for score in (score_page(page, book) for page in readable)}
    ranges = find_ranges(list(scores.values()))
    located = tuple(
        _best_page(r, scores) for r in ranges if r.type in REQUIRED_STATEMENTS and r.rank == 1
    )
    last = max((page.page_no for page in located), default=0)
    return TextFacts(
        arabic_share=script_share("\n".join(page.text for page in textful)),
        textful_pages=len(textful),
        garbled_pages=len(textful) - len(readable),
        shows_period=any(shows_period_end(text, period) for text in first)
        and not any(names_later_period(text, period) for text in first[:TITLE_PAGES]),
        period_seen=seen,
        statements=frozenset(r.type for r in ranges),
        image_pages_before_statements=tuple(
            page.page_no
            for page in pages
            if 2 <= page.page_no <= last and page.mode is not PageMode.TEXT
        ),
        located=located,
    )


def _best_page(found: StatementRange, scores: dict[int, PageScore]) -> LocatedPage:
    """The page of a range that scores highest for its statement (the earliest on a tie): the
    first page of a range can be an auditor's letter that names the statement."""
    pages = [scores[n] for n in range(found.first_page, found.last_page + 1) if n in scores]
    best = max(pages, key=lambda score: (score.type_scores[found.type], -score.page_no))
    return LocatedPage(found.type, best.page_no, best.numeric_tokens)


def is_clean(facts: TextFacts) -> bool:
    return (
        facts.garbled_pages == 0
        and statements_problem(facts) is None
        and _only_a_letter_before(facts)
    )


def statements_problem(facts: TextFacts) -> str | None:
    """Why the located pages are not two statements with their figures, or None when they are."""
    found = {page.statement: page for page in facts.located}
    missing = [_STATEMENT_NAMES[t] for t in sorted(REQUIRED_STATEMENTS) if t not in found]
    if missing:
        return f"no text-layer page found for the {' and the '.join(missing)}"
    bare = [
        f"{_STATEMENT_NAMES[t]} (page {p.page_no}, {p.amounts} amounts)"
        for t, p in sorted(found.items())
        if p.amounts < MIN_STATEMENT_AMOUNTS
    ]
    if bare:
        return (
            f"fewer than {MIN_STATEMENT_AMOUNTS} amounts on the page found for the "
            + " and ".join(bare)
        )
    pages = sorted(page.page_no for page in found.values())
    if pages[-1] - pages[0] > MAX_STATEMENT_GAP:
        return f"the statements are found on pages {pages[0]} and {pages[-1]}, too far apart"
    return None


def _only_a_letter_before(facts: TextFacts) -> bool:
    images = facts.image_pages_before_statements
    first = min((page.page_no for page in facts.located), default=0)
    return not images or (
        len(images) <= MAX_LETTER_PAGES
        and images[-1] == first - 1
        and images[-1] - images[0] == len(images) - 1
    )


def language_matches(language: str, arabic_share: float | None) -> bool:
    if arabic_share is None:
        return False
    if language == "ar":
        return arabic_share >= ARABIC_EDITION_MIN_ARABIC
    return arabic_share <= ENGLISH_EDITION_MAX_ARABIC


class Decision(NamedTuple):
    """A reviewer's call on a company: `same` as an existing issuer (naming it; also how a
    rename is recorded), `different` (from the issuers it was compared with, `against`), or
    `exclude` (a doubt that stays open), with the reason, which goes in the report."""

    decision: str
    issuer: str | None
    reason: str
    against: tuple[str, ...] = ()


class NearRow(NamedTuple):
    new: str
    existing: str
    score: float
    decision: str  # same | different | excluded
    reason: str


@dataclass
class Plan:
    funnel: list[tuple[str, int, int]] = field(default_factory=list)  # label, issuers, documents
    entries: list[dict[str, Any]] = field(default_factory=list)
    near: list[NearRow] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)
    skipped_sectors: Counter[str] = field(default_factory=Counter)
    skipped_golden: list[str] = field(default_factory=list)
    skipped_existing: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class _Existing:
    issuer: str
    pool: str
    role: str
    docs: list[dict[str, Any]]


def build_plan(
    rows: list[ListingRow],
    names: dict[str, str],
    documents: list[dict[str, Any]],
    golden_keys: set[str],
    decisions: dict[str, Decision],
    market: Market,
) -> Plan:
    """Which entries to add, with the count at every step. `names` maps a company path to its
    full English name; `documents` are the entries already in candidates.yaml, those of every
    market (a company can move between them)."""
    plan = Plan()

    def step(label: str, group: list[ListingRow]) -> None:
        plan.funnel.append(
            (label, len(group), sum(len(r.editions.get(market.column, {})) for r in group))
        )

    step("rows in the listing", rows)
    arabic = [r for r in rows if has_arabic(r, market)]
    step(f"with an Arabic {market.column} edition", arabic)
    corporate = [r for r in arabic if is_corporate(r)]
    plan.skipped_sectors.update(r.sector for r in arabic if r not in corporate)
    step("after the sector filter", corporate)
    candidates = [r for r in corporate if issuer_key(names[r.path]) not in golden_keys]
    plan.skipped_golden = [names[r.path] for r in corporate if r not in candidates]
    step("after the golden issuers", candidates)

    taken_ids = {doc["id"] for doc in documents}
    existing: dict[str, _Existing] = {}
    for doc in documents:
        found = existing.setdefault(
            issuer_key(doc["issuer"]), _Existing(doc["issuer"], doc["pool"], doc["role"], [])
        )
        found.docs.append(doc)
    by_url = {doc["url"]: issuer_key(doc["issuer"]) for doc in documents}
    key_count = Counter(issuer_key(names[r.path]) for r in candidates)
    existing_names = sorted({e.issuer for e in existing.values()})
    by_token = url_token_issuers(documents)

    groups: dict[str, list[ListingRow]] = defaultdict(list)
    undecided: list[str] = []
    for r in candidates:
        name = names[r.path]
        key = issuer_key(name)
        linked = {by_url[u] for urls in r.editions.values() for u in urls.values() if u in by_url}
        if len(linked) > 1 or (linked and key in existing and linked != {key}):
            raise ValueError(f"{name}: its urls belong to other issuers {sorted(linked)}")
        if key_count[key] > 1:
            plan.excluded.append((name, "same issuer key as another new company"))
            groups["same key as another new company"].append(r)
            continue
        match = next(iter(linked), key if key in existing else None)
        if match is not None:
            groups["same issuer as an existing one (name or url)"].append(r)
            _add_pair(plan, r, existing[match], taken_ids, market)
            continue
        others = existing_names + [names[o.path] for o in candidates if o is not r]
        near = near_matches(name, others, [r.short_name])
        near += [
            (issuer, 1.0, "url token")
            for issuer in sorted(by_token.get(_joined(r.short_name), ()))
            if issuer not in {n for n, _, _ in near}
        ]
        decision = decisions.get(name)
        if decision is None:
            if near:
                undecided.append(f"{name} (near {', '.join(o for o, _, _ in near)})")
            else:
                groups["new issuer, no near match"].append(r)
                _add_new(plan, r, issuer_name(name), market, taken_ids)
            continue
        if decision.decision == "same":
            target = existing.get(issuer_key(decision.issuer or ""))
            if target is None:
                raise ValueError(f"{name}: decided same as {decision.issuer!r}, not an issuer")
            groups["decided same as an existing issuer"].append(r)
            plan.near += [
                NearRow(
                    name,
                    o,
                    sc,
                    "same" if issuer_key(o) == issuer_key(decision.issuer or "") else "different",
                    decision.reason,
                )
                for o, sc, _ in near
            ]
            _add_pair(plan, r, target, taken_ids, market)
        elif decision.decision == "exclude":
            plan.excluded.append((name, decision.reason))
            groups["decided to leave out"].append(r)
            plan.near += [NearRow(name, o, sc, "excluded", decision.reason) for o, sc, _ in near]
        elif decision.decision == "different":
            uncovered = sorted({o for o, _, _ in near} - set(decision.against))
            if uncovered:
                raise ValueError(
                    f"{name}: decided different, but it was not compared with {uncovered}; "
                    f"review it and extend `against` in {DECISIONS.name}"
                )
            groups["decided different"].append(r)
            plan.near += [NearRow(name, o, sc, "different", decision.reason) for o, sc, _ in near]
            _add_new(plan, r, issuer_name(name), market, taken_ids)
        else:
            raise ValueError(
                f"{name}: decision must be same, different or exclude, got {decision.decision!r}"
            )
    if undecided:
        raise ValueError(
            f"{len(undecided)} companies nearly match an existing issuer and have no decision in "
            f"{DECISIONS.name}; add one (same, different or exclude, with a reason) for: "
            + "; ".join(undecided)
        )
    for label, group in groups.items():
        step(label, group)
    return plan


def _add_new(plan: Plan, row: ListingRow, name: str, market: Market, taken_ids: set[str]) -> None:
    entries = pair_entries(row, name, market=market, pool=assign_pool(name), role="corporate")
    plan.entries += _unique_ids(entries, taken_ids)


def _unique_ids(entries: list[dict[str, Any]], taken_ids: set[str]) -> list[dict[str, Any]]:
    """An entry whose id is taken (by another issuer's, or by the same issuer's edition of
    another period) takes the first four characters of its file name as an id suffix, as other
    repeated ids do."""
    for entry in entries:
        if entry["id"] in taken_ids:
            entry["id"] += "-" + entry["url"].rsplit("/", 1)[-1][:4]
        taken_ids.add(entry["id"])
    return entries


def _add_pair(
    plan: Plan, row: ListingRow, issuer: _Existing, taken_ids: set[str], market: Market
) -> None:
    """Entries for the editions of the market's period an existing issuer lacks, under its own
    name, pool and role. A 2026 edition of another period does not count as this period's."""
    if issuer.role == "negative_control":
        plan.skipped_existing.append((issuer.issuer, "both", "negative control"))
        return
    editions = row.editions[market.column]
    other_periods = {
        url
        for column, urls in row.editions.items()
        if column != market.column
        for url in urls.values()
    }
    urls = {doc["url"] for doc in issuer.docs}
    wanted = []
    for language in (lang for lang in LANGUAGES if lang in editions):
        recent = [
            d
            for d in issuer.docs
            if d["fiscal_year"] == FISCAL_YEAR
            and d["period"] == "interim"
            and d["language"] == language
        ]
        if editions[language] in urls:
            plan.skipped_existing.append((issuer.issuer, language, "url already in candidates"))
        elif any(d["url"] not in other_periods for d in recent):
            plan.skipped_existing.append(
                (issuer.issuer, language, f"has a {FISCAL_YEAR} interim edition of unknown quarter")
            )
        else:
            wanted.append(language)
    if not wanted:
        return
    entries = pair_entries(
        row,
        issuer.issuer,
        market=market,
        pool=issuer.pool,
        role=issuer.role,
        languages=tuple(wanted),
    )
    plan.entries += _unique_ids(entries, taken_ids)


_SECTION = re.compile(r"^  # ---- (\w+) ----$")
_ENTRY_ID = re.compile(r"^  - \{id: ([^,]+),")


def _sort_key(line: str) -> tuple[str, str]:
    doc = yaml.safe_load(line.strip())[0]
    return doc["issuer"].lower(), doc["id"]


def add_entries(text: str, docs: list[dict[str, Any]]) -> str:
    """Insert each entry in its pool's section, before the first entry of a later issuer (then
    id), leaving the rest of the file as it is."""
    lines = text.splitlines(keepends=True)
    for doc in docs:
        start = next(
            (
                i
                for i, ln in enumerate(lines)
                if (m := _SECTION.match(ln.rstrip())) and m[1] == doc["pool"]
            ),
            None,
        )
        if start is None:
            raise ValueError(f"{doc['id']}: no section for pool {doc['pool']!r}")
        end = next(
            (i for i in range(start + 1, len(lines)) if _SECTION.match(lines[i].rstrip())),
            len(lines),
        )
        entries = [i for i in range(start + 1, end) if _ENTRY_ID.match(lines[i])]
        key = (doc["issuer"].lower(), doc["id"])
        at = next((i for i in entries if _sort_key(lines[i]) > key), None)
        if at is None:
            at = entries[-1] + 1 if entries else start + 1
        lines.insert(at, entry_line(doc) + "\n")
    return "".join(lines)


def remove_entries(text: str, ids: set[str]) -> str:
    """Drop the entries with these ids; an id that is not in the file is an error."""
    kept, found = [], set()
    for line in text.splitlines(keepends=True):
        m = _ENTRY_ID.match(line)
        if m and m[1] in ids:
            found.add(m[1])
        else:
            kept.append(line)
    if found != ids:
        raise ValueError(f"not in the candidates file: {sorted(ids - found)}")
    return "".join(kept)


class Requests:
    """Pages of the site, each fetched once into a cache directory and counted."""

    def __init__(self, cache: Path, polite: Politeness) -> None:
        self.cache = cache
        self.polite = polite
        self.made = 0

    def page(self, url: str, name: str, parse: Callable[[str], T]) -> T:
        """The parsed page; a page that is downloaded is cached only once it has parsed."""
        path = self.cache / name
        if path.exists():
            return parse(path.read_text(encoding="utf-8"))
        if not self.polite.allowed(url):
            raise PermissionError(f"{url}: {self.polite.refusal(url)}")
        self.made += 1
        data = self.polite.get(url)
        parsed = parse(data.decode("utf-8"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return parsed


def _load_decisions() -> dict[str, Decision]:
    raw = corpus.load_yaml(DECISIONS) or {}
    return {
        name: Decision(
            entry["decision"],
            entry.get("issuer"),
            entry["reason"],
            tuple(entry.get("against", ())),
        )
        for name, entry in raw.items()
    }


def _report(plan: Plan) -> str:
    lines = [
        "# Near matches between Argaam companies and existing issuers",
        "",
        "| New company | Existing issuer | Score | Decision | Reason |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {n.new} | {n.existing} | {n.score} | {n.decision} | {n.reason} |" for n in plan.near
    ]
    lines += ["", "## Left out", ""]
    lines += [f"- {name}: {reason}" for name, reason in plan.excluded] or ["- none"]
    lines += ["", "## Existing issuers: editions not added", ""]
    lines += [f"- {i} ({lang}): {why}" for i, lang, why in plan.skipped_existing] or ["- none"]
    return "\n".join(lines) + "\n"


def cmd_collect(args: argparse.Namespace) -> int:
    polite = Politeness()
    requests = Requests(args.cache, polite)
    market = MARKETS[args.market]
    rows = requests.page(market.listing_url, "listing.html", parse_listing)
    wanted = [r for r in rows if has_arabic(r, market) and is_corporate(r)]
    names = {
        r.path: requests.page(
            SITE + r.path, f"company/{r.path.split('/')[-1]}.html", parse_company_name
        )
        for r in wanted
    }
    # Documents set aside are as known as the collected ones: not proposed again.
    documents = corpus.load_yaml(corpus.CANDIDATES)["documents"]
    documents += set_aside_documents()
    golden_keys, _ = corpus.golden_index()
    plan = build_plan(rows, names, documents, golden_keys, _load_decisions(), market)

    for label, issuers, editions in plan.funnel:
        print(f"{label:<48} issuers={issuers:<4} {market.column} editions={editions}")
    print(f"skipped by sector: {dict(plan.skipped_sectors)}")
    added_issuers = {e["issuer"] for e in plan.entries}
    print(f"entries to add: {len(plan.entries)} for {len(added_issuers)} issuers")
    print(f"requests to the site: {requests.made}" + (" (and robots.txt)" if requests.made else ""))
    args.report.write_text(_report(plan), encoding="utf-8")
    if args.write:
        corpus.CANDIDATES.write_text(
            add_entries(corpus.CANDIDATES.read_text(encoding="utf-8"), plan.entries),
            encoding="utf-8",
        )
        (args.cache / "added.yaml").write_text(
            yaml.safe_dump([e["id"] for e in plan.entries]), encoding="utf-8"
        )
    return 0


DEFERRED = corpus.ROOT / "eval/corpus/deferred.yaml"
DEFERRED_HEADER = """\
# Documents set aside, with the reason (the same fields as candidates.yaml plus `reason`).
# They are not in candidates.yaml or fetched.yaml, their PDFs are not kept, and `collect` does
# not propose them again. An issuer keeps the pool recorded here, unless a group relationship
# or a rename found later puts it in another (rule 1 in README.md).

documents:
"""
_LANGUAGE_NAMES = {"ar": "Arabic", "en": "English"}


def _pages(count: int) -> str:
    return f"{count} page" + ("" if count == 1 else "s")


@dataclass
class DocCheck:
    doc: dict[str, Any]
    clean: bool = False
    text: TextFacts | None = None
    problems: list[str] = field(default_factory=list)
    existing: bool = False  # a corpus document that pairs with an added one, judged but kept


def _share(arabic_share: float | None) -> str:
    return "no letters" if arabic_share is None else f"{arabic_share:.0%} Arabic letters"


def check_document(
    doc: dict[str, Any],
    entry: dict[str, Any] | None,
    config: IngestConfig,
    book: TitleBook,
    period: Period,
) -> DocCheck:
    """Language, period and clean checks for one downloaded file. A file that was not downloaded
    stops the run: it says nothing about the document."""
    path = corpus.STORE / doc["pool"] / f"{doc['id']}.pdf"
    if not entry or entry.get("status") == "failed" or not path.exists():
        error = (entry or {}).get("error", "no entry in fetched.yaml")
        raise ValueError(
            f"{doc['id']}: not downloaded ({error}); run `corpus.py fetch --id {doc['id']}`"
        )
    result = DocCheck(doc)
    text = result.text = analyse_pages(read_pages(path, config, None), book, period)
    result.clean = is_clean(text)
    # A file with no text, or whose text layer is noise, cannot be read for its language or
    # period; the clean rule sets such a file aside, and the other edition still shows the period.
    if text.textful_pages == 0 or text.garbled_pages:
        return result
    language = _LANGUAGE_NAMES[doc["language"]]
    if not language_matches(doc["language"], text.arabic_share):
        result.problems.append(
            f"the text of the {language} edition is not {language} ({_share(text.arabic_share)}): "
            "a wrong-language file or an undecodable text layer"
        )
    if not text.shows_period:
        result.problems.append(period_problem(text, period))
    return result


def period_problem(text: TextFacts, period: Period) -> str:
    """Why a file does not show the period: the reader found another period end in its first
    pages (`period_seen`), or found none, which says nothing of what the file holds."""
    seen = (
        f"another period is shown, {text.period_seen!r}"
        if text.period_seen
        else "the reader found no period end in them"
    )
    return f"the first pages do not show {period.label}: {seen}"


def _why_not_clean(check: DocCheck) -> str:
    text = check.text
    assert text
    reasons = []
    if text.garbled_pages:
        reasons.append(f"{_pages(text.garbled_pages)} with a garbled text layer")
    problem = statements_problem(text)
    if problem:
        reasons.append(problem)
    elif not _only_a_letter_before(text):
        images = text.image_pages_before_statements
        reasons.append(
            f"{_pages(len(images))} without a text layer (from page {images[0]}) before the "
            "statements, which may be images of them"
        )
    return f"{_LANGUAGE_NAMES[check.doc['language']]} edition: {', '.join(reasons)}"


class Verdict(NamedTuple):
    """What becomes of one document: `kept`, or set aside as `wrong` (another period or the wrong
    language), `noisy` (not clean) or `no_arabic` (an English edition with no Arabic edition of
    the same statements to keep it beside), with the reason."""

    kind: str
    reason: str


def judge(checks: list[DocCheck]) -> dict[str, Verdict]:
    """One issuer's documents -> a verdict for each. A period or language problem in any edition
    sets the whole company aside: a file that shows another period says the company's slot holds
    that period, whichever edition is clean. Otherwise which clean editions are kept is
    `kept_editions`, and a document that is not kept is set aside with the reasons of the
    editions that are not good: when the Arabic edition is kept that is the English edition's
    alone."""
    by_language = {c.doc["language"]: c for c in checks}
    clean = {lang: c.clean for lang, c in by_language.items()}
    kept = (
        frozenset()
        if any(c.problems for c in checks)
        else kept_editions(clean.get("ar"), clean.get("en"))
    )
    left_out = _not_kept(checks)  # every reason lies with an edition that is not kept
    return {
        c.doc["id"]: Verdict("kept", "") if c.doc["language"] in kept else left_out for c in checks
    }


def _not_kept(checks: list[DocCheck]) -> Verdict:
    problems = [f"{c.doc['id']}: {p}" for c in checks for p in c.problems]
    if problems:
        return Verdict("wrong", "; ".join(problems))
    reasons = [_why_not_clean(c) for c in checks if not c.clean]
    if not reasons:
        return Verdict(
            "no_arabic",
            "English edition: no Arabic edition of the same statements was judged; an English "
            "edition is kept only beside a clean Arabic one",
        )
    unverified = [
        c.doc["id"]
        for c in checks
        if c.text and (c.text.textful_pages == 0 or c.text.garbled_pages)
    ]
    if unverified:
        reasons.append(f"language and period not checkable in {', '.join(unverified)}")
    return Verdict("noisy", "; ".join(reasons))


def counterparts(
    docs: list[dict[str, Any]],
    rows: list[ListingRow],
    candidates: list[dict[str, Any]],
    market: Market,
) -> list[dict[str, Any]]:
    """The corpus documents that are the other edition of the given documents (by the listing's
    URLs in the market's column) and are not among them: an edition that relies on one is
    judged beside it."""
    editions = {
        u: row.editions[market.column]
        for row in rows
        for u in row.editions.get(market.column, {}).values()
    }
    taken = {d["id"] for d in docs}
    wanted = {u for d in docs for u in editions.get(d["url"], {}).values()}
    return [d for d in candidates if d["url"] in wanted and d["id"] not in taken]


def _located(text: TextFacts | None) -> str:
    if text is None:
        return ""
    return " ".join(f"{p.statement.value} p{p.page_no} amounts {p.amounts}" for p in text.located)


def cmd_screen(args: argparse.Namespace) -> int:
    added = set(corpus.load_yaml(args.cache / "added.yaml"))
    candidates = corpus.load_yaml(corpus.CANDIDATES)["documents"]
    set_aside = set_aside_documents()
    if args.reopen:
        reopened = [
            {k: v for k, v in d.items() if k != "reason"} for d in set_aside if d["id"] in added
        ]
        corpus.CANDIDATES.write_text(
            add_entries(corpus.CANDIDATES.read_text(encoding="utf-8"), reopened), encoding="utf-8"
        )
        write_set_aside([d for d in set_aside if d["id"] not in added])
        print(f"{len(reopened)} documents are back in candidates.yaml; run fetch --id next")
        return 0
    documents = {d["id"]: d for d in candidates}
    fetched = (corpus.load_yaml(corpus.FETCHED) or {}).get("documents", {})
    config, book = load_config(), load_title_book()
    market = MARKETS[args.market]
    rows = parse_listing((args.cache / "listing.html").read_text(encoding="utf-8"))
    # Documents set aside by an earlier run are no longer candidates and are not checked again.
    checked = [documents[i] for i in sorted(added & documents.keys())]
    by_issuer: dict[str, list[DocCheck]] = defaultdict(list)
    for doc in checked:
        by_issuer[doc["issuer"]].append(
            check_document(doc, fetched.get(doc["id"]), config, book, market.period)
        )
    for doc in counterparts(checked, rows, candidates, market):
        found = check_document(doc, fetched.get(doc["id"]), config, book, market.period)
        found.existing = True
        by_issuer[doc["issuer"]].append(found)

    all_checks = [c for checks in by_issuer.values() for c in checks]
    for c in all_checks:
        detail = (
            f"clean {c.clean} garbled pages {c.text.garbled_pages} {_located(c.text)} "
            f"period {c.text.shows_period}"
            if c.text
            else "no measurement"
        )
        print(
            f"{c.doc['id']:<60} {'existing ' if c.existing else ''}{detail} {'; '.join(c.problems)}"
        )

    verdicts: dict[str, Verdict] = {}
    for checks in by_issuer.values():
        verdicts |= judge(checks)
    for kind in ("kept", "wrong", "noisy", "no_arabic"):
        chosen = [c for c in all_checks if not c.existing and verdicts[c.doc["id"]].kind == kind]
        print(f"{kind}: {len({c.doc['issuer'] for c in chosen})} issuers, {len(chosen)} documents")
        if kind != "kept":
            for c in chosen:
                print(f"  {c.doc['id']}: {verdicts[c.doc['id']].reason}")
    if not args.apply:
        return 0

    leaving = [c for c in all_checks if not c.existing and verdicts[c.doc["id"]].kind != "kept"]
    out_ids = {c.doc["id"] for c in leaving}
    now_set_aside = [{**c.doc, "reason": verdicts[c.doc["id"]].reason} for c in leaving]
    corpus.CANDIDATES.write_text(
        remove_entries(corpus.CANDIDATES.read_text(encoding="utf-8"), out_ids),
        encoding="utf-8",
    )
    corpus.write_fetched({k: v for k, v in fetched.items() if k not in out_ids})
    for c in leaving:
        (corpus.STORE / c.doc["pool"] / f"{c.doc['id']}.pdf").unlink(missing_ok=True)
    write_set_aside(set_aside + now_set_aside)
    return 0


def set_aside_documents() -> list[dict[str, Any]]:
    """The documents in deferred.yaml; none when the file is missing or holds no entry."""
    if not DEFERRED.exists():
        return []
    documents: list[dict[str, Any]] | None = corpus.load_yaml(DEFERRED)["documents"]
    return documents or []


def write_set_aside(documents: list[dict[str, Any]]) -> None:
    ordered = sorted(documents, key=lambda d: (d["issuer"].lower(), d["id"]))
    DEFERRED.write_text(
        DEFERRED_HEADER + "".join(entry_line(d) + "\n" for d in ordered), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    market_help = f"which listing to read: {', '.join(MARKETS)}"
    collect = sub.add_parser("collect", help="read the listing and add candidate editions")
    collect.add_argument("--market", choices=sorted(MARKETS), required=True, help=market_help)
    collect.add_argument("--cache", type=Path, required=True, help="where fetched pages are kept")
    collect.add_argument("--report", type=Path, required=True, help="near-match report to write")
    collect.add_argument("--write", action="store_true", help="add the entries to candidates.yaml")
    collect.set_defaults(func=cmd_collect)
    screen = sub.add_parser("screen", help="check the downloaded files and set the failures aside")
    screen.add_argument("--market", choices=sorted(MARKETS), required=True, help=market_help)
    screen.add_argument("--cache", type=Path, required=True, help="the collect cache directory")
    screen.add_argument("--apply", action="store_true", help="edit the corpus files, else report")
    screen.add_argument(
        "--reopen", action="store_true", help="first move the cache's set-aside documents back"
    )
    screen.set_defaults(func=cmd_screen)
    args = parser.parse_args(argv)
    corpus.use_system_trust()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
