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
