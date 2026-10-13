"""A job through the worker's pipeline, with the stages stood in for: the timeline, the
analysis it leaves, a decline, a page limit and a failing stage."""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

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

    def structure(pdf: Path) -> StructureResult:
        step("structure")
        return result or structured(sha256)

    def metrics(statements: Sequence[Statement]) -> list[MetricValue]:
        step("metrics")
        return [metric(1.5), metric(None)]

    return Pipeline(locate=locate, convert=convert, structure=structure, metrics=metrics)


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


def test_a_bank_is_declined_without_convert_or_metrics_and_says_why(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    calls = Calls()
    job = run_one(store, pipeline_for(sha, calls, industry=BANK, result=declined(sha)))
    assert job.status is JobStatus.NEEDS_REVIEW
    assert calls.names == ["locate", "structure"]
    details = {(e.stage, e.detail) for e in store.events(job.id) if e.kind == "finished"}
    assert (JobStage.CONVERT, "skipped: declined (bank)") in details
    assert (JobStage.METRICS, "skipped: declined") in details
    analysis = store.load_analysis(job.id)
    assert analysis is not None and analysis.decline is not None
    assert analysis.decline.code == "bank" and "total deposits" in analysis.decline.reason


def test_a_pdf_over_the_page_limit_fails_at_locate_before_convert(tmp_path: Path) -> None:
    store, sha = queued(tmp_path)
    calls = Calls()
    job = run_one(store, pipeline_for(sha, calls, pages=51), max_pages=50)
    assert job.status is JobStatus.FAILED and job.stage is JobStage.LOCATE
    assert job.error == "PageLimitExceeded: the PDF has 51 pages; the limit is 50"
    assert calls.names == ["locate"]
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
