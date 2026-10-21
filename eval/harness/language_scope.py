"""Manifest language policy shared by evaluation gates."""

from collections.abc import Mapping
from typing import TypedDict

GATED_LANGUAGE = "en"
REPORTED_LANGUAGES = (GATED_LANGUAGE, "ar")


class UnreadableDocument(TypedDict):
    id: str
    language: str
    reason: str
    detail: str


def manifest_language(entry: Mapping[str, object]) -> str:
    """Require explicit attribution; extracted text cannot identify an unreadable input."""
    doc_id, language = entry.get("id"), entry.get("language")
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError("document attribution requires a nonempty id")
    if not isinstance(language, str) or language not in REPORTED_LANGUAGES:
        raise ValueError(
            f"document {doc_id!r} has missing or unsupported manifest language {language!r}"
        )
    return language
