"""Trusted presentation language shared by summary producers and renderers."""

from typing import Literal

from fra_core.schemas.caveat import CaveatId

Language = Literal["en", "ar"]

CAVEAT_TEXT: dict[str, dict[CaveatId, str]] = {
    "en": {
        "scale_assumed_units": "Amounts are shown as printed. The statement names no unit "
        "multiplier such as thousands or millions, so they are taken to be in single {currency}.",
        "currency_from_domicile": "The statement does not name its currency. {currency} is "
        "taken from the company's country of incorporation.",
        "currency_inferred": "The statement does not name its currency. {currency} is the "
        "currency named most often in the document.",
    },
    "ar": {
        "scale_assumed_units": "المبالغ معروضة كما وردت. لم تذكر القائمة مضاعفاً للوحدة مثل "
        "الآلاف أو الملايين، ولذلك اعتُبرت بالـ{currency} دون مضاعف.",
        "currency_from_domicile": "لم تذكر القائمة عملتها. اعتُمد {currency} بحسب بلد تأسيس الشركة.",
        "currency_inferred": "لم تذكر القائمة عملتها. اعتُمد {currency} لأنه العملة الأكثر ذكراً "
        "في المستند.",
    },
}


def caveat_text(caveat_id: CaveatId, currency: str, language: Language) -> str:
    # An RTL paragraph that embeds a Latin currency code needs explicit bidi isolation
    # (U+2066 LRI .. U+2069 PDI), matching the <bdi dir="ltr"> already used for numeric
    # claims in the rendered template; plain concatenation can misorder the surrounding text.
    isolated_currency = f"\u2066{currency}\u2069" if language == "ar" else currency
    return CAVEAT_TEXT[language][caveat_id].format(currency=isolated_currency)
