"""Read-only fingerprints of the saved inputs and plans behind document location."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from fra_ingest.pages import PAGES_STAGE_VERSION
from fra_ingest.results import PageText, RangePlan
from harness.reproducibility import record_hash

PageNumber = Annotated[int, Field(ge=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Score = Annotated[float, Field(allow_inf_nan=False)]


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _PageSpan(_Evidence):
    first_page: PageNumber
    last_page: PageNumber

    @model_validator(mode="after")
    def _ordered(self) -> _PageSpan:
        if self.last_page < self.first_page:
            msg = f"range {self.first_page}-{self.last_page} ends before it starts"
            raise ValueError(msg)
        return self


class PageFingerprint(_Evidence):
    page_no: PageNumber
    sha256: Sha256


class LocatedRange(_PageSpan):
    type: str
    score: Score
    rank: PageNumber


class ConversionSpan(_PageSpan):
    ocr: str
    ocr_language: str | None
    image_pages: tuple[PageNumber, ...]
    renderer: str


class DocumentEvidence(_Evidence):
    status: Literal["complete", "incomplete"]
    reason: str | None
    page_count: PageNumber | None
    pages_sha256: Sha256 | None
    pages: tuple[PageFingerprint, ...]
    locate_version: str | None
    ranges: tuple[LocatedRange, ...]
    conversion: Literal["read", "not_run"] | None
    conversion_spans: tuple[ConversionSpan, ...]
    conversion_settings_hash: str | None
    ocr_key: str | None

    @model_validator(mode="after")
    def _complete_is_consistent(self) -> DocumentEvidence:
        if self.status != "complete":
            return self
        if self.page_count != len(self.pages):
            msg = f"page_count {self.page_count!r} does not match {len(self.pages)} page records"
            raise ValueError(msg)
        if [item.page_no for item in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError(f"pages must be numbered 1..{len(self.pages)} without gaps")
        if self.pages_sha256 != record_hash([item.sha256 for item in self.pages]):
            raise ValueError("pages_sha256 does not match the page hashes")
        for name in ("locate_version", "ocr_key"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        if self.conversion == "read":
            if not self.conversion_settings_hash:
                raise ValueError("conversion_settings_hash must be a non-empty string")
        elif self.conversion == "not_run":
            if self.conversion_settings_hash is not None or self.conversion_spans:
                raise ValueError("conversion not_run cannot carry a settings hash or spans")
        else:
            raise ValueError("conversion must be read or not_run")
        return self

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _validated[M: BaseModel](model: type[M], value: object) -> M:
    """Validate a decoded JSON value without coercing any of it."""
    return model.model_validate_json(json.dumps(value), strict=True)


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


def incomplete_evidence(
    reason: str, conversion: Literal["read", "not_run"] | None
) -> DocumentEvidence:
    return DocumentEvidence(
        status="incomplete",
        reason=reason,
        page_count=None,
        pages_sha256=None,
        pages=(),
        locate_version=None,
        ranges=(),
        conversion=conversion,
        conversion_spans=(),
        conversion_settings_hash=None,
        ocr_key=None,
    )


def _serialized(item: _Evidence) -> str:
    return json.dumps(item.model_dump(mode="json"), sort_keys=True)


def _span(plan: RangePlan) -> ConversionSpan:
    return ConversionSpan(
        first_page=plan.first_page,
        last_page=plan.last_page,
        ocr=plan.ocr,
        ocr_language=plan.ocr_language,
        image_pages=plan.image_pages,
        renderer=plan.renderer,
    )


def _stamp(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _conversion_spans(
    convert: dict[str, Any], locate: dict[str, Any]
) -> tuple[ConversionSpan, ...]:
    """The saved plans, once convert.json is shown to belong to the same run as locate.json."""
    if convert["sha256"] != locate["document"]["sha256"]:
        raise ValueError("convert.json sha256 differs from locate.json document sha256")
    if convert["locate_version"] != locate["version"]:
        raise ValueError("convert.json locate_version differs from locate.json version")
    plan_values = convert.get("settings_plans")
    if not isinstance(plan_values, list):
        raise ValueError("convert.json settings_plans is missing, null or not a list")
    plans = [_validated(RangePlan, item) for item in plan_values]
    page_count = locate["document"]["page_count"]
    for plan in plans:
        outside = [
            n for n in (plan.first_page, plan.last_page, *plan.image_pages) if n > page_count
        ]
        if outside:
            raise ValueError(f"plan pages {outside} lie beyond locate page_count {page_count}")
    return tuple(_span(plan) for plan in plans)


def document_evidence(artifact_dir: Path, *, converted: bool) -> DocumentEvidence:
    """Read the page cache and saved locate/convert records; never open or process a PDF.

    convert.json is read only when this run converted the document: a declined document leaves
    whatever an earlier run saved there, which is not this run's evidence.
    """
    conversion: Literal["read", "not_run"] = "read" if converted else "not_run"
    paths = [artifact_dir / f"pages.v{PAGES_STAGE_VERSION}.json", artifact_dir / "locate.json"]
    if converted:
        paths.append(artifact_dir / "convert.json")
    try:
        before = [_stamp(path) for path in paths]
        pages_payload = json.loads(paths[0].read_text(encoding="utf-8"))
        if (
            not isinstance(pages_payload, dict)
            or pages_payload.get("version") != PAGES_STAGE_VERSION
        ):
            raise ValueError(f"page cache version must be {PAGES_STAGE_VERSION}")
        page_values = pages_payload["pages"]
        if not isinstance(page_values, list) or not page_values:
            raise ValueError("page cache has no pages")
        pages = fingerprint_pages(tuple(PageText.model_validate(value) for value in page_values))

        locate = _load_object(paths[1])
        range_values = locate["ranges"]
        if not isinstance(range_values, list):
            raise ValueError("locate ranges must be a list")
        ranges = tuple(_validated(LocatedRange, item) for item in range_values)
        spans: tuple[ConversionSpan, ...] = ()
        settings_hash = None
        if converted:
            convert = _load_object(paths[2])
            spans = _conversion_spans(convert, locate)
            settings_hash = convert["settings_hash"]
        if [_stamp(path) for path in paths] != before:
            return incomplete_evidence("artifacts changed while being read", conversion)
        return DocumentEvidence(
            status="complete",
            reason=None,
            page_count=locate["document"]["page_count"],
            pages_sha256=record_hash([item.sha256 for item in pages]),
            pages=pages,
            locate_version=locate["version"],
            ranges=ranges,
            conversion=conversion,
            conversion_spans=spans,
            conversion_settings_hash=settings_hash,
            ocr_key=locate["ocr_key"],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError) as exc:
        return incomplete_evidence(f"{artifact_dir}: {exc}", conversion)


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
    before_ranges = {_serialized(item) for item in previous.ranges}
    after_ranges = {_serialized(item) for item in current.ranges}
    lost = [json.loads(item) for item in sorted(before_ranges - after_ranges)]
    gained = [json.loads(item) for item in sorted(after_ranges - before_ranges)]
    conversion_changed = (
        {"previous": previous.conversion, "current": current.conversion}
        if previous.conversion != current.conversion
        else None
    )
    conversion_spans_changed = previous.conversion_spans != current.conversion_spans
    previous_spans = {_serialized(item) for item in previous.conversion_spans}
    current_spans = {_serialized(item) for item in current.conversion_spans}
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
    elif conversion_changed:
        status = "conversion run changed"
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
        "conversion_changed": conversion_changed,
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
        return incomplete_evidence("malformed locate_evidence: expected an object", None)
    if value.get("status") == "incomplete":
        reason = value.get("reason")
        return incomplete_evidence(
            f"earlier evidence incomplete: {reason or 'no reason provided'}", None
        )
    if value.get("status") != "complete":
        return incomplete_evidence(
            "malformed locate_evidence: status must be complete or incomplete", None
        )
    try:
        return _validated(DocumentEvidence, value)
    except (ValidationError, ValueError, TypeError, OverflowError) as exc:
        return incomplete_evidence(f"malformed locate_evidence: {exc}", None)
