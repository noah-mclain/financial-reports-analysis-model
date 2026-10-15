"""One job through the pipeline, with each stage recorded on the job's timeline.

Locate, convert, structure and metrics are timed as separate stages: convert is run on its own
first (``current_convert``), so ``structure_pdf`` then reuses the located pages and the
conversion rather than repeating them. A bank or an insurer is declined after locate, and its
convert and metrics stages are recorded as skipped. Any exception ends the job as failed with
its message; nothing is retried.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from fra_analytics.frame import primary_statements
from fra_analytics.metrics.registry import compute
from fra_analytics.policy import Policy
from fra_core.jobs import JobStore
from fra_core.schemas import (
    Analysis,
    Decline,
    Job,
    JobStage,
    MetricValue,
    Statement,
)
from fra_ingest.child import convert_in_child
from fra_ingest.config import IngestConfig
from fra_ingest.industry import industry_decision
from fra_ingest.ocr import make_engine
from fra_ingest.pages import page_count
from fra_ingest.results import ConvertResult, LocateResult, StructureResult
from fra_ingest.stage import load_or_locate
from fra_ingest.structure import current_convert, structure_pdf


@dataclass(frozen=True)
class Pipeline:
    """The stages a job runs, as callables so tests can stand in for them. ``page_count`` reads
    no page, so the page limit is checked before locate reads (and may OCR) every one."""

    page_count: Callable[[Path], int]
    locate: Callable[[Path], LocateResult]
    convert: Callable[[Path, LocateResult], ConvertResult]
    structure: Callable[[Path], StructureResult]
    metrics: Callable[[Sequence[Statement]], list[MetricValue]]


def real_pipeline(
    config: IngestConfig, policy: Policy, *, worker_lock_fd: int | None = None
) -> Pipeline:
    """Today's stages, as the dry run calls them: convert in a child process per document."""
    ocr = make_engine(config)
    convert = partial(convert_in_child, worker_lock_fd=worker_lock_fd)
    return Pipeline(
        page_count=page_count,
        locate=lambda pdf: load_or_locate(pdf, config, ocr),
        convert=lambda pdf, located: current_convert(pdf, config, ocr, located, convert),
        structure=lambda pdf: structure_pdf(pdf, config, ocr, convert=convert),
        metrics=lambda statements: compute(primary_statements(statements), policy),
    )


class PageLimitExceeded(ValueError):
    pass


def analysis_of(
    job: Job, located: LocateResult, result: StructureResult, metrics: list[MetricValue]
) -> Analysis:
    industry = result.industry
    return Analysis(
        job_id=job.id,
        upload_sha256=job.upload_sha256,
        page_count=located.document.page_count,
        language=located.document.language,
        statements=result.statements,
        reviews=result.reviews,
        flags=list(dict.fromkeys([*located.flags, *result.flags])),
        decline=(
            Decline(code=industry.code, reason=industry.reason)
            if industry is not None and industry.outcome == "declined"
            else None
        ),
        hold=industry.reason
        if industry is not None and industry.outcome == "needs_review"
        else None,
        metrics=metrics,
    )


def run_job(store: JobStore, job: Job, pipeline: Pipeline, max_pages: int) -> Job:
    """Run a claimed job to its end. Returns the job as it finished: completed or
    needs_review with its analysis saved, or failed with the reason."""
    pdf = store.upload_path(job.upload_sha256)
    try:
        store.start_stage(job.id, JobStage.LOCATE)
        pages = pipeline.page_count(pdf)
        if pages > max_pages:
            raise PageLimitExceeded(f"the PDF has {pages} pages; the limit is {max_pages}")
        located = pipeline.locate(pdf)
        document = located.document
        store.finish_stage(
            job.id,
            JobStage.LOCATE,
            f"{document.page_count} pages, {len(document.scanned_pages)} without a text layer, "
            f"{len(located.ranges)} statement ranges",
        )
        decision = industry_decision(located.industry)
        declined = (
            decision.code if decision is not None and decision.outcome == "declined" else None
        )
        store.start_stage(job.id, JobStage.CONVERT)
        if declined is not None:
            store.finish_stage(job.id, JobStage.CONVERT, f"skipped: declined ({declined})")
        else:
            converted = pipeline.convert(pdf, located)
            store.finish_stage(job.id, JobStage.CONVERT, f"{len(converted.ranges)} page ranges")
        store.start_stage(job.id, JobStage.STRUCTURE)
        result = pipeline.structure(pdf)
        store.finish_stage(job.id, JobStage.STRUCTURE, f"{len(result.statements)} statements")
        store.start_stage(job.id, JobStage.METRICS)
        if declined is not None:
            metrics: list[MetricValue] = []
            store.finish_stage(job.id, JobStage.METRICS, "skipped: declined")
        else:
            metrics = pipeline.metrics(result.statements)
            computed = sum(1 for m in metrics if m.value is not None)
            store.finish_stage(
                job.id, JobStage.METRICS, f"{computed} of {len(metrics)} values computed"
            )
        return store.finish(analysis_of(job, located, result, metrics))
    except Exception as exc:  # the job's own failure: recorded on the job, never retried
        return store.fail(job.id, f"{type(exc).__name__}: {exc}")
