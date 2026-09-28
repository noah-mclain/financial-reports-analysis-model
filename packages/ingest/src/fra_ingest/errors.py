"""Failures that stop ingest for a document, each with a reason the UI can show."""

from __future__ import annotations

from typing import Literal

IngestErrorReason = Literal["unreadable_pdf", "encrypted_pdf", "empty_pdf"]


class IngestError(Exception):
    def __init__(self, reason: IngestErrorReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: IngestErrorReason = reason
        self.detail = detail
