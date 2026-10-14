import copy
import hashlib
import json
from pathlib import Path
from typing import get_args

import pytest
from harness.locate_evidence import (
    compare_evidence,
    document_evidence,
    evidence_from_dict,
    fingerprint_pages,
)
from harness.reproducibility import record_hash

from fra_ingest.results import PageText, RangeOcr, RangeRenderer


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


DEFAULT_PLANS: list[dict[str, object]] = [
    {
        "first_page": 1,
        "last_page": 1,
        "ocr": "full_page",
        "ocr_language": "ar-SA",
        "renderer": "pdfium",
    }
]


DOCUMENT_SHA = "d" * 64


def _artifacts(
    root: Path,
    *,
    header: str = "Balance sheet",
    ranges: list[dict] | None = None,
    plans: list[dict[str, object]] | None = DEFAULT_PLANS,
    page_total: int = 1,
    convert_sha: str = DOCUMENT_SHA,
    convert_locate_version: str = "7",
) -> None:
    root.mkdir()
    pages = [page(header_text=header)] + [page(page_no=n) for n in range(2, page_total + 1)]
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
                "document": {"page_count": page_total, "sha256": DOCUMENT_SHA},
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
                "sha256": convert_sha,
                "locate_version": convert_locate_version,
                "settings_hash": "settings",
                "settings_plans": plans,
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
    evidence = document_evidence(root, converted=True)
    assert evidence.status == "complete"
    assert evidence.page_count == 1
    assert evidence.locate_version == "7"
    assert evidence.ocr_key == "pages-key"
    assert evidence.ranges[0].type == "balance"
    assert evidence.conversion_spans[0].renderer == "pdfium"
    assert document_evidence(tmp_path / "missing", converted=True).status == "incomplete"
    (root / "locate.json").write_text("{", encoding="utf-8")
    malformed = document_evidence(root, converted=True)
    assert malformed.status == "incomplete" and malformed.reason


def test_compare_reports_changed_page_and_lost_range_and_identical(tmp_path: Path) -> None:

    old, new = tmp_path / "old", tmp_path / "new"
    _artifacts(old)
    _artifacts(new, header="Balance sheet continued", ranges=[])
    assert (
        compare_evidence(
            document_evidence(old, converted=True), document_evidence(new, converted=True)
        )["status"]
        == "inputs changed"
    )
    comparison = compare_evidence(
        document_evidence(old, converted=True), document_evidence(new, converted=True)
    )
    assert comparison["changed_pages"] == [1]
    assert comparison["lost_ranges"] == [
        {"type": "balance", "first_page": 1, "last_page": 1, "score": 5.5, "rank": 1}
    ]
    assert (
        compare_evidence(
            document_evidence(old, converted=True), document_evidence(old, converted=True)
        )["status"]
        == "unchanged"
    )
    serialized = document_evidence(old, converted=True).as_dict()
    assert evidence_from_dict(serialized) == document_evidence(old, converted=True)
    assert evidence_from_dict(None) is None


def test_compare_separates_ranges_spans_settings_and_locate_version(tmp_path: Path) -> None:
    from harness.locate_evidence import DocumentEvidence

    root = tmp_path / "base"
    _artifacts(root)
    base = document_evidence(root, converted=True)

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
        base, changed(conversion_spans=(span.model_copy(update={"ocr_language": "en"}),))
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


def _plan(**changes: object) -> dict[str, object]:
    return {
        "first_page": 1,
        "last_page": 5,
        "ocr": "pdf_aware",
        "ocr_language": "ar-SA",
        "image_pages": [4],
        "renderer": "pdfium",
        **changes,
    }


def test_conversion_span_carries_ocr_mode_and_image_pages(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root, plans=[_plan()], page_total=5)
    span = document_evidence(root, converted=True).conversion_spans[0]
    assert (span.ocr, span.image_pages) == ("pdf_aware", (4,))


def test_plan_ocr_mode_change_is_a_conversion_span_change(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    _artifacts(old, plans=[_plan(ocr="pdf_aware")], page_total=5)
    _artifacts(new, plans=[_plan(ocr="full_page")], page_total=5)
    comparison = compare_evidence(
        document_evidence(old, converted=True), document_evidence(new, converted=True)
    )
    assert comparison["conversion_spans_changed"] is True
    assert comparison["status"] == "conversion spans changed"
    assert [item["ocr"] for item in comparison["lost_conversion_spans"]] == ["pdf_aware"]
    assert [item["ocr"] for item in comparison["gained_conversion_spans"]] == ["full_page"]
    assert comparison["conversion_settings_changed"] is False


def test_plan_image_pages_change_is_a_conversion_span_change(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    _artifacts(old, plans=[_plan(image_pages=[4])], page_total=5)
    _artifacts(new, plans=[_plan(image_pages=[4, 5])], page_total=5)
    comparison = compare_evidence(
        document_evidence(old, converted=True), document_evidence(new, converted=True)
    )
    assert comparison["conversion_spans_changed"] is True
    assert comparison["lost_conversion_spans"][0]["image_pages"] == [4]
    assert comparison["gained_conversion_spans"][0]["image_pages"] == [4, 5]


@pytest.mark.parametrize("plans", [None, "missing"])
def test_missing_or_null_settings_plans_is_incomplete_not_an_empty_plan(
    tmp_path: Path, plans: str | None
) -> None:
    root = tmp_path / "doc"
    _artifacts(root, plans=None)
    if plans == "missing":
        convert = json.loads((root / "convert.json").read_text(encoding="utf-8"))
        del convert["settings_plans"]
        (root / "convert.json").write_text(json.dumps(convert), encoding="utf-8")
    evidence = document_evidence(root, converted=True)
    assert evidence.status == "incomplete"
    assert evidence.reason and "settings_plans" in evidence.reason


@pytest.mark.parametrize(
    "plan",
    [
        _plan(unknown="x"),
        _plan(ocr="partial"),
        _plan(first_page="1"),
        _plan(first_page=3, last_page=2),
        _plan(image_pages=[True]),
        _plan(renderer=None),
    ],
    ids=["extra key", "ocr mode", "numeric string", "reversed", "bool page", "null renderer"],
)
def test_malformed_plan_makes_document_evidence_incomplete(
    tmp_path: Path, plan: dict[str, object]
) -> None:
    root = tmp_path / "doc"
    _artifacts(root, plans=[plan], page_total=5)
    assert document_evidence(root, converted=True).status == "incomplete"


@pytest.mark.parametrize(
    "item",
    [
        {"type": "balance", "first_page": "1", "last_page": 1, "score": 5.5, "rank": 1},
        {"type": "balance", "first_page": 1, "last_page": 1, "score": "5.5", "rank": 1},
        {"type": "balance", "first_page": 1, "last_page": 1, "score": 5.5, "rank": 1.0},
        {"type": 7, "first_page": 1, "last_page": 1, "score": 5.5, "rank": 1},
        {"type": "balance", "first_page": 1, "last_page": 1, "score": 5.5, "rank": 0},
        {"type": "balance", "first_page": 1, "last_page": 1, "score": 5.5},
        None,
    ],
)
def test_malformed_located_range_makes_document_evidence_incomplete(
    tmp_path: Path, item: object
) -> None:
    root = tmp_path / "doc"
    _artifacts(root, ranges=[item])  # type: ignore[list-item]
    assert document_evidence(root, converted=True).status == "incomplete"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _valid_dict(page_total: int = 2) -> dict[str, object]:
    hashes = [_sha(f"page {number}") for number in range(1, page_total + 1)]
    return {
        "status": "complete",
        "reason": None,
        "page_count": page_total,
        "pages_sha256": record_hash(hashes),
        "pages": [{"page_no": n, "sha256": h} for n, h in enumerate(hashes, start=1)],
        "locate_version": "7",
        "ranges": [{"type": "balance", "first_page": 1, "last_page": 2, "score": 5.5, "rank": 1}],
        "conversion_spans": [
            {
                "first_page": 1,
                "last_page": 2,
                "ocr": "pdf_aware",
                "ocr_language": None,
                "image_pages": [2],
                "renderer": "default",
            }
        ],
        "conversion": "read",
        "conversion_settings_hash": "settings",
        "ocr_key": "pages-key",
    }


def _mutated(path: tuple[object, ...], value: object) -> dict[str, object]:
    document = copy.deepcopy(_valid_dict())
    target: object = document
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    return document


@pytest.mark.parametrize("ocr", get_args(RangeOcr))
@pytest.mark.parametrize("renderer", get_args(RangeRenderer))
def test_every_ocr_mode_and_renderer_a_plan_can_carry_is_accepted(
    ocr: str, renderer: str, tmp_path: Path
) -> None:
    root = tmp_path / "doc"
    _artifacts(root, plans=[_plan(ocr=ocr, renderer=renderer)], page_total=5)
    read = document_evidence(root, converted=True)
    assert read.status == "complete"
    saved = evidence_from_dict(read.as_dict())
    assert saved is not None and saved.status == "complete"
    assert (saved.conversion_spans[0].ocr, saved.conversion_spans[0].renderer) == (ocr, renderer)


def test_valid_evidence_dict_is_accepted_and_int_score_is_stored_as_float() -> None:
    assert evidence_from_dict(_valid_dict()).status == "complete"  # type: ignore[union-attr]
    integral = evidence_from_dict(_mutated(("ranges", 0, "score"), 5))
    assert integral is not None and integral.status == "complete"
    assert isinstance(integral.ranges[0].score, float) and integral.ranges[0].score == 5.0


def _without(key: str) -> dict[str, object]:
    document = _valid_dict()
    del document[key]
    return document


@pytest.mark.parametrize(
    ("document", "field"),
    [
        (_mutated(("pages_sha256",), None), "pages_sha256"),
        (_mutated(("locate_version",), None), "locate_version"),
        (_mutated(("locate_version",), ""), "locate_version"),
        (_mutated(("ocr_key",), ["pages-key"]), "ocr_key"),
        (_mutated(("ocr_key",), None), "ocr_key"),
        (_mutated(("conversion_settings_hash",), ""), "conversion_settings_hash"),
        (_mutated(("page_count",), "12"), "page_count"),
        (_mutated(("page_count",), True), "page_count"),
        (_mutated(("page_count",), 3), "page_count"),
        (_mutated(("page_count",), 10**400), "page_count"),
        (_mutated(("pages_sha256",), "0" * 64), "pages_sha256"),
        (_mutated(("pages_sha256",), "ABC"), "pages_sha256"),
        (_mutated(("pages", 0, "sha256"), ["a"]), "sha256"),
        (_mutated(("pages", 0, "sha256"), _sha("x").upper()), "sha256"),
        (_mutated(("pages", 1, "page_no"), 3), "pages"),
        (_mutated(("pages", 1, "page_no"), 1), "pages"),
        (_mutated(("ranges", 0, "rank"), 0), "rank"),
        (_mutated(("ranges", 0, "rank"), "1"), "rank"),
        (_mutated(("ranges", 0, "first_page"), 3), "ranges"),
        (_mutated(("ranges", 0, "score"), "5.5"), "score"),
        (_mutated(("ranges", 0, "score"), float("nan")), "score"),
        (_mutated(("ranges", 0, "score"), float("inf")), "score"),
        (_mutated(("ranges", 0, "score"), 10**400), "score"),
        (_mutated(("ranges", 0, "extra"), 1), "extra"),
        (_mutated(("conversion_spans", 0, "image_pages"), [0]), "image_pages"),
        (_mutated(("conversion_spans", 0, "ocr"), None), "ocr"),
        (_mutated(("conversion_spans", 0, "ocr"), "partial"), "ocr"),
        (_mutated(("conversion_spans", 0, "renderer"), "ghost"), "renderer"),
        (_mutated(("conversion_spans", 0, "renderer"), None), "renderer"),
        (_mutated(("pages",), "not pages"), "pages"),
        (_mutated(("extra",), 1), "extra"),
        (_without("conversion_spans"), "conversion_spans"),
        (_without("conversion"), "conversion"),
        (_mutated(("conversion",), "maybe"), "conversion"),
        ({**_valid_dict(), "reason": 5}, "reason"),
    ],
)
def test_malformed_evidence_dict_is_incomplete_never_coerced_or_raised(
    document: dict[str, object], field: str
) -> None:
    evidence = evidence_from_dict(document)
    assert evidence is not None
    assert evidence.status == "incomplete"
    assert evidence.reason and evidence.reason.startswith("malformed locate_evidence")
    assert field in evidence.reason


def test_evidence_round_trips_through_json(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root, plans=[_plan()], page_total=5)
    original = document_evidence(root, converted=True)
    assert original.status == "complete"
    revived = evidence_from_dict(json.loads(json.dumps(original.as_dict())))
    assert revived == original
    assert isinstance(original.as_dict()["pages"], list)


def test_a_declined_document_ignores_a_stale_convert_json(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root, convert_sha="e" * 64)
    convert = json.loads((root / "convert.json").read_text(encoding="utf-8"))
    (root / "convert.json").write_text(
        json.dumps({**convert, "settings_hash": "stale-settings-hash"}), encoding="utf-8"
    )
    evidence = document_evidence(root, converted=False)
    assert evidence.status == "complete"
    assert evidence.conversion == "not_run"
    assert evidence.conversion_spans == ()
    assert evidence.conversion_settings_hash is None
    assert "stale-settings-hash" not in json.dumps(evidence.as_dict())
    assert "e" * 64 not in json.dumps(evidence.as_dict())


def test_a_declined_document_does_not_need_a_convert_json(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root)
    (root / "convert.json").unlink()
    assert document_evidence(root, converted=False).status == "complete"
    assert document_evidence(root, converted=True).status == "incomplete"


def test_read_conversion_is_marked_read(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root)
    assert document_evidence(root, converted=True).conversion == "read"


def test_not_run_evidence_with_a_settings_hash_is_rejected() -> None:
    document = _valid_dict()
    document["conversion"] = "not_run"
    evidence = evidence_from_dict(document)
    assert evidence is not None and evidence.status == "incomplete"
    assert evidence.reason and "conversion" in evidence.reason


def test_convert_json_for_another_document_is_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root, convert_sha="e" * 64)
    evidence = document_evidence(root, converted=True)
    assert evidence.status == "incomplete"
    assert evidence.reason and "sha256" in evidence.reason


def test_convert_json_from_another_locate_version_is_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "doc"
    _artifacts(root, convert_locate_version="6")
    evidence = document_evidence(root, converted=True)
    assert evidence.status == "incomplete"
    assert evidence.reason and "locate_version" in evidence.reason


@pytest.mark.parametrize(
    "plan",
    [_plan(last_page=6), _plan(image_pages=[6])],
    ids=["range past the end", "image page past the end"],
)
def test_a_plan_beyond_the_page_count_is_incomplete(
    tmp_path: Path, plan: dict[str, object]
) -> None:
    root = tmp_path / "doc"
    _artifacts(root, plans=[plan], page_total=5)
    evidence = document_evidence(root, converted=True)
    assert evidence.status == "incomplete"
    assert evidence.reason and "page_count" in evidence.reason


def test_artifacts_rewritten_while_being_read_are_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "doc"
    _artifacts(root)
    read_text = Path.read_text

    def read_then_rewrite(self: Path, *args: object, **kwargs: object) -> str:
        text = read_text(self, *args, **kwargs)  # type: ignore[arg-type]
        if self.name == "locate.json":
            self.write_text(text + " ", encoding="utf-8")
        return text

    monkeypatch.setattr(Path, "read_text", read_then_rewrite)
    evidence = document_evidence(root, converted=True)
    assert evidence.status == "incomplete"
    assert evidence.reason == "artifacts changed while being read"


def test_conversion_run_change_is_reported_before_spans(tmp_path: Path) -> None:
    read, skipped = tmp_path / "read", tmp_path / "skipped"
    _artifacts(read)
    _artifacts(skipped)
    comparison = compare_evidence(
        document_evidence(read, converted=True), document_evidence(skipped, converted=False)
    )
    assert comparison["status"] == "conversion run changed"
    assert comparison["conversion_changed"] == {"previous": "read", "current": "not_run"}
    assert comparison["conversion_spans_changed"] is True


def test_two_runs_that_skipped_conversion_compare_as_unchanged(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    _artifacts(old, convert_sha="e" * 64)
    _artifacts(new, plans=[_plan()], page_total=1, convert_locate_version="3")
    comparison = compare_evidence(
        document_evidence(old, converted=False), document_evidence(new, converted=False)
    )
    assert comparison["status"] == "unchanged"
    assert comparison["conversion_changed"] is None
    assert comparison["conversion_spans_changed"] is False
    assert comparison["conversion_settings_changed"] is False
