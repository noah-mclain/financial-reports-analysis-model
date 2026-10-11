import json
from pathlib import Path

import pytest
from harness.locate_evidence import (
    compare_evidence,
    document_evidence,
    evidence_from_dict,
    fingerprint_pages,
)

from fra_ingest.results import PageText


def page(**changes: object) -> PageText:
    values: dict[str, object] = {
        "page_no": 1,
        "mode": "image",
        "source": "ocr",
        "header_text": "Balance sheet",
        "body_text": "Assets ١٢٣",
        "char_count": 15,
        "width_pt": 612.0,
        "height_pt": 792.0,
        "arabic_chars": 3,
        "latin_chars": 10,
        "visual_arabic": False,
        "ocr_language": "ar-SA",
        "ocr_seconds": 1.0,
        "ocr_call_seconds": [1.0],
        "flags": [],
    }
    values.update(changes)
    return PageText.model_validate(values)


def _artifacts(
    root: Path, *, header: str = "Balance sheet", ranges: list[dict] | None = None
) -> None:
    root.mkdir()
    pages = [page(header_text=header)]
    (root / "pages.v5.json").write_text(
        json.dumps(
            {"version": "5", "settings": {}, "pages": [p.model_dump(mode="json") for p in pages]}
        ),
        encoding="utf-8",
    )
    (root / "locate.json").write_text(
        json.dumps(
            {
                "version": "7",
                "ocr_key": "pages-key",
                "document": {"page_count": 1},
                "ranges": ranges
                if ranges is not None
                else [
                    {"type": "balance", "first_page": 1, "last_page": 1, "score": 5.5, "rank": 1}
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "convert.json").write_text(
        json.dumps(
            {
                "settings_hash": "settings",
                "settings_plans": [
                    {
                        "first_page": 1,
                        "last_page": 1,
                        "ocr": "full_page",
                        "ocr_language": "ar-SA",
                        "renderer": "pdfium",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_page_fingerprint_omits_only_timing_and_preserves_text_exactly() -> None:
    baseline = fingerprint_pages([page()])
    assert fingerprint_pages([page(ocr_seconds=99, ocr_call_seconds=[99])]) == baseline
    changes = dict(
        (
            ("header_text", "Balance sheet "),
            ("body_text", "Assets 123"),
            ("flags", ["ocr_failed"]),
            ("ocr_language", "en-US"),
            ("mode", "text"),
            ("source", "text"),
            ("width_pt", 613.0),
            ("height_pt", 793.0),
            ("visual_arabic", True),
            ("char_count", 16),
            ("arabic_chars", 4),
            ("latin_chars", 11),
        )
    )
    for field in PageText.model_fields:
        if field == "page_no":
            with pytest.raises(ValueError, match="missing page numbers"):
                fingerprint_pages([page(page_no=2)])
        elif field not in {"ocr_seconds", "ocr_call_seconds"}:
            assert field in dict(changes), f"fingerprint test must vary PageText field {field}"
    for field, value in changes.items():
        assert fingerprint_pages([page(**{field: value})]) != baseline


def test_page_fingerprint_rejects_duplicate_or_missing_page_numbers() -> None:
    with pytest.raises(ValueError, match="duplicate page_no"):
        fingerprint_pages([page(), page()])
    with pytest.raises(ValueError, match="page_no"):
        fingerprint_pages([{"header_text": "missing"}])  # type: ignore[list-item]


def test_document_evidence_reads_artifacts_and_marks_missing_or_malformed_incomplete(
    tmp_path: Path,
) -> None:
    root = tmp_path / "doc"
    _artifacts(root)
    evidence = document_evidence(root)
    assert evidence.status == "complete"
    assert evidence.page_count == 1
    assert evidence.locate_version == "7"
    assert evidence.ocr_key == "pages-key"
    assert evidence.ranges[0].type == "balance"
    assert evidence.conversion_spans[0].renderer == "pdfium"
    assert document_evidence(tmp_path / "missing").status == "incomplete"
    (root / "locate.json").write_text("{", encoding="utf-8")
    malformed = document_evidence(root)
    assert malformed.status == "incomplete" and malformed.reason


def test_compare_reports_changed_page_and_lost_range_and_identical(tmp_path: Path) -> None:

    old, new = tmp_path / "old", tmp_path / "new"
    _artifacts(old)
    _artifacts(new, header="Balance sheet continued", ranges=[])
    assert (
        compare_evidence(document_evidence(old), document_evidence(new))["status"]
        == "inputs changed"
    )
    comparison = compare_evidence(document_evidence(old), document_evidence(new))
    assert comparison["changed_pages"] == [1]
    assert comparison["lost_ranges"] == [
        {"type": "balance", "first_page": 1, "last_page": 1, "score": 5.5, "rank": 1}
    ]
    assert compare_evidence(document_evidence(old), document_evidence(old))["status"] == "unchanged"
    serialized = document_evidence(old).as_dict()
    assert evidence_from_dict(serialized) == document_evidence(old)
    assert evidence_from_dict(None) is None


def test_compare_separates_ranges_spans_settings_and_locate_version(tmp_path: Path) -> None:
    from harness.locate_evidence import DocumentEvidence

    root = tmp_path / "base"
    _artifacts(root)
    base = document_evidence(root)

    def changed(**values: object) -> DocumentEvidence:
        return DocumentEvidence(**{**base.__dict__, **values})

    equal_pages_ranges = changed(ranges=())
    range_result = compare_evidence(base, equal_pages_ranges)
    assert range_result["status"] == "ranges changed"
    assert range_result["changed_pages"] == []
    assert range_result["lost_ranges"] and not range_result["gained_ranges"]
    gained = compare_evidence(equal_pages_ranges, base)
    assert gained["gained_ranges"] and not gained["lost_ranges"]

    span = base.conversion_spans[0]
    spans_result = compare_evidence(
        base, changed(conversion_spans=(span.__class__(1, 1, "en", "pdfium"),))
    )
    assert spans_result["conversion_spans_changed"] is True
    assert spans_result["status"] == "conversion spans changed"

    hash_result = compare_evidence(base, changed(conversion_settings_hash="other"))
    assert hash_result["conversion_settings_changed"] is True
    assert hash_result["conversion_spans_changed"] is False
    assert "plan changed" not in hash_result["status"]

    version_result = compare_evidence(base, changed(locate_version="8"))
    assert version_result["locate_version_changed"] == {"previous": "7", "current": "8"}
    assert "locate version" in version_result["status"]

    key_result = compare_evidence(base, changed(ocr_key="other-pages-key"))
    assert key_result["ocr_key_changed"] == {
        "previous": "pages-key",
        "current": "other-pages-key",
    }


def test_incomplete_or_malformed_earlier_evidence_has_a_reason() -> None:
    assert evidence_from_dict({"status": "incomplete", "reason": "missing cache"}).reason == (
        "earlier evidence incomplete: missing cache"
    )
    assert "malformed" in evidence_from_dict({"status": "complete"}).reason
