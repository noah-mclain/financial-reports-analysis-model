"""Synthetic API-to-worker integration through persisted, rendered results."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from api_support import settings_at
from fastapi.testclient import TestClient

from fra_api.app import create_app
from fra_api.views import source_url
from fra_core.jobs import JobStore
from fra_core.profile import Profile
from fra_core.schemas import (
    BBox,
    Cell,
    Document,
    LineItem,
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
from fra_ingest.results import (
    ConvertResult,
    IndustrySignal,
    LocateResult,
    RangeConversion,
    StatementRange,
    StructureResult,
)
from fra_worker.pipeline import Pipeline, run_job


def summary_panel(page: str) -> str:
    """The written-summary panel alone, so its direction and language are not read off another."""
    match = re.search(r'<div class="panel" dir=.*?</div>', page, re.S)
    assert match is not None
    return match.group(0)


PDF = b"%PDF-1.7\nsynthetic integration fixture\n"
SOURCE_A = Provenance(
    page_no=1, bbox=BBox(left=10, top=20, right=30, bottom=40), table_ref="#/tables/0", row=1, col=1
)
SOURCE_B = Provenance(
    page_no=1, bbox=BBox(left=50, top=60, right=70, bottom=80), table_ref="#/tables/0", row=2, col=1
)
PERIOD = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)


def pipeline_for(
    sha256: str,
    language: str,
    review_status: Literal["passed", "needs_review"] = "passed",
) -> Pipeline:
    cell = Cell(
        period_key=PERIOD.key,
        reported=Decimal("3"),
        raw_text="3",
        provenance=SOURCE_A,
    )
    statement = Statement(
        id="assets",
        document_sha256=sha256,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        language=language,
        periods=[PERIOD],
        source_pages=[1],
        line_items=[
            LineItem(
                id="current-assets",
                raw_label="Current assets",
                canonical_id="total_current_assets",
                cells=[cell],
            ),
            LineItem(
                id="current-liabilities",
                raw_label="Current liabilities",
                canonical_id="total_current_liabilities",
                cells=[
                    Cell(
                        period_key=PERIOD.key,
                        reported=Decimal("2"),
                        raw_text="2",
                        provenance=SOURCE_B,
                    )
                ],
            ),
        ],
    )
    review = StatementReview(
        statement_id="assets",
        status=review_status,
        numeric_cells=2,
        checked_cells=2,
        flagged_cells=0,
    )
    metric_input = MetricInput(
        statement_id="assets",
        line_item_id="current-assets",
        canonical_id="total_current_assets",
        period_key=PERIOD.key,
        reported=Decimal("3"),
        scale=1000,
        currency="SAR",
        provenance=SOURCE_A,
    )
    computed = MetricValue(
        metric_id="current_ratio",
        period_key=PERIOD.key,
        value=1.5,
        unit=MetricUnit.TIMES,
        inputs={
            "numerator": [metric_input],
            "denominator": [
                MetricInput(
                    statement_id="assets",
                    line_item_id="current-liabilities",
                    canonical_id="total_current_liabilities",
                    period_key=PERIOD.key,
                    reported=Decimal("2"),
                    scale=1000,
                    currency="SAR",
                    provenance=SOURCE_B,
                )
            ],
        },
        formula="total_current_assets / total_current_liabilities",
        formula_version="1",
        flags=[],
    )

    def locate(pdf: Path) -> LocateResult:
        assert pdf.read_bytes() == PDF
        return LocateResult(
            version="synthetic",
            document=Document(
                sha256=sha256,
                filename="report.pdf",
                page_count=1,
                language=language,
            ),
            pages=[],
            ranges=[
                StatementRange(
                    type=StatementType.BALANCE,
                    first_page=1,
                    last_page=1,
                    score=1,
                    rank=1,
                )
            ],
            convert_ranges=[(1, 1)],
            industry=IndustrySignal(kind="corporate"),
        )

    def convert(pdf: Path, located: LocateResult) -> ConvertResult:
        assert pdf.read_bytes() == PDF and located.document.sha256 == sha256
        return ConvertResult(
            version="synthetic",
            sha256=sha256,
            locate_version="synthetic",
            docling_version="stubbed",
            device="cpu",
            settings_hash="synthetic",
            ranges=[
                RangeConversion(
                    first_page=1,
                    last_page=1,
                    ocr="pdf_aware",
                    ocr_language=None,
                    status="ok",
                )
            ],
            peak_footprint_gb=0.0,
        )

    def structure(pdf: Path, converted: ConvertResult | None) -> StructureResult:
        assert pdf.read_bytes() == PDF and converted is not None
        return StructureResult(
            version="synthetic",
            sha256=sha256,
            convert_version="synthetic",
            settings_hash="synthetic",
            statements=[statement],
            reviews=[review],
        )

    def metrics(statements: Sequence[Statement]) -> list[MetricValue]:
        assert statements == [statement]
        return [computed]

    return Pipeline(
        page_count=lambda pdf: 1,
        locate=locate,
        convert=convert,
        structure=structure,
        metrics=metrics,
    )


@pytest.mark.parametrize("language", ["en", "ar"])
def test_upload_runs_through_worker_and_renders_grounded_result(
    tmp_path: Path, language: str
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        uploaded = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "report.pdf"},
        )
        assert uploaded.status_code == 201
        sha256 = uploaded.json()["sha256"]
        queued = client.post(f"/documents/{sha256}/analyses")
        assert queued.status_code == 202
        job_id = queued.json()["id"]
        assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"

        store = JobStore(settings.root)
        job = store.claim()
        assert job is not None and job.id == job_id
        finished = run_job(store, job, pipeline_for(sha256, language), settings.max_pages)
        assert finished.status.value == "completed"
        persisted = store.load_analysis(job_id)
        assert persisted is not None and persisted.narrative is not None
        assert persisted.narrative.language == language
        assert persisted.narrative.origin == "direct_template"

        response = client.get(f"/analyses/{job_id}/view")
        assert response.status_code == 200
        page = response.text

    panel = summary_panel(page)
    assert panel.startswith(f'<div class="panel" dir="{"rtl" if language == "ar" else "ltr"}"')
    assert f'lang="{language}"' in panel.split(">", 1)[0]
    heading, sentence = (
        ("liquidity", "Current ratio") if language == "en" else ("السيولة", "نسبة التداول")
    )
    assert f"<h3>{heading}</h3>" in panel and sentence in panel
    assert 'href="#metric-current_ratio-2025-12-31"' in panel
    for ids, source in (
        (("total_current_assets", "current-assets"), SOURCE_A),
        (("total_current_liabilities", "current-liabilities"), SOURCE_B),
    ):
        url = source_url(sha256, source)
        assert f'<a href="{url}" title="2025-12-31">{ids[0]}</a>' in page
        assert page.count(f'href="{url}"') == 2  # the statement cell and the metric input
    assert "total_current_assets" in page and "current_ratio" in page
    assert "total_current_assets / total_current_liabilities" in page
    assert "1.5" in page and "total_current_liabilities" in page
    origin = "Direct template" if language == "en" else "قالب مباشر"
    assert origin in page
    assert "Written by the model" not in page


def test_held_statement_stays_visible_without_a_confident_summary(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        uploaded = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "held.pdf"},
        )
        sha256 = uploaded.json()["sha256"]
        job_id = client.post(f"/documents/{sha256}/analyses").json()["id"]
        store = JobStore(settings.root)
        job = store.claim()
        assert job is not None
        finished = run_job(
            store,
            job,
            pipeline_for(sha256, "en", review_status="needs_review"),
            settings.max_pages,
        )
        assert finished.status.value == "needs_review"
        saved = store.load_analysis(job_id)
        assert saved is not None and saved.narrative is None and saved.narrative_suppression
        page = client.get(f"/analyses/{job_id}/view").text

    assert "Some figures did not pass their own checks." in page
    assert 'class="badge needs_review">held for review</span>' in page
    assert "Summary withheld: the analysis is held for review." in page
    assert "no eligible latest-period facts" not in page
    for label, printed in (("Current assets", "3"), ("Current liabilities", "2")):
        assert label in page and f'title="printed: {printed}"' in page
    assert "Direct template" not in page and "Written by the model" not in page


def test_worker_failure_is_recorded_on_the_job_and_its_event_stream(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        uploaded = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "failure.pdf"},
        )
        sha256 = uploaded.json()["sha256"]
        job_id = client.post(f"/documents/{sha256}/analyses").json()["id"]
        store = JobStore(settings.root)
        job = store.claim()
        assert job is not None

        def fail_convert(pdf: Path, located: LocateResult) -> ConvertResult:
            raise RuntimeError("synthetic conversion failure")

        failed_pipeline = replace(pipeline_for(sha256, "en"), convert=fail_convert)
        failed = run_job(store, job, failed_pipeline, settings.max_pages)
        assert failed.status.value == "failed"
        job_page = client.get(f"/jobs/{job_id}/view").text
        events = client.get(f"/jobs/{job_id}/events").text
        api_job = client.get(f"/jobs/{job_id}").json()

    assert api_job["status"] == "failed"
    assert api_job["error"] == "RuntimeError: synthetic conversion failure"
    assert 'class="badge failed">failed</span>' in job_page
    assert '"stage":"convert"' in events and '"kind":"failed"' in events
    assert "synthetic conversion failure" in events
