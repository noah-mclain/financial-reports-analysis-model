"""Scale, currency, entity and consolidation (spec 11, Data flow step 6)."""

from fra_ingest.metadata import detect_metadata


def test_header_scale_and_currency() -> None:
    meta = detect_metadata(header_text="2025 EGP '000", context_texts=[], document_texts=[])
    assert (meta.scale, meta.currency, meta.flags) == (1000, "EGP", [])


def test_arabic_thousands_in_a_joined_run() -> None:
    meta = detect_metadata(
        header_text="31 ديسمبر 2025 مبآلاف X",
        context_texts=["بآلافالريالاتالسعودية"],
        document_texts=[],
    )
    assert (meta.scale, meta.currency) == (1000, "SAR")


def test_a_glyph_currency_is_inferred_from_the_document() -> None:
    meta = detect_metadata(
        header_text="31 December 2025 X '000",
        context_texts=["Statement of financial position"],
        document_texts=["Amounts in Saudi Riyals", "SAR 2,000", "paid in US dollars"],
    )
    assert meta.currency == "SAR"
    assert "currency_inferred" in meta.flags


def test_missing_values_are_flagged() -> None:
    meta = detect_metadata(header_text="2025", context_texts=[], document_texts=[])
    assert meta.scale is None and meta.currency is None
    assert {"scale_missing", "currency_missing"} <= set(meta.flags)


def test_disagreeing_signals_are_flagged_and_the_header_wins() -> None:
    meta = detect_metadata(
        header_text="EGP '000",
        context_texts=["In millions of Egyptian pounds"],
        document_texts=[],
    )
    assert meta.scale == 1000 and meta.conflict
    assert "scale_conflict" in meta.flags


def test_entity_and_consolidation_from_headings() -> None:
    meta = detect_metadata(
        header_text="",
        context_texts=[
            "Juhayna Food Industries Company",
            "Consolidated statement of financial position",
        ],
        document_texts=[],
    )
    assert meta.entity_name == "Juhayna Food Industries Company"
    assert meta.consolidated is True
    arabic = detect_metadata(
        header_text="", context_texts=["القوائمالماليةالموحدة"], document_texts=[]
    )
    assert arabic.consolidated is True
    standalone = detect_metadata(
        header_text="", context_texts=["Standalone statement of profit or loss"], document_texts=[]
    )
    assert standalone.consolidated is False


def test_entity_markers_must_match_word_boundaries() -> None:
    # "company" inside "accompanying" should not match
    meta = detect_metadata(header_text="", context_texts=["Accompanying notes"], document_texts=[])
    assert meta.entity_name is None

    # "Statement of financial position of the company" starts with "statement"
    meta = detect_metadata(
        header_text="",
        context_texts=["Statement of financial position of the company"],
        document_texts=[],
    )
    assert meta.entity_name is None

    # "The accompanying notes are an integral part of these financial statements"
    # starts with "the" and has "notes"
    meta = detect_metadata(
        header_text="",
        context_texts=["The accompanying notes are an integral part of these financial statements"],
        document_texts=[],
    )
    assert meta.entity_name is None


def test_consolidated_only_from_specific_phrases() -> None:
    # "Separate components of equity" alone should give None (not a full phrase)
    meta = detect_metadata(
        header_text="", context_texts=["Separate components of equity"], document_texts=[]
    )
    assert meta.consolidated is None

    # "Separate financial statements" is a specific phrase -> False
    meta = detect_metadata(
        header_text="", context_texts=["Separate financial statements"], document_texts=[]
    )
    assert meta.consolidated is False

    # "Separate statement" is a specific phrase -> False
    meta = detect_metadata(header_text="", context_texts=["Separate statement"], document_texts=[])
    assert meta.consolidated is False

    # "Stand-alone statement" -> False
    meta = detect_metadata(
        header_text="", context_texts=["Stand-alone statement"], document_texts=[]
    )
    assert meta.consolidated is False

    # Consolidated marker in any text wins over standalone in another
    meta = detect_metadata(
        header_text="",
        context_texts=[
            "Separate components of equity",
            "Consolidated statement of financial position",
        ],
        document_texts=[],
    )
    assert meta.consolidated is True

    # Arabic standalone phrase -> False
    meta = detect_metadata(
        header_text="", context_texts=["المستقلةالقوائمالمالية"], document_texts=[]
    )
    assert meta.consolidated is False


GLYPH_HEADER = "31 December 2025 X '000"
USD_DOCUMENT = [
    "issued a USD 500 million Sukuk",
    "USD 500 million international sukuk",
    "SAR million",
]


def test_a_domicile_phrase_decides_a_glyph_currency_before_the_document_majority() -> None:
    meta = detect_metadata(
        header_text=GLYPH_HEADER,
        context_texts=["Consolidated Statement of Financial Position"],
        document_texts=USD_DOCUMENT,
        domicile_texts=["Almarai Company (the Company) is a Saudi Joint Stock Company, which was"],
    )
    assert meta.currency == "SAR"
    assert "currency:domicile" in meta.signals
    assert "currency_from_domicile" in meta.flags
    assert "currency_inferred" not in meta.flags


def test_an_arabic_domicile_phrase_is_read_space_free() -> None:
    meta = detect_metadata(
        header_text=GLYPH_HEADER,
        context_texts=[],
        document_texts=USD_DOCUMENT,
        domicile_texts=["شركةالمراعي (شركةمساهمةسعودية)"],
    )
    assert (meta.currency, meta.flags) == ("SAR", ["currency_from_domicile"])


def test_an_egyptian_sae_company_is_in_pounds() -> None:
    meta = detect_metadata(
        header_text="2025",
        context_texts=[],
        document_texts=USD_DOCUMENT,
        domicile_texts=["Juhayna Food Industries (S.A.E.)"],
    )
    assert meta.currency == "EGP"


def test_a_country_name_alone_is_not_a_domicile() -> None:
    meta = detect_metadata(
        header_text=GLYPH_HEADER,
        context_texts=[],
        document_texts=USD_DOCUMENT,
        domicile_texts=[
            "Membership of Joint Stock Companies Inside and Outside of the Kingdom of Saudi Arabia",
            "operates in Saudi Arabia",
        ],
    )
    assert meta.currency == "USD"
    assert "currency_inferred" in meta.flags


def test_the_earliest_domicile_phrase_wins_over_later_subsidiaries() -> None:
    meta = detect_metadata(
        header_text=GLYPH_HEADER,
        context_texts=[],
        document_texts=USD_DOCUMENT,
        domicile_texts=[
            "Almarai Company is a Saudi Joint Stock Company",
            "Beyti Company (S.A.E.), a subsidiary",
            "International Dairy and Juice (Egypt) S.A.E.",
        ],
    )
    assert meta.currency == "SAR"
    assert "currency_from_domicile" in meta.flags


def _entity(*lines: str) -> str | None:
    return detect_metadata(header_text="", context_texts=list(lines), document_texts=[]).entity_name


def test_an_arabic_entity_line_starts_with_the_company_marker() -> None:
    assert _entity("حوكمةالشركة") is None
    assert _entity("حوكمة الشركة") is None
    assert _entity("شركة المراعي") == "شركة المراعي"
    assert _entity("شركةجهينةللصناعاتالغذائية") == "شركةجهينةللصناعاتالغذائية"


def test_a_domicile_line_is_not_an_entity_name() -> None:
    assert _entity("(An Egyptian Joint Stock Company)") is None
    assert _entity("An Egyptian Joint Stock Company") is None
    assert _entity("a Saudi Joint Stock Company") is None
    assert _entity("(شركة مساهمة مصرية)") is None
    assert _entity("شركة مساهمة مصرية") is None


def test_the_entity_line_is_kept_when_a_domicile_line_follows_it() -> None:
    lines = ("Juhayna Food Industries S.A.E.", "(An Egyptian Joint Stock Company)")
    assert _entity(*lines) == "Juhayna Food Industries S.A.E."
    assert _entity(*reversed(lines)) == "Juhayna Food Industries S.A.E."
