"""One job through the pipeline, with each stage recorded on the job's timeline.

Locate, convert, structure and metrics are timed as separate stages: convert is run on its own
first (``current_convert``), and ``structure_pdf`` is given that conversion and reuses the
located pages, so neither is repeated, even when a range converted only in part. A bank or an
insurer is declined after locate, and its convert and metrics stages are recorded as skipped.
Any exception ends the job as failed with its message; nothing is retried.
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
from fra_ingest.structure import current_convert, range_conversion_flags, structure_pdf
from fra_model import SummaryBuilt, SummarySuppressed, build_summary, validate_summary


@dataclass(frozen=True)
class Pipeline:
    """The stages a job runs, as callables so tests can stand in for them. ``page_count`` reads
    no page, so the page limit is checked before locate reads (and may OCR) every one."""

    page_count: Callable[[Path], int]
    locate: Callable[[Path], LocateResult]
    convert: Callable[[Path, LocateResult], ConvertResult]
    structure: Callable[[Path, ConvertResult | None], StructureResult]
    metrics: Callable[[Sequence[Statement]], list[MetricValue]]
    summarize: Callable[[Analysis], SummaryBuilt | SummarySuppressed] = build_summary


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
        structure=lambda pdf, converted: structure_pdf(
            pdf, config, ocr, convert=convert, conversion=converted
        ),
        metrics=lambda statements: compute(primary_statements(statements), policy),
    )


class PageLimitExceeded(ValueError):
    pass


def analysis_of(
    job: Job,
    located: LocateResult,
    converted: ConvertResult | None,
    result: StructureResult,
    metrics: list[MetricValue],
    decline: Decline | None = None,
) -> Analysis:
    industry = result.industry
    conversion_flags: list[str] = []
    if converted is not None:
        conversion_flags.extend(converted.flags)
        for page_range in converted.ranges:
            conversion_flags.extend(range_conversion_flags([page_range]))
            conversion_flags.extend(page_range.flags)
    return Analysis(
        job_id=job.id,
        upload_sha256=job.upload_sha256,
        page_count=located.document.page_count,
        language=located.document.language,
        statements=result.statements,
        reviews=result.reviews,
        flags=list(dict.fromkeys([*located.flags, *conversion_flags, *result.flags])),
        decline=decline
        or (
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
        decline = None
        if document.language == "ar":
            decline = Decline(
                code="unsupported_language",
                reason="This report is mainly in Arabic. Only English financial reports are "
                "analyzed, so no statements, metrics or summary were produced.",
            )
        else:
            decision = industry_decision(located.industry)
            if decision is not None and decision.outcome == "declined":
                decline = Decline(code=decision.code, reason=decision.reason)
        declined = decline.code if decline is not None else None
        store.start_stage(job.id, JobStage.CONVERT)
        converted: ConvertResult | None = None
        if declined is not None:
            store.finish_stage(job.id, JobStage.CONVERT, f"skipped: declined ({declined})")
        else:
            converted = pipeline.convert(pdf, located)
            store.finish_stage(job.id, JobStage.CONVERT, f"{len(converted.ranges)} page ranges")
        store.start_stage(job.id, JobStage.STRUCTURE)
        if declined == "unsupported_language":
            result = StructureResult(
                version="declined", sha256=job.upload_sha256, convert_version="", settings_hash=""
            )
            store.finish_stage(job.id, JobStage.STRUCTURE, "skipped: declined")
        else:
            result = pipeline.structure(pdf, converted)
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
        analysis = analysis_of(job, located, converted, result, metrics, decline)
        store.start_stage(job.id, JobStage.NARRATE)
        summary = pipeline.summarize(analysis)
        if isinstance(summary, SummaryBuilt):
            analysis.narrative = validate_summary(analysis, summary.narrative)
            store.finish_stage(job.id, JobStage.NARRATE, "checked direct-template summary")
        else:
            analysis.narrative_suppression = list(summary.reasons)
            store.finish_stage(job.id, JobStage.NARRATE, "skipped: " + "; ".join(summary.reasons))
        return store.finish(analysis)
    except Exception as exc:  # the job's own failure: recorded on the job, never retried
        return store.fail(job.id, f"{type(exc).__name__}: {exc}")
