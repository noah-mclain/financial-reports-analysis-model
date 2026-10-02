"""The Argaam listing parsers, candidate building and the clean-document rule."""

from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

import argaam_listing
import fra_ingest.locate
from argaam_listing import (
    MIN_STATEMENT_AMOUNTS,
    REQUIRED_STATEMENTS,
    SKIPPED_SECTORS,
    Decision,
    ListingRow,
    LocatedPage,
    TextFacts,
    add_entries,
    analyse_pages,
    build_plan,
    entry_line,
    is_clean,
    is_clean_pair,
    is_corporate,
    issuer_name,
    issuer_slug,
    language_matches,
    names_later_period,
    near_matches,
    pair_entries,
    parse_company_name,
    parse_listing,
    remove_entries,
    script_share,
    shows_period_end,
)
from corpus import assign_pool, issuer_key
from fra_core.schemas import PageMode, StatementType, TextSource
from fra_ingest.locate import load_title_book
from fra_ingest.pages import text_layer_is_garbled
from fra_ingest.results import PageText

FIXTURES = Path(__file__).parent / "fixtures"
S3 = "https://argaamplus.s3.amazonaws.com/"


def rows() -> dict[str, ListingRow]:
    html = (FIXTURES / "argaam_listing_trimmed.html").read_text(encoding="utf-8")
    return {row.short_name: row for row in parse_listing(html)}


def test_listing_row_with_both_editions() -> None:
    row = rows()["RIBL"]
    assert row.path == "/en/tadawul/tasi/ribl"
    assert row.arabic_name == "الرياض"
    assert row.sector == "Banks"
    assert row.editions["Q1"] == {
        "ar": S3 + "a73ecda0-035e-4da9-bf09-05516aed6dc4.pdf",
        "en": S3 + "41f22173-5026-4885-b5d9-ab72e9ab35d0.pdf",
    }
    assert set(row.editions) == {"Q1", "Q2"}


def test_listing_row_with_annual_filled() -> None:
    row = rows()["ATAA"]
    assert row.sector == "Consumer Services"
    assert set(row.editions) == {"Q1", "Q2", "Q3", "Q4", "Annual"}
    assert set(row.editions["Annual"]) == {"ar", "en"}


def test_listing_row_with_one_edition_only() -> None:
    row = rows()["LADUN"]
    assert row.sector == "Real Estate Mgmt & Dev't"
    assert set(row.editions["Q1"]) == {"ar"}


def test_listing_row_with_an_empty_cell_has_no_such_period() -> None:
    row = rows()["AL MAATHER REIT"]
    assert "Q1" not in row.editions
    assert row.sector == "REITs"
    assert row.arabic_name == "المعذر-ريت"


def test_listing_that_is_not_the_table_fails_loudly() -> None:
    with pytest.raises(ValueError, match="no company table"):
        parse_listing("<html><body>Please sign in</body></html>")


def test_listing_with_other_columns_fails_loudly() -> None:
    html = (FIXTURES / "argaam_listing_trimmed.html").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        parse_listing(html.replace(">Board Report<", ">Notes<"))


def test_reit_tables_report_halves_instead_of_q2_and_q4() -> None:
    html = (FIXTURES / "argaam_listing_trimmed.html").read_text(encoding="utf-8")
    halves = html.replace(">Q2<", ">First half<").replace(">Q4<", ">Second half<")
    by_name = {r.short_name: r for r in parse_listing(halves)}
    assert set(by_name["RIBL"].editions) == {"Q1", "First half"}


def test_board_report_links_are_not_statements_and_are_ignored() -> None:
    html = (FIXTURES / "argaam_listing_trimmed.html").read_text(encoding="utf-8")
    link = f'<a target="_blank" href="{S3}board.pdf">Advanced Package</a>'
    marked = html.replace("<span>-</span>", link, 1)  # the first empty cell is RIBL's Q3
    with pytest.raises(ValueError, match="edition"):
        parse_listing(marked)
    last = html.rsplit("</tr>", 1)
    board = last[0].rsplit("<span>-</span>", 1)
    parsed = parse_listing(board[0] + link + board[1] + "</tr>" + last[1])
    assert "Board Report" not in parsed[-1].editions


def test_listing_with_an_unknown_edition_label_fails_loudly() -> None:
    html = (FIXTURES / "argaam_listing_trimmed.html").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="edition"):
        parse_listing(html.replace(">Ar</a>", ">Fr</a>", 1))


def test_company_name_comes_from_the_page_heading() -> None:
    html = (FIXTURES / "argaam_company_page_trimmed.html").read_text(encoding="utf-8")
    assert parse_company_name(html) == "Jarir Marketing Co."


def test_company_page_without_a_heading_fails_loudly() -> None:
    with pytest.raises(ValueError, match="heading"):
        parse_company_name("<html><title>x</title></html>")


@pytest.mark.parametrize(
    ("full", "issuer"),
    [
        ("Jarir Marketing Co.", "Jarir Marketing"),
        ("Saudi Basic Industries Corp.", "Saudi Basic Industries"),
        ("Almarai Company", "Almarai"),
        ("Saudi Company for Hardware", "Saudi Company for Hardware"),
        ("Savola Group", "Savola Group"),
        ("Dallah Healthcare Holding Company Ltd.", "Dallah Healthcare Holding"),
    ],
)
def test_issuer_name_drops_a_trailing_legal_form(full: str, issuer: str) -> None:
    assert issuer_name(full) == issuer


def test_skipped_sectors_are_the_financial_ones() -> None:
    assert {"Banks", "Insurance", "Financial Services", "REITs", "ETFs", "CEFs"} == SKIPPED_SECTORS


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Yanbu National Petrochemical (Yansab)", "yanbu-national-petrochemical"),
        ("Saudi Company for Hardware", "saudi-company-for-hardware"),
        ("Dar Al Majed Real Estate Co.", "dar-al-majed-real-estate-co"),
    ],
)
def test_issuer_slug_matches_the_existing_id_convention(name: str, slug: str) -> None:
    assert issuer_slug(name) == slug


def test_pair_entries_follow_the_candidates_format() -> None:
    row = rows()["RIBL"]
    ar, en = pair_entries(row, "Riyad Bank", pool="train", role="corporate")
    assert ar["id"] == "riyad-bank-2026-ar-interim"
    assert en["id"] == "riyad-bank-2026-en-interim"
    assert (ar["language"], en["language"]) == ("ar", "en")
    assert ar["url"] == row.editions["Q1"]["ar"]
    for doc in (ar, en):
        assert doc["issuer"] == "Riyad Bank"
        assert (doc["country"], doc["fiscal_year"], doc["period"]) == ("SA", 2026, "interim")
        assert (doc["kind"], doc["pool"], doc["role"], doc["verified"]) == (
            "financial_statements",
            "train",
            "corporate",
            False,
        )


def test_pair_entries_refuse_a_row_without_both_q1_editions() -> None:
    with pytest.raises(ValueError, match="LADUN"):
        pair_entries(rows()["LADUN"], "Ladun", pool="train", role="corporate")


@pytest.mark.parametrize(
    "issuer", ["Riyad Bank", "Yanbu National Petrochemical (Yansab)", "A, B & C"]
)
def test_entry_line_is_one_line_of_valid_yaml(issuer: str) -> None:
    ar, _ = pair_entries(rows()["RIBL"], issuer, pool="train", role="corporate")
    line = entry_line(ar)
    assert "\n" not in line and line.startswith("  - {id: ")
    assert "kind: financial_statements," in line  # plain, as the existing lines are
    assert yaml.safe_load(line.strip()) == [ar]


def test_near_matches_find_the_same_company_under_another_name() -> None:
    existing = [
        "Yanbu National Petrochemical (Yansab)",
        "Savola Group",
        "Orascom Investment Holding",
    ]
    found = near_matches("Yanbu National Petrochemical Co.", existing)
    assert [name for name, _, _ in found] == ["Yanbu National Petrochemical (Yansab)"]
    assert found[0][1] >= 0.7


def test_near_matches_flag_containment_and_ignore_unrelated() -> None:
    existing = ["Orascom Construction", "Jarir Marketing"]
    assert [n for n, _, _ in near_matches("Orascom Construction Industries", existing)] == [
        "Orascom Construction"
    ]
    assert near_matches("Savola Group", existing) == []


def test_the_listing_short_name_finds_an_issuer_recorded_under_its_brand() -> None:
    existing = ["Luberef", "Savola Group"]
    assert near_matches("Saudi Aramco Base Oil Co.", existing) == []
    found = near_matches("Saudi Aramco Base Oil Co.", existing, aliases=["LUBEREF"])
    assert [(name, why) for name, _, why in found] == [("Luberef", "alias")]


@pytest.mark.parametrize(
    ("new", "old"),
    [
        ("Alandalus Property", "Al Andalus Property"),
        ("Lazurde Company for Jewelry", "L'azurde Company for Jewelry"),
    ],
)
def test_names_that_differ_only_in_spacing_or_punctuation_are_near_matches(
    new: str, old: str
) -> None:
    assert near_matches(new, [old, "Savola Group"]) == [(old, 1.0, "spacing")]


def test_exact_issuer_key_is_not_a_near_match() -> None:
    assert near_matches("ALMARAI CO.", ["Almarai Company"]) == []


def test_script_share_counts_letters_of_each_script() -> None:
    assert script_share("الأرباح 12 net profit") == pytest.approx(7 / 16)
    assert script_share("net profit") == 0.0
    assert script_share("12 345 ...") is None


@pytest.mark.parametrize(
    "text",
    [
        "Interim condensed statements for the three months ended 31 March 2026",
        "For the period ended March 31, 2026 (unaudited)",
        "31 مارس 2026",
        "٣١ مارس ٢٠٢٦",
        "للفترة المنتهية في 31/03/2026",
        "2026-03-31",
        "2026 مارس 31",
        "2026م مارس 31",
        "31 مارس 2026م",
    ],
)
def test_period_is_found_in_either_language_and_digit_form(text: str) -> None:
    assert shows_period_end(text)


@pytest.mark.parametrize(
    "text",
    [
        "31 December 2025",
        "30 June 2026",
        "Page 31 of 2026 filings",
        "March 2025 31",
        "As of 30 June 2026 Note 3 31 December 2025",
        "31 March 2025 and 2026 forecast",
    ],
)
def test_other_dates_are_not_the_period(text: str) -> None:
    assert not shows_period_end(text)


def test_a_pair_is_clean_only_when_both_editions_are() -> None:
    assert is_clean_pair(True, True)
    assert not is_clean_pair(False, True)
    assert not is_clean_pair(True, False)


def test_clean_rule_applies_to_the_edition_that_is_there() -> None:
    assert is_clean_pair(True, None)
    assert not is_clean_pair(None, False)


# ---- which companies become candidates ----


def row(short: str, sector: str = "Materials", q1: tuple[str, ...] = ("ar", "en")) -> ListingRow:
    editions = {"Q1": {lang: f"{S3}{short.lower()}-{lang}.pdf" for lang in q1}} if q1 else {}
    return ListingRow(short, f"/en/tadawul/tasi/{short.lower()}", None, sector, editions)


def existing_doc(
    issuer: str,
    lang: str,
    *,
    pool: str = "train",
    role: str = "corporate",
    year: int = 2025,
    period: str = "annual",
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{issuer_slug(issuer)}-{year}-{lang}-{period}",
        "issuer": issuer,
        "language": lang,
        "fiscal_year": year,
        "period": period,
        "pool": pool,
        "role": role,
        "url": url or f"https://old.example/{issuer_slug(issuer)}-{lang}-{year}.pdf",
    }


NAMES = {
    "/en/tadawul/tasi/new1": "Gulf Cement Co.",
    "/en/tadawul/tasi/bank1": "Some Bank",
    "/en/tadawul/tasi/almarai": "Almarai Co.",
    "/en/tadawul/tasi/jarir": "Jarir Marketing Co.",
    "/en/tadawul/tasi/hold": "Held Back Co.",
    "/en/tadawul/tasi/onlyar": "Only Arabic Co.",
    "/en/tadawul/tasi/bnk": "Existing Bank Co.",
    "/en/tadawul/tasi/yansab": "Yanbu National Petrochemical Co.",
    "/en/tadawul/tasi/siig": "Saudi Industrial Investment Group",
    "/en/tadawul/tasi/mystery": "Saudi Industrial Mystery",
}
GOLDEN_KEYS = {issuer_key("Almarai Company")}


def plan_for(
    rows_: list[ListingRow],
    docs: list[dict[str, Any]],
    decisions: dict[str, Decision] | None = None,
):  # type: ignore[no-untyped-def]
    return build_plan(rows_, NAMES, docs, GOLDEN_KEYS, decisions or {})


def test_new_corporate_issuer_gets_a_pair_in_the_hashed_pool() -> None:
    plan = plan_for([row("new1")], [])
    assert [e["id"] for e in plan.entries] == [
        "gulf-cement-2026-ar-interim",
        "gulf-cement-2026-en-interim",
    ]
    assert {e["pool"] for e in plan.entries} == {assign_pool("Gulf Cement")}
    assert {e["issuer"] for e in plan.entries} == {"Gulf Cement"}


def test_funnel_and_exclusions_are_counted_step_by_step() -> None:
    rows_ = [
        row("new1"),
        row("bank1", "Banks"),
        row("almarai"),
        row("onlyar", q1=("ar",)),
        row("hold", q1=()),
    ]
    plan = plan_for(rows_, [])
    steps = {label: (issuers, documents) for label, issuers, documents in plan.funnel}
    assert steps["rows in the listing"] == (5, 7)  # editions of Q1 listed
    assert steps["with both Q1 editions"][0] == 3
    assert steps["after the sector filter"][0] == 2
    assert steps["after the golden issuers"][0] == 1
    assert plan.skipped_sectors == {"Banks": 1}
    assert plan.skipped_golden == ["Almarai Co."]


def test_existing_issuer_keeps_its_pool_and_role_and_gets_only_what_it_lacks() -> None:
    docs = [
        existing_doc("Jarir Marketing", "ar", pool="blind"),
        existing_doc("Jarir Marketing", "en", pool="blind", year=2026, period="interim"),
    ]
    plan = plan_for([row("jarir")], docs)
    assert [e["language"] for e in plan.entries] == ["ar"]
    assert plan.entries[0]["issuer"] == "Jarir Marketing"
    assert plan.entries[0]["pool"] == "blind"
    assert plan.skipped_existing == [
        ("Jarir Marketing", "en", "has a 2026 interim edition of unknown quarter")
    ]


def test_existing_negative_control_is_skipped() -> None:
    docs = [existing_doc("Existing Bank", "en", role="negative_control")]
    plan = plan_for([row("bnk")], docs)
    assert plan.entries == []
    assert plan.skipped_existing == [("Existing Bank", "both", "negative control")]


def test_a_url_already_in_the_candidates_is_not_added_again() -> None:
    url = f"{S3}new1-ar.pdf"
    docs = [existing_doc("Gulf Cement", "ar", year=2026, period="interim", url=url)]
    plan = plan_for([row("new1")], docs)
    assert [e["language"] for e in plan.entries] == ["en"]
    assert plan.entries[0]["issuer"] == "Gulf Cement"


def test_a_near_match_without_a_decision_stops_the_run_naming_it() -> None:
    docs = [existing_doc("Yanbu National Petrochemical (Yansab)", "en", pool="train")]
    with pytest.raises(ValueError, match=r"Yanbu National Petrochemical Co\.") as stopped:
        plan_for([row("yansab")], docs)
    assert "argaam_decisions.yaml" in str(stopped.value)


def test_every_undecided_company_is_named_at_once() -> None:
    docs = [
        existing_doc("Yanbu National Petrochemical (Yansab)", "en"),
        existing_doc("Saudi Industrial Development", "en"),
    ]
    with pytest.raises(ValueError) as stopped:
        plan_for([row("yansab"), row("siig")], docs)
    assert "Yanbu National Petrochemical Co." in str(stopped.value)
    assert "Saudi Industrial Investment Group" in str(stopped.value)


def test_a_near_match_decided_same_uses_the_existing_name_and_pool() -> None:
    docs = [existing_doc("Yanbu National Petrochemical (Yansab)", "en", pool="model_test")]
    decisions = {
        "Yanbu National Petrochemical Co.": Decision(
            "same", "Yanbu National Petrochemical (Yansab)", "same company"
        )
    }
    plan = plan_for([row("yansab")], docs, decisions)
    assert {e["issuer"] for e in plan.entries} == {"Yanbu National Petrochemical (Yansab)"}
    assert {e["pool"] for e in plan.entries} == {"model_test"}
    assert plan.near[0].decision == "same"


def test_a_near_match_decided_different_is_a_new_issuer() -> None:
    docs = [existing_doc("Saudi Industrial Development", "en", pool="blind")]
    decisions = {
        "Saudi Industrial Investment Group": Decision(
            "different", None, "holding group", ("Saudi Industrial Development",)
        )
    }
    plan = plan_for([row("siig")], docs, decisions)
    assert {e["issuer"] for e in plan.entries} == {"Saudi Industrial Investment Group"}
    assert {e["pool"] for e in plan.entries} == {assign_pool("Saudi Industrial Investment Group")}
    assert plan.near[0].decision == "different"


def test_a_near_match_decided_exclude_is_left_out_with_the_reason() -> None:
    docs = [existing_doc("Saudi Industrial Development", "en", pool="blind")]
    decisions = {
        "Saudi Industrial Investment Group": Decision("exclude", None, "parent of a filer")
    }
    plan = plan_for([row("siig")], docs, decisions)
    assert plan.entries == []
    assert plan.excluded == [("Saudi Industrial Investment Group", "parent of a filer")]
    assert plan.near[0].decision == "excluded"


def test_a_near_match_decided_different_must_cover_every_current_near_match() -> None:
    docs = [
        existing_doc("Saudi Industrial Development", "en", pool="blind"),
        existing_doc("Saudi Industrial Services", "en", pool="train"),
    ]
    decisions = {
        "Saudi Industrial Investment Group": Decision(
            "different", None, "holding group", ("Saudi Industrial Development",)
        )
    }
    with pytest.raises(ValueError, match="Saudi Industrial Investment Group") as stopped:
        plan_for([row("siig")], docs, decisions)
    assert "Saudi Industrial Services" in str(stopped.value)
    assert "argaam_decisions.yaml" in str(stopped.value)


def test_a_company_decided_same_without_a_near_match_joins_the_existing_issuer() -> None:
    # A rename: the new name shares no token with the recorded one, so nothing nearly matches.
    docs = [existing_doc("Old Name Holdings", "en", pool="model_test", role="corporate")]
    assert near_matches("Gulf Cement Co.", ["Old Name Holdings"], ["new1"]) == []
    decisions = {"Gulf Cement Co.": Decision("same", "Old Name Holdings", "renamed")}
    plan = plan_for([row("new1")], docs, decisions)
    assert {e["issuer"] for e in plan.entries} == {"Old Name Holdings"}
    assert {e["pool"] for e in plan.entries} == {"model_test"}
    assert {e["role"] for e in plan.entries} == {"corporate"}
    assert plan.near == []


def test_a_sector_in_neither_list_stops_the_run_naming_it() -> None:
    with pytest.raises(ValueError, match="Mystery Sector") as stopped:
        is_corporate(row("new1", "Mystery Sector"))
    assert "new1" in str(stopped.value)
    with pytest.raises(ValueError, match="Mystery Sector"):
        plan_for([row("new1", "Mystery Sector")], [])


def test_two_new_companies_with_one_issuer_key_are_both_left_out() -> None:
    names = {"/en/tadawul/tasi/a1": "Gulf Cement Co.", "/en/tadawul/tasi/a2": "Gulf Cement Company"}
    rows_ = [row("a1"), row("a2")]
    plan = build_plan(rows_, names, [], GOLDEN_KEYS, {})
    assert plan.entries == []
    assert [reason for _, reason in plan.excluded] == ["same issuer key as another new company"] * 2


CANDIDATES_TEXT = """\
documents:

  # ---- blind ----
  - {id: agthia-2025-ar, issuer: Agthia Group, pool: blind}
  - {id: alrajhi-2025-en, issuer: Al Rajhi Bank, pool: blind}

  # ---- train ----
  - {id: jarir-marketing-2025-ar, issuer: Jarir Marketing, pool: train}
  - {id: zain-ksa-2025-en, issuer: Zain KSA, pool: train}
"""


def test_entries_are_added_in_their_pool_section_in_issuer_order() -> None:
    new = [
        {"id": "alf-2026-ar-interim", "issuer": "Alf", "pool": "blind"},
        {"id": "jarir-marketing-2026-ar-interim", "issuer": "Jarir Marketing", "pool": "train"},
        {"id": "zzz-2026-ar-interim", "issuer": "Zzz", "pool": "train"},
    ]
    text = add_entries(CANDIDATES_TEXT, new)
    ids = [
        line.split(",")[0].removeprefix("  - {id: ")
        for line in text.splitlines()
        if line.startswith("  - {id")
    ]
    assert ids == [
        "agthia-2025-ar",
        "alrajhi-2025-en",
        "alf-2026-ar-interim",
        "jarir-marketing-2025-ar",
        "jarir-marketing-2026-ar-interim",
        "zain-ksa-2025-en",
        "zzz-2026-ar-interim",
    ]


def test_entries_are_removed_by_id_and_nothing_else_changes() -> None:
    text = remove_entries(CANDIDATES_TEXT, {"alrajhi-2025-en", "zain-ksa-2025-en"})
    assert text == CANDIDATES_TEXT.replace(
        "  - {id: alrajhi-2025-en, issuer: Al Rajhi Bank, pool: blind}\n", ""
    ).replace("  - {id: zain-ksa-2025-en, issuer: Zain KSA, pool: train}\n", "")
    with pytest.raises(ValueError, match="no-such-id"):
        remove_entries(CANDIDATES_TEXT, {"no-such-id"})


# ---- what the downloaded bytes show ----

BOOK = load_title_book()


def page_of(text: str, number: int = 1, *, body: str = "") -> PageText:
    """A page as the reader makes it: text pages above the minimum, flagged when garbled."""
    has_text = len(text.strip()) >= 50
    return PageText(
        page_no=number,
        mode=PageMode.TEXT if has_text else PageMode.IMAGE,
        source=TextSource.TEXT if has_text else None,
        header_text=text if has_text else "",
        body_text=body if has_text else "",
        char_count=len(text.replace(" ", "")) if has_text else 0,
        width_pt=595.0,
        height_pt=842.0,
        flags=(["garbled_text_layer"] if text_layer_is_garbled(text) else [])
        if has_text
        else ["ocr_unavailable"],
    )


def analyse_texts(texts: list[str]) -> TextFacts:
    return analyse_pages([page_of(t, i + 1) for i, t in enumerate(texts)], BOOK)


AMOUNTS = "\n".join(f"Item {i}  {i}1,234,567  {i}2,345,678" for i in range(1, 40))
BALANCE_PAGE = page_of(
    "Interim condensed statement of financial position\nAs at 31 March 2026 and 31 December 2025\n"
    "(Saudi Riyals)",
    4,
    body=AMOUNTS,
)
INCOME_PAGE = page_of(
    "Interim condensed statement of profit or loss and other comprehensive income\n"
    "For the three-month period ended 31 March 2026 and 2025\n(Saudi Riyals)",
    5,
    body=AMOUNTS,
)
NOTES_PAGE = page_of("Notes to the interim condensed financial statements\nGeneral " * 4, 6)


def test_a_document_with_both_statements_as_text_is_clean() -> None:
    found = analyse_pages([BALANCE_PAGE, INCOME_PAGE, NOTES_PAGE], BOOK)
    assert set(REQUIRED_STATEMENTS) <= found.statements
    assert is_clean(found)


def test_a_document_missing_a_statement_page_is_not_clean() -> None:
    only_balance = analyse_pages([BALANCE_PAGE, NOTES_PAGE], BOOK)
    assert not is_clean(only_balance)
    textless = analyse_pages([BALANCE_PAGE, page_of("", 5), NOTES_PAGE], BOOK)
    assert not is_clean(textless)


def test_a_garbled_page_anywhere_makes_a_document_not_clean() -> None:
    garbled = page_of("αβγδεζηθικλμνξοπρστυφχψω " * 8, 7)
    found = analyse_pages([BALANCE_PAGE, INCOME_PAGE, garbled], BOOK)
    assert found.garbled_pages == 1
    assert not is_clean(found)


def test_the_statements_are_found_by_the_projects_locator_not_by_this_script() -> None:
    import argaam_listing

    assert argaam_listing.score_page is fra_ingest.locate.score_page


ARABIC_PAGE = "قائمة المركز المالي الأولية الموجزة كما في 31 مارس 2026 " * 3
ENGLISH_PAGE = "Interim condensed statement of financial position as at 31 March 2026 " * 3
GREEK_PAGE = "αβγδεζηθικλμνξοπρστυφχψω " * 8


def test_texts_are_read_for_script_readability_and_period() -> None:
    found = analyse_texts([ARABIC_PAGE, "", ENGLISH_PAGE, "12"])
    assert found.textful_pages == 2  # pages under the locator's minimum have no text layer
    assert found.garbled_pages == 0
    assert 0.4 < (found.arabic_share or 0) < 0.6
    assert found.shows_period


def test_only_the_first_pages_can_show_the_period() -> None:
    late = ["Notes " * 20] * 5 + [ENGLISH_PAGE]
    assert not analyse_texts(late).shows_period


def test_a_later_2026_period_on_the_title_pages_overrules_a_march_mention() -> None:
    q2 = "Interim statements for the three-month period ended 30 June 2026 " * 2
    comparison = "Balance as at 31 March 2026 and 31 December 2025 and more words " * 2
    assert not analyse_texts([q2, comparison]).shows_period
    notes = ["Notes to the statements " * 5] * 2
    assert analyse_texts([ENGLISH_PAGE, comparison, *notes, q2]).shows_period


@pytest.mark.parametrize(
    "text",
    [
        "for the three-month period ended 30 June 2026",
        "AS AT SEPTEMBER 30, 2026",
        "period ended: June 30,2026",
        "ended 30/06/2026",
    ],
)
def test_months_after_march_2026_are_recognised_as_a_period_end(text: str) -> None:
    assert names_later_period(text)


@pytest.mark.parametrize(
    "text",
    [
        "ended 31 March 2026",
        "as at 31 December 2025",
        "ended 30 June 2025",
        "Review report dated May 12, 2026",
        "Authorised for issue on 14 June 2026",
    ],
)
def test_march_earlier_dates_and_report_dates_are_not_a_later_period(text: str) -> None:
    assert not names_later_period(text)


def test_pages_without_text_do_not_use_up_the_first_pages() -> None:
    assert analyse_texts(["", "", "", "", "", "", ENGLISH_PAGE]).shows_period


def test_the_period_a_file_does_show_is_reported() -> None:
    page = "Condensed statements for the three-month period ended 30 June 2026 " * 2
    found = analyse_texts([page])
    assert not found.shows_period
    assert found.period_seen == "ended 30 June 2026"
    assert analyse_texts([ARABIC_PAGE]).period_seen == ""


def test_a_page_of_greek_letters_is_a_garbled_text_layer() -> None:
    assert analyse_texts([ARABIC_PAGE, GREEK_PAGE]).garbled_pages == 1


def test_a_file_without_any_text_has_no_script_share() -> None:
    found = analyse_texts(["", "7"])
    assert found.arabic_share is None and found.textful_pages == 0 and not found.shows_period


@pytest.mark.parametrize(
    ("language", "share", "ok"),
    [
        ("ar", 0.8, True),
        ("ar", 0.2, False),
        ("en", 0.02, True),
        ("en", 0.9, False),
        ("en", None, False),
    ],
)
def test_language_must_match_the_edition(language: str, share: float | None, ok: bool) -> None:
    assert language_matches(language, share) is ok


def test_an_existing_2026_edition_of_another_quarter_is_not_the_q1_edition() -> None:
    q2_url = f"{S3}9f3a0c11-0000-4000-8000-000000000001.pdf"
    r = ListingRow(
        "JARIR",
        "/en/tadawul/tasi/jarir",
        None,
        "Materials",
        {
            "Q1": {
                "ar": f"{S3}7c1d0e22-0000-4000-8000-000000000002.pdf",
                "en": f"{S3}b4e5f633-0000-4000-8000-000000000003.pdf",
            },
            "Q2": {"en": q2_url},
        },
    )
    docs = [existing_doc("Jarir Marketing", "en", year=2026, period="interim", url=q2_url)]
    plan = plan_for([r], docs)
    ids = {e["language"]: e["id"] for e in plan.entries}
    assert ids == {
        "ar": "jarir-marketing-2026-ar-interim",
        "en": "jarir-marketing-2026-en-interim-b4e5",  # the plain id is the Q2 edition's
    }
    assert plan.skipped_existing == []


def test_an_existing_2026_edition_of_unknown_quarter_is_left_alone() -> None:
    docs = [existing_doc("Jarir Marketing", "en", year=2026, period="interim")]
    plan = plan_for([row("jarir")], docs)
    assert [e["language"] for e in plan.entries] == ["ar"]
    assert plan.skipped_existing == [
        ("Jarir Marketing", "en", "has a 2026 interim edition of unknown quarter")
    ]


def test_the_set_aside_file_reads_back_what_was_written_and_may_be_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "deferred.yaml"
    monkeypatch.setattr(argaam_listing, "DEFERRED", path)
    assert argaam_listing.set_aside_documents() == []  # no file yet
    argaam_listing.write_set_aside([])
    assert argaam_listing.set_aside_documents() == []  # a header and no entries
    doc = existing_doc("Jarir Marketing", "ar", year=2026, period="interim") | {
        "reason": "ar edition: a garbled page, 3: it is not clean"
    }
    argaam_listing.write_set_aside([doc])
    assert argaam_listing.set_aside_documents() == [doc]


def layout(textless: set[int], balance: int, income: int, pages: int = 20) -> list[PageText]:
    """A document of `pages` pages: the two statements on the given pages, notes elsewhere, and
    no text layer on the `textless` pages."""
    made = []
    for number in range(1, pages + 1):
        if number in textless:
            made.append(page_of("", number))
        elif number == balance:
            made.append(BALANCE_PAGE.model_copy(update={"page_no": number}))
        elif number == income:
            made.append(INCOME_PAGE.model_copy(update={"page_no": number}))
        else:
            made.append(
                page_of("Notes to the interim condensed financial statements\nGeneral " * 4, number)
            )
    return made


def test_statements_found_on_notes_after_a_run_of_images_are_not_clean() -> None:
    # The primary statements are the images on pages 5 to 8; the pages found are later notes.
    found = analyse_pages(layout({5, 6, 7, 8}, balance=16, income=18), BOOK)
    assert set(REQUIRED_STATEMENTS) <= found.statements
    assert found.image_pages_before_statements == (5, 6, 7, 8)
    assert not is_clean(found)


def test_a_balance_sheet_image_before_a_notes_page_that_names_it_is_not_clean() -> None:
    found = analyse_pages(layout({4}, balance=11, income=5), BOOK)
    assert found.image_pages_before_statements == (4,)
    assert not is_clean(found)


def test_an_image_cover_is_allowed_before_text_statements() -> None:
    found = analyse_pages(layout({1}, balance=4, income=5), BOOK)
    assert found.image_pages_before_statements == ()
    assert is_clean(found)


@pytest.mark.parametrize(
    ("textless", "balance", "income", "clean"),
    [
        ({3}, 4, 5, True),  # an auditor's letter directly before the statements
        ({2, 3}, 4, 5, True),  # a two-page letter
        ({2, 3, 4}, 5, 6, False),  # three pages are a run of images, not a letter
        ({4}, 12, 5, False),  # the statements are too far apart: one was matched in the notes
        ({3}, 16, 18, False),  # the letter is not next to the first located page
        ({3, 6}, 4, 7, False),  # two separate runs
        (set(), 4, 9, False),  # no image, but the statements are too far apart
        (set(), 4, 7, False),  # three pages apart is too far
        (set(), 4, 6, True),  # two pages apart is allowed
    ],
)
def test_images_before_the_statements_are_allowed_only_as_a_short_letter_next_to_them(
    textless: set[int], balance: int, income: int, clean: bool
) -> None:
    assert is_clean(analyse_pages(layout(textless, balance, income), BOOK)) is clean


def test_images_after_the_last_statement_page_do_not_matter() -> None:
    found = analyse_pages(layout({6, 7, 8, 20}, balance=4, income=5), BOOK)
    assert is_clean(found)


STATEMENT_TITLES = {
    StatementType.BALANCE: BALANCE_PAGE.header_text,
    StatementType.INCOME: INCOME_PAGE.header_text,
}


def statement_page(statement: StatementType, number: int, amounts: int) -> PageText:
    """A statement page with a title and `amounts` numeric tokens, one per line."""
    body = "\n".join(f"Item  {i}1,234,567" for i in range(1, amounts + 1))
    return page_of(STATEMENT_TITLES[statement], number, body=body)


def test_a_letter_naming_both_statements_with_few_amounts_is_not_clean() -> None:
    letter = page_of(
        "Report on review of the interim condensed financial statements\n"
        "Interim condensed statement of financial position\n"
        "Interim condensed statement of profit or loss and other comprehensive income\n"
        "as at and for the period ended 31 March 2026\n(Saudi Riyals)",
        3,
        body="\n".join(f"Ref  {i}1,234" for i in range(1, 5)),
    )
    found = analyse_pages([letter, page_of("", 4), page_of("", 5), NOTES_PAGE], BOOK)
    assert {p.statement for p in found.located} == set(REQUIRED_STATEMENTS)
    assert {p.page_no for p in found.located} == {3}
    assert {p.amounts for p in found.located} == {4}
    assert not is_clean(found)


def test_a_statement_title_without_amounts_is_not_clean() -> None:
    # The balance sheet is a picture pasted into a text page that keeps only its title.
    pages = [
        statement_page(StatementType.BALANCE, 3, 0),
        statement_page(StatementType.INCOME, 4, 30),
        NOTES_PAGE,
    ]
    found = analyse_pages(pages, BOOK)
    assert LocatedPage(StatementType.BALANCE, 3, 0) in found.located
    assert not is_clean(found)


def test_the_located_page_of_a_range_is_its_best_page_not_its_auditors_letter() -> None:
    letter = page_of(
        "Report on review of the interim condensed financial statements\n"
        "Interim condensed statement of financial position\nas at 31 March 2026\n(Saudi Riyals)",
        3,
        body="\n".join(f"Ref  {i}1,234" for i in range(1, 5)),
    )
    pages = [
        letter,
        statement_page(StatementType.BALANCE, 4, 40),
        statement_page(StatementType.INCOME, 5, 40),
        NOTES_PAGE,
    ]
    found = analyse_pages(pages, BOOK)
    balance = next(p for p in found.located if p.statement is StatementType.BALANCE)
    assert (balance.page_no, balance.amounts) == (4, 40)
    assert is_clean(found)


@pytest.mark.parametrize(
    ("amounts", "clean"),
    [(MIN_STATEMENT_AMOUNTS - 1, False), (MIN_STATEMENT_AMOUNTS, True)],
)
def test_a_located_page_needs_the_minimum_number_of_amounts(amounts: int, clean: bool) -> None:
    pages = [
        statement_page(StatementType.BALANCE, 3, amounts),
        statement_page(StatementType.INCOME, 4, MIN_STATEMENT_AMOUNTS + 10),
        NOTES_PAGE,
    ]
    assert is_clean(analyse_pages(pages, BOOK)) is clean


def text_facts(*, clean: bool, language: str, period: bool = True) -> TextFacts:
    return TextFacts(
        arabic_share=0.9 if language == "ar" else 0.0,
        textful_pages=10,
        garbled_pages=0,
        shows_period=period,
        period_seen="ENDED 30 JUNE 2026",
        statements=REQUIRED_STATEMENTS if clean else frozenset(),
        image_pages_before_statements=(),
        located=(
            (LocatedPage(StatementType.BALANCE, 4, 50), LocatedPage(StatementType.INCOME, 5, 30))
            if clean
            else ()
        ),
    )


class Screening:
    """A tiny corpus on disk and the seams `screen` reads it through. read_pages hands back the
    file's stem in place of pages and analyse_pages turns that into the facts of a clean, noisy
    or wrong-period file (the clean rule itself is tested above); parse_listing returns rows
    that pair each company's editions by URL."""

    VERDICTS: ClassVar[dict[str, str]] = {
        "clean": "clean",
        "noisy": "noisy",
        "wrong": "wrong",
        "half": "clean",
    }

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import corpus

        self.corpus = corpus
        self.store = tmp_path / "store"
        self.candidates = tmp_path / "candidates.yaml"
        self.fetched = tmp_path / "fetched.yaml"
        self.deferred = tmp_path / "deferred.yaml"
        self.cache = tmp_path / "cache"
        self.cache.mkdir()
        (self.cache / "listing.html").write_text("", encoding="utf-8")
        self.docs: dict[str, dict[str, Any]] = {}
        self.listing: list[ListingRow] = []
        self.noisy_counterpart = False
        monkeypatch.setattr(argaam_listing, "read_pages", lambda path, config, ocr: [path.stem])
        monkeypatch.setattr(argaam_listing, "analyse_pages", self.analyse)
        monkeypatch.setattr(argaam_listing, "parse_listing", lambda html: self.listing)
        monkeypatch.setattr(argaam_listing, "load_config", lambda: None)
        monkeypatch.setattr(argaam_listing, "load_title_book", lambda: None)
        monkeypatch.setattr(corpus, "use_system_trust", lambda: None)
        monkeypatch.setattr(corpus, "CANDIDATES", self.candidates)
        monkeypatch.setattr(corpus, "FETCHED", self.fetched)
        monkeypatch.setattr(corpus, "STORE", self.store)
        monkeypatch.setattr(argaam_listing, "DEFERRED", self.deferred)

    def analyse(self, pages: list[str], book: Any) -> TextFacts:
        stem = pages[0]
        company = stem.split("-")[0]
        language = stem.split("-")[-2]
        verdict = self.VERDICTS[company]
        if company == "half" and language == "en":
            verdict = "noisy" if self.noisy_counterpart else "clean"
        return text_facts(clean=verdict == "clean", language=language, period=verdict != "wrong")

    def add(self, issuer: str, languages: tuple[str, ...], *, on_disk: bool = True) -> list[str]:
        ids = []
        for language in ("ar", "en"):
            doc = existing_doc(issuer, language, year=2026, period="interim")
            self.docs[doc["id"]] = doc
            ids.append(doc["id"])
        self.listing.append(
            ListingRow(
                issuer,
                f"/en/{issuer}",
                None,
                "Materials",
                {
                    "Q1": {
                        d["language"]: d["url"] for d in self.docs.values() if d["issuer"] == issuer
                    }
                },
            )
        )
        (self.store / "train").mkdir(parents=True, exist_ok=True)
        for doc_id in ids:
            if on_disk and doc_id.rsplit("-", 2)[-2] in languages:
                (self.store / "train" / f"{doc_id}.pdf").write_bytes(b"%PDF-1.7")
        return [i for i in ids if i.rsplit("-", 2)[-2] in languages]

    def write(self, added: list[str], failed: str | None = None) -> None:
        docs = list(self.docs.values())
        self.candidates.write_text(
            "documents:\n  # ---- train ----\n" + "".join(entry_line(d) + "\n" for d in docs),
            encoding="utf-8",
        )
        entry = {"pool": "train", "status": "new", "text_layer": "digital", "pages": 10}
        self.fetched.write_text(
            yaml.safe_dump(
                {
                    "documents": {
                        d["id"]: (
                            {"pool": "train", "status": "failed", "error": "HTTP Error 404"}
                            if d["id"] == failed
                            else entry | {"pages_without_text": 0}
                        )
                        for d in docs
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.cache / "added.yaml").write_text(yaml.safe_dump(added), encoding="utf-8")

    def run(self, *flags: str) -> int:
        return argaam_listing.main(["screen", "--cache", str(self.cache), *flags])

    def candidate_ids(self) -> list[str]:
        return [d["id"] for d in self.corpus.load_yaml(self.candidates)["documents"] or []]

    def pdfs(self) -> set[str]:
        return {path.stem for path in (self.store / "train").glob("*.pdf")}


def test_screen_apply_keeps_clean_pairs_and_sets_the_rest_aside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screening = Screening(tmp_path, monkeypatch)
    clean = screening.add("Clean Co", ("ar", "en"))
    noisy = screening.add("Noisy Co", ("ar", "en"))
    wrong = screening.add("Wrong Co", ("ar", "en"))
    # An existing issuer that gains its Arabic edition: its English one is a corpus document.
    half = screening.add("Half Co", ("ar", "en"))
    screening.noisy_counterpart = True
    screening.write(clean + noisy + wrong + [half[0]])

    assert screening.run("--apply") == 0

    assert screening.candidate_ids() == [*clean, half[1]]  # the counterpart stays
    assert screening.pdfs() == {*clean, *half[1:]}
    assert sorted(screening.corpus.load_yaml(screening.fetched)["documents"]) == sorted(
        [*clean, half[1]]
    )
    set_aside = {d["id"]: d["reason"] for d in argaam_listing.set_aside_documents()}
    assert sorted(set_aside) == sorted([*noisy, *wrong, half[0]])
    assert "30 JUNE 2026" in set_aside[wrong[0]]
    assert "Arabic edition" in set_aside[noisy[0]] or "English edition" in set_aside[noisy[0]]
    # A second run has nothing left to check and leaves the files as they are.
    before = (screening.candidates.read_text(), screening.fetched.read_text())
    assert screening.run("--apply") == 0
    assert (screening.candidates.read_text(), screening.fetched.read_text()) == before
    assert sorted(d["id"] for d in argaam_listing.set_aside_documents()) == sorted(set_aside)


def test_screen_judges_the_existing_counterpart_and_stops_when_it_is_not_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screening = Screening(tmp_path, monkeypatch)
    half = screening.add("Half Co", ("ar",))  # only the Arabic file is on disk
    screening.write([half[0]])
    before = screening.candidates.read_text()
    with pytest.raises(ValueError, match=r"half-co-2026-en-interim.*fetch --id"):
        screening.run("--apply")
    assert screening.candidates.read_text() == before
    assert screening.pdfs() == {half[0]}


def test_screen_stops_on_a_failed_download_instead_of_setting_the_issuer_aside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screening = Screening(tmp_path, monkeypatch)
    clean = screening.add("Clean Co", ("ar", "en"))
    screening.write(clean, failed=clean[0])
    before = screening.candidates.read_text()
    with pytest.raises(ValueError, match="not downloaded"):
        screening.run("--apply")
    assert screening.candidates.read_text() == before
    assert not screening.deferred.exists()


def test_screen_reopen_brings_the_set_aside_documents_of_the_run_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screening = Screening(tmp_path, monkeypatch)
    noisy = screening.add("Noisy Co", ("ar", "en"))
    screening.write(noisy)
    assert screening.run("--apply") == 0
    assert screening.candidate_ids() == []
    assert screening.run("--reopen") == 0
    assert sorted(screening.candidate_ids()) == sorted(noisy)
    assert argaam_listing.set_aside_documents() == []


def test_a_downloaded_page_is_cached_only_after_it_has_parsed(tmp_path: Path) -> None:
    class Polite:
        def allowed(self, url: str) -> bool:
            return True

        def get(self, url: str) -> bytes:
            return b"sign in"

    requests = argaam_listing.Requests(tmp_path, Polite())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="listing"):
        requests.page("https://x.example/l", "listing.html", parse_listing)
    assert not (tmp_path / "listing.html").exists()
    assert requests.made == 1
