"""Read-only fingerprints of the saved inputs and plans behind document location."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from fra_ingest.pages import PAGES_STAGE_VERSION
from fra_ingest.results import PageText
from harness.reproducibility import record_hash


@dataclass(frozen=True)
class PageFingerprint:
    page_no: int
    sha256: str


@dataclass(frozen=True)
class LocatedRange:
    type: str
    first_page: int
    last_page: int
    score: float
    rank: int


@dataclass(frozen=True)
class ConversionSpan:
    first_page: int
    last_page: int
    ocr_language: str | None
    renderer: str


@dataclass(frozen=True)
class DocumentEvidence:
    status: str
    reason: str | None
    page_count: int | None
    pages_sha256: str | None
    pages: tuple[PageFingerprint, ...]
    locate_version: str | None
    ranges: tuple[LocatedRange, ...]
    conversion_spans: tuple[ConversionSpan, ...]
    conversion_settings_hash: str | None
    ocr_key: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fingerprint_pages(pages: list[PageText] | tuple[PageText, ...]) -> tuple[PageFingerprint, ...]:
    """Hash every serialized PageText field except timing, without text normalization."""
    fingerprints: list[PageFingerprint] = []
    seen: set[int] = set()
    for raw in pages:
        try:
            page = raw if isinstance(raw, PageText) else PageText.model_validate(raw)
        except (ValidationError, TypeError) as exc:
            raise ValueError(f"invalid page record or missing page_no: {exc}") from exc
        if page.page_no in seen:
            raise ValueError(f"duplicate page_no {page.page_no}")
        seen.add(page.page_no)
        payload = page.model_dump(mode="json", exclude={"ocr_seconds", "ocr_call_seconds"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fingerprints.append(
            PageFingerprint(
                page_no=page.page_no, sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            )
        )
    fingerprints.sort(key=lambda item: item.page_no)
    expected_pages = list(range(1, len(fingerprints) + 1))
    actual_pages = [item.page_no for item in fingerprints]
    if actual_pages != expected_pages:
        missing = sorted(set(expected_pages) - set(actual_pages))
        raise ValueError(f"missing page numbers: {missing}")
    return tuple(fingerprints)


def _incomplete(reason: str) -> DocumentEvidence:
    return DocumentEvidence("incomplete", reason, None, None, (), None, (), (), None, None)


def document_evidence(artifact_dir: Path) -> DocumentEvidence:
    """Read the page cache and saved locate/convert records; never open or process a PDF."""
    try:
        pages_path = artifact_dir / f"pages.v{PAGES_STAGE_VERSION}.json"
        pages_payload = json.loads(pages_path.read_text(encoding="utf-8"))
        if (
            not isinstance(pages_payload, dict)
            or pages_payload.get("version") != PAGES_STAGE_VERSION
        ):
            raise ValueError(f"page cache version must be {PAGES_STAGE_VERSION}")
        page_values = pages_payload["pages"]
        if not isinstance(page_values, list) or not page_values:
            raise ValueError("page cache has no pages")
        pages = fingerprint_pages(tuple(PageText.model_validate(value) for value in page_values))

        locate = json.loads((artifact_dir / "locate.json").read_text(encoding="utf-8"))
        convert = json.loads((artifact_dir / "convert.json").read_text(encoding="utf-8"))
        if not isinstance(locate, dict) or not isinstance(convert, dict):
            raise ValueError("locate.json and convert.json must contain JSON objects")
        page_count = locate["document"]["page_count"]
        if type(page_count) is not int or page_count != len(pages):
            raise ValueError(
                f"locate page_count {page_count!r} does not match cached pages {len(pages)}"
            )
        if not isinstance(locate["version"], str) or not locate["version"]:
            raise ValueError("locate version must be a non-empty string")
        ocr_key = locate["ocr_key"]
        if not isinstance(ocr_key, str) or not ocr_key:
            raise ValueError("locate ocr_key must be a non-empty string")
        if not isinstance(locate["ranges"], list):
            raise ValueError("locate ranges must be a list")
        ranges = tuple(
            LocatedRange(
                type=str(item["type"]),
                first_page=int(item["first_page"]),
                last_page=int(item["last_page"]),
                score=float(item["score"]),
                rank=int(item["rank"]),
            )
            for item in locate["ranges"]
        )
        plan_values = convert["settings_plans"]
        if plan_values is None:
            plan_values = []
        if not isinstance(plan_values, list):
            raise ValueError("convert settings_plans must be a list or null")
        settings_hash = convert["settings_hash"]
        if not isinstance(settings_hash, str) or not settings_hash:
            raise ValueError("convert settings_hash must be a non-empty string")
        spans = tuple(
            ConversionSpan(
                first_page=int(item["first_page"]),
                last_page=int(item["last_page"]),
                ocr_language=item["ocr_language"],
                renderer=str(item.get("renderer", "default")),
            )
            for item in plan_values
        )
        digest = record_hash([item.sha256 for item in pages])
        return DocumentEvidence(
            "complete",
            None,
            page_count,
            digest,
            pages,
            locate["version"],
            ranges,
            spans,
            settings_hash,
            ocr_key,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError) as exc:
        return _incomplete(f"{artifact_dir}: {exc}")


def compare_evidence(previous: DocumentEvidence, current: DocumentEvidence) -> dict[str, Any]:
    if previous.status != "complete" or current.status != "complete":
        return {"status": "not comparable", "reason": previous.reason or current.reason}
    before_pages = {page.page_no: page.sha256 for page in previous.pages}
    after_pages = {page.page_no: page.sha256 for page in current.pages}
    changed_pages = sorted(
        page_no
        for page_no in before_pages.keys() | after_pages.keys()
        if before_pages.get(page_no) != after_pages.get(page_no)
    )
    before_ranges = {json.dumps(asdict(item), sort_keys=True) for item in previous.ranges}
    after_ranges = {json.dumps(asdict(item), sort_keys=True) for item in current.ranges}
    lost = [json.loads(item) for item in sorted(before_ranges - after_ranges)]
    gained = [json.loads(item) for item in sorted(after_ranges - before_ranges)]
    conversion_spans_changed = previous.conversion_spans != current.conversion_spans
    previous_spans = {
        json.dumps(asdict(item), sort_keys=True) for item in previous.conversion_spans
    }
    current_spans = {json.dumps(asdict(item), sort_keys=True) for item in current.conversion_spans}
    conversion_settings_changed = (
        previous.conversion_settings_hash != current.conversion_settings_hash
    )
    locate_version_changed = (
        {"previous": previous.locate_version, "current": current.locate_version}
        if previous.locate_version != current.locate_version
        else None
    )
    ocr_key_changed = (
        {"previous": previous.ocr_key, "current": current.ocr_key}
        if previous.ocr_key != current.ocr_key
        else None
    )
    if changed_pages:
        status = "inputs changed"
    elif lost or gained:
        status = "ranges changed"
    elif conversion_spans_changed:
        status = "conversion spans changed"
    elif locate_version_changed:
        status = f"locate version changed ({previous.locate_version} -> {current.locate_version})"
    elif ocr_key_changed:
        status = "locate OCR key changed"
    elif conversion_settings_changed:
        status = "conversion settings hash changed"
    else:
        status = "unchanged"
    return {
        "status": status,
        "changed_pages": changed_pages,
        "ranges_changed": bool(lost or gained),
        "lost_ranges": lost,
        "gained_ranges": gained,
        "conversion_spans_changed": conversion_spans_changed,
        "lost_conversion_spans": [
            json.loads(item) for item in sorted(previous_spans - current_spans)
        ],
        "gained_conversion_spans": [
            json.loads(item) for item in sorted(current_spans - previous_spans)
        ],
        "conversion_settings_changed": conversion_settings_changed,
        "locate_version_changed": locate_version_changed,
        "ocr_key_changed": ocr_key_changed,
    }


def evidence_from_dict(value: Any) -> DocumentEvidence | None:
    """Validate evidence from an earlier report; old reports intentionally have no evidence."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return _incomplete("malformed locate_evidence: expected an object")
    if value.get("status") == "incomplete":
        reason = value.get("reason")
        return _incomplete(f"earlier evidence incomplete: {reason or 'no reason provided'}")
    if value.get("status") != "complete":
        return _incomplete("malformed locate_evidence: status must be complete or incomplete")
    try:
        return DocumentEvidence(
            status="complete",
            reason=None,
            page_count=int(value["page_count"]),
            pages_sha256=str(value["pages_sha256"]),
            pages=tuple(PageFingerprint(**item) for item in value["pages"]),
            locate_version=str(value["locate_version"]),
            ranges=tuple(LocatedRange(**item) for item in value["ranges"]),
            conversion_spans=tuple(ConversionSpan(**item) for item in value["conversion_spans"]),
            conversion_settings_hash=str(value["conversion_settings_hash"]),
            ocr_key=str(value["ocr_key"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _incomplete(f"malformed locate_evidence: {exc}")
