"""Caveat text isolates an embedded Latin currency code inside Arabic RTL text."""

from fra_core.presentation import caveat_text


def test_arabic_caveat_text_isolates_the_currency_code() -> None:
    text = caveat_text("scale_assumed_units", "EGP", "ar")
    assert "\u2066EGP\u2069" in text
    assert "EGP" not in text.replace("\u2066EGP\u2069", "")


def test_english_caveat_text_is_not_isolated() -> None:
    text = caveat_text("scale_assumed_units", "EGP", "en")
    assert "\u2066" not in text and "\u2069" not in text
    assert "EGP" in text
