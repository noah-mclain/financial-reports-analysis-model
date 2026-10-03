"""Shared contract: schemas, printed-value parsers, the canonical taxonomy and the issuer
split of the corpus (`fra_core.split`).

This package carries no heavy dependencies. Extraction, analytics, the model runtime and the
API all depend on it, and it depends on none of them.
"""

from fra_core.numbers import ParsedNumber, normalize_digits, parse_number, strip_bidi

__all__ = ["ParsedNumber", "normalize_digits", "parse_number", "strip_bidi"]
