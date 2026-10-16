"""A job through the worker's pipeline, with the stages stood in for: the timeline, the
analysis it leaves, a decline, a page limit and a failing stage."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from fra_core.jobs import JobSettings, JobStore
from fra_core.schemas import (
    BBox,
    Document,
    JobStage,
    JobStatus,
    MetricInput,
    MetricUnit,
    MetricValue,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementReview,
    StatementType,
)
from fra_ingest.industry import UNCERTAIN_MARGIN, VERDICT_THRESHOLD, WHOLE_DOCUMENT_MIN_CUES
from fra_ingest.results import (
    ConvertResult,
    IndustryDecision,
    IndustrySignal,
    LocateResult,
    RangeConversion,
    StatementRange,
    StructureResult,
)
from fra_worker.main import drain
from fra_worker.pipeline import Pipeline, run_job

PDF = b"%PDF-1.7\n%worker test\n"
SETTINGS = JobSettings(root=Path("unused"), max_upload_bytes=10_000, max_pages=50, poll_seconds=1)
PERIOD = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BANK = IndustrySignal(
    kind="bank",
    score=VERDICT_THRESHOLD + UNCERTAIN_MARGIN,
    distinct_cues=WHOLE_DOCUMENT_MIN_CUES,
    evidence=[(3, "total deposits")],
)


def located(sha256: str, pages: int = 10, industry: IndustrySignal | None = None) -> LocateResult:
    return LocateResult(
        version="v",
        document=Document(sha256=sha256, filename="r.pdf", page_count=pages, language="ar"),
        pages=[],
        ranges=[
            StatementRange(type=StatementType.BALANCE, first_page=3, last_page=3, score=8, rank=1)
        ],
        convert_ranges=[(2, 4)],
        industry=industry or IndustrySignal(kind="corporate"),
    )


def converted(sha256: str) -> ConvertResult:
    return ConvertResult(
        version="v",
        sha256=sha256,
        locate_version="v",
        docling_version="d",
        device="cpu",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=2, last_page=4, ocr="pdf_aware", ocr_language=None, status="ok"
            )
        ],
        peak_footprint_gb=1.0,
    )


def structured(sha256: str, review: str = "passed") -> StructureResult:
    statement = Statement(
        id="s",
        document_sha256=sha256,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[PERIOD],
    )
    return StructureResult(
        version="v",
        sha256=sha256,
        convert_version="v",
        settings_hash="h",
        statements=[statement],
        reviews=[
            StatementReview(
                statement_id="s",
                status=review,  # type: ignore[arg-type]
                numeric_cells=1,
                checked_cells=1,
                flagged_cells=0,
            )
        ],
    )


def declined(sha256: str) -> StructureResult:
    return StructureResult(
        version="v",
        sha256=sha256,
        convert_version="",
        settings_hash="",
        industry=IndustryDecision(
            outcome="declined", code="bank", signal=BANK, reason="declined:bank: total deposits"
        ),
        flags=["declined:bank"],
    )


def metric(value: float | None) -> MetricValue:
    cell = MetricInput(
        statement_id="s",
        line_item_id="r1",
        canonical_id="total_current_assets",
        period_key=PERIOD.key,
        reported=Decimal("3"),
        scale=1000,
        currency="SAR",
        provenance=Provenance(
            page_no=3,
            bbox=BBox(left=1, top=1, right=2, bottom=2),
            table_ref="#/tables/0",
            row=1,
            col=1,
        ),
    )
    return MetricValue(
        metric_id="current_ratio",
        period_key=PERIOD.key,
        value=value,
        unit=MetricUnit.TIMES,
        inputs={"numerator": [cell]} if value is not None else {},
        formula="a / b",
        formula_version="1",
        flags=[] if value is not None else ["missing_input:total_current_liabilities"],
    )


class Calls:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.conversions: list[ConvertResult | None] = []


def pipeline_for(
    sha256: str,
    calls: Calls,
    *,
    pages: int = 10,
    industry: IndustrySignal | None = None,
    result: StructureResult | None = None,
    fail_at: str | None = None,
) -> Pipeline:
    def step(name: str) -> None:
        calls.names.append(name)
        if name == fail_at:
            raise RuntimeError(f"{name} broke")

    def locate(pdf: Path) -> LocateResult:
        step("locate")
        assert pdf.read_bytes() == PDF  # the stored upload is what each stage reads
        return located(sha256, pages, industry)

    def convert(pdf: Path, loc: LocateResult) -> ConvertResult:
        step("convert")
        return converted(sha256)

    def structure(pdf: Path, conversion: ConvertResult | None) -> StructureResult:
        step("structure")
        calls.conversions.append(conversion)
        return result or structured(sha256)

    def metrics(statements: Sequence[Statement]) -> list[MetricValue]:
        step("metrics")
        return [metric(1.5), metric(None)]

    def count(pdf: Path) -> int:
        assert pdf.read_bytes() == PDF
        return pages

    return Pipeline(
        page_count=count, locate=locate, convert=convert, structure=structure, metrics=metrics
    )


def queued(tmp_path: Path) -> tuple[JobStore, str]:
    store = JobStore(tmp_path / "jobs")
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    store.enqueue(upload.sha256)
    return store, upload.sha256


def run_one(store: JobStore, pipeline: Pipeline, max_pages: int = 50):  # type: ignore[no-untyped-def]
    job = store.claim()
    assert job is not None
    return run_job(store, job, pipeline, max_pages)


def test_a_job_runs_each_stage_in_order_and_leaves_its_analysis(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    calls = Calls()
    job = run_one(store, pipeline_for(sha, calls))
    assert job.status is JobStatus.COMPLETED
    assert calls.names == ["locate", "convert", "structure", "metrics"]
    assert calls.conversions == [converted(sha)]  # structure reads this job's conversion
    timeline = [(e.kind, e.stage, e.detail) for e in store.events(job.id)]
    assert timeline == [
        ("queued", None, None),
        ("started", JobStage.LOCATE, None),
        ("finished", JobStage.LOCATE, "10 pages, 0 without a text layer, 1 statement ranges"),
        ("started", JobStage.CONVERT, None),
        ("finished", JobStage.CONVERT, "1 page ranges"),
        ("started", JobStage.STRUCTURE, None),
        ("finished", JobStage.STRUCTURE, "1 statements"),
        ("started", JobStage.METRICS, None),
        ("finished", JobStage.METRICS, "1 of 2 values computed"),
        ("completed", None, None),
    ]
    analysis = store.load_analysis(job.id)
    assert analysis is not None
    assert analysis.page_count == 10 and analysis.language == "ar"
    assert [s.id for s in analysis.statements] == ["s"]
    assert [m.value for m in analysis.metrics] == [1.5, None]
    assert analysis.decline is None and analysis.narrative is None


def test_a_statement_held_for_review_ends_the_job_in_needs_review(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    job = run_one(store, pipeline_for(sha, Calls(), result=structured(sha, "needs_review")))
    assert job.status is JobStatus.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("status", "flag"),
    [
        ("partial", "range_partially_converted:2-4"),
        ("failed", "range_not_converted:2-4"),
        ("skipped", "range_not_converted:2-4"),
    ],
)
def test_incomplete_conversion_holds_even_passing_statements(
    tmp_path: Path, status: str, flag: str
) -> None:
    store, sha = queued(tmp_path)
    conversion = converted(sha)
    conversion = conversion.model_copy(
        update={"ranges": [conversion.ranges[0].model_copy(update={"status": status})]}
    )
    pipeline = replace(pipeline_for(sha, Calls()), convert=lambda pdf, located: conversion)
    job = run_one(store, pipeline)
    analysis = store.load_analysis(job.id)
    assert job.status is JobStatus.NEEDS_REVIEW
    assert analysis is not None and analysis.flags == [flag]
    assert analysis.reviews[0].status == "passed"
    assert store.events(job.id)[-1].kind == "needs_review"


def test_conversion_diagnostics_survive_and_flags_are_deduplicated(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    flag = "range_partially_converted:2-4"
    conversion = converted(sha).model_copy(update={"flags": ["conversion_warning"]})
    conversion = conversion.model_copy(
        update={
            "ranges": [
                conversion.ranges[0].model_copy(
                    update={"status": "partial", "flags": ["convert_partial:2-4"]}
                )
            ]
        }
    )
    result = structured(sha).model_copy(update={"flags": [flag]})
    pipeline = replace(
        pipeline_for(sha, Calls(), result=result), convert=lambda pdf, located: conversion
    )
    job = run_one(store, pipeline)
    analysis = store.load_analysis(job.id)
    assert analysis is not None
    assert analysis.flags == ["conversion_warning", flag, "convert_partial:2-4"]
    assert job.status is JobStatus.NEEDS_REVIEW


@pytest.mark.parametrize("status", ["unknown", None, "missing"])
def test_invalid_range_status_fails_at_the_conversion_validation_boundary(
    tmp_path: Path, status: str | None
) -> None:
    store, sha = queued(tmp_path)
    data = converted(sha).model_dump()
    if status == "missing":
        del data["ranges"][0]["status"]
    else:
        data["ranges"][0]["status"] = status

    def convert(pdf: Path, located: LocateResult) -> ConvertResult:
        return ConvertResult.model_validate(data)

    with pytest.raises(ValidationError, match=r"ranges\.0\.status"):
        ConvertResult.model_validate(data)
    job = run_one(store, replace(pipeline_for(sha, Calls()), convert=convert))
    assert job.status is JobStatus.FAILED and job.stage is JobStage.CONVERT
    assert job.error is not None and "ValidationError" in job.error and "status" in job.error
    assert store.load_analysis(job.id) is None


def test_a_bank_is_declined_without_convert_or_metrics_and_says_why(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    calls = Calls()
    job = run_one(store, pipeline_for(sha, calls, industry=BANK, result=declined(sha)))
    assert job.status is JobStatus.NEEDS_REVIEW
    assert calls.names == ["locate", "structure"]
    assert calls.conversions == [None]
    details = {(e.stage, e.detail) for e in store.events(job.id) if e.kind == "finished"}
    assert (JobStage.CONVERT, "skipped: declined (bank)") in details
    assert (JobStage.METRICS, "skipped: declined") in details
    analysis = store.load_analysis(job.id)
    assert analysis is not None and analysis.decline is not None
    assert analysis.decline.code == "bank" and "total deposits" in analysis.decline.reason
    assert analysis.flags == ["declined:bank"]


def test_a_pdf_over_the_page_limit_fails_before_any_page_is_read(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    calls = Calls()
    job = run_one(store, pipeline_for(sha, calls, pages=51), max_pages=50)
    assert job.status is JobStatus.FAILED and job.stage is JobStage.LOCATE
    assert job.error == "PageLimitExceeded: the PDF has 51 pages; the limit is 50"
    assert calls.names == []  # refused before locate read a single page
    assert store.load_analysis(job.id) is None


@pytest.mark.parametrize("stage", ["locate", "convert", "structure", "metrics"])
def test_a_stage_that_raises_fails_the_job_at_that_stage_with_its_message(
    tmp_path: Path, stage: str
) -> None:
    store, sha = queued(tmp_path)
    job = run_one(store, pipeline_for(sha, Calls(), fail_at=stage))
    assert job.status is JobStatus.FAILED
    assert job.stage is JobStage(stage)
    assert job.error == f"RuntimeError: {stage} broke"
    assert store.events(job.id)[-1].kind == "failed"


def test_drain_runs_every_queued_job_and_stops_when_none_is_left(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    store.enqueue(sha)
    calls = Calls()
    assert drain(store, pipeline_for(sha, calls), max_pages=50) == 2
    assert calls.names.count("locate") == 2
    assert drain(store, pipeline_for(sha, calls), max_pages=50) == 0


@pytest.mark.parametrize(
    ("locate_flags", "structure_flags", "expected_flags"),
    [
        (["statement_not_found:income"], [], ["statement_not_found:income"]),
        (["image_pages_not_read:2"], [], ["image_pages_not_read:2"]),
        (["ocr_failed_pages:1"], [], ["ocr_failed_pages:1"]),
        (["garbled_text_layers_not_read:1"], [], ["garbled_text_layers_not_read:1"]),
        ([], ["statement_not_extracted:income"], ["statement_not_extracted:income"]),
        ([], ["range_not_converted:5-6"], ["range_not_converted:5-6"]),
        (["future_failure:page7"], [], ["future_failure:page7"]),
        (
            ["ocr_failed_pages:1", "ocr_failed_pages:1"],
            ["ocr_failed_pages:1", "range_not_converted:5-6"],
            ["ocr_failed_pages:1", "range_not_converted:5-6"],
        ),
    ],
)
def test_partial_extraction_keeps_document_flags_and_needs_review(
    tmp_path: Path,
    locate_flags: list[str],
    structure_flags: list[str],
    expected_flags: list[str],
) -> None:
    store, sha = queued(tmp_path)
    loc = located(sha).model_copy(update={"flags": locate_flags})
    result = structured(sha).model_copy(update={"flags": structure_flags})
    pipeline = replace(pipeline_for(sha, Calls(), result=result), locate=lambda pdf: loc)
    job = run_one(store, pipeline)
    assert job.status is JobStatus.NEEDS_REVIEW
    analysis = store.load_analysis(job.id)
    assert analysis is not None
    assert analysis.flags == expected_flags
    assert analysis.status() is JobStatus.NEEDS_REVIEW
    assert store.events(job.id)[-1].kind == "needs_review"


def test_low_selectivity_is_preserved_without_holding_complete_extraction(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    loc = located(sha).model_copy(update={"flags": ["low_selectivity"]})
    pipeline = replace(pipeline_for(sha, Calls()), locate=lambda pdf: loc)
    job = run_one(store, pipeline)
    assert job.status is JobStatus.COMPLETED
    analysis = store.load_analysis(job.id)
    assert analysis is not None and analysis.flags == ["low_selectivity"]


def test_no_statements_keeps_the_document_failure_reasons(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    result = structured(sha).model_copy(
        update={"statements": [], "reviews": [], "flags": ["statement_not_extracted:balance"]}
    )
    job = run_one(store, pipeline_for(sha, Calls(), result=result))
    analysis = store.load_analysis(job.id)
    assert job.status is JobStatus.NEEDS_REVIEW
    assert analysis is not None and analysis.flags == ["statement_not_extracted:balance"]
