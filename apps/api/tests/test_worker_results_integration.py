"""Synthetic API-to-worker integration through persisted, rendered results."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from html import escape
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
def test_upload_runs_through_worker_and_renders_language_scoped_result(
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
        if language == "ar":
            assert finished.status.value == "needs_review"
            persisted = store.load_analysis(job_id)
            assert persisted is not None and persisted.decline is not None
            assert persisted.decline.code == "unsupported_language"
            assert not persisted.statements and not persisted.metrics
            assert persisted.narrative is None
            response = client.get(f"/analyses/{job_id}/view")
            assert response.status_code == 200
            assert persisted.decline.reason in response.text
            assert '<h2 id="verdict-title">Declined</h2>' in response.text
            decline_summary = (
                "لم يُنتج ملخص لأن المستند رُفض."
                if language == "ar"
                else "No summary was produced because this document was declined."
            )
            assert decline_summary in response.text
            assert 'id="metrics"' not in response.text
            return
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
        url = source_url(sha256, source, job_id)
        assert f'href="{escape(url, quote=True)}"' in page
        assert (
            f'<code dir="ltr">{ids[0]}</code> · <code dir="ltr">2025-12-31</code></details>'
        ) in page
        assert page.count(f'href="{escape(url, quote=True)}"') == 2  # statement and metric input
    assert "Total current assets / Total current liabilities" in page
    assert "total_current_assets / total_current_liabilities" in page
    assert "Current ratio" in page and "1.5" in page
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

    assert "Figures are not verified." in page
    assert 'class="badge needs_review">Held for review</span>' in page
    assert "Summary withheld because the analysis is held for review." in page
    assert "no eligible latest-period facts" not in page
    for label, printed in (("Current assets", "3"), ("Current liabilities", "2")):
        assert label in page and f'title="Printed as: {printed}"' in page
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
    assert 'class="badge failed">Failed</span>' in job_page
    assert '"stage":"convert"' in events and '"kind":"failed"' in events
    assert "synthetic conversion failure" in events


def partial_pipeline(sha256: str, *, scanned: bool = True) -> Pipeline:
    """A checked income statement beside an unavailable balance sheet."""
    from fra_analytics import Policy, compute
    from fra_core.schemas import PageMode, PageProfile

    original = pipeline_for(sha256, "en")

    def locate(pdf: Path) -> LocateResult:
        result = original.locate(pdf)
        result.document.pages = [
            PageProfile(
                page_no=1,
                mode=PageMode.IMAGE if scanned else PageMode.TEXT,
                char_count=0 if scanned else 100,
                width_pt=600,
                height_pt=800,
            )
        ]
        return result

    def structure(pdf: Path, converted: ConvertResult | None) -> StructureResult:
        result = original.structure(pdf, converted)
        statement = result.statements[0]
        statement.type = StatementType.INCOME
        statement.periods = [PERIOD.model_copy(update={"kind": PeriodKind.DURATION, "months": 12})]
        for row, canonical in zip(statement.line_items, ["net_income", "revenue"], strict=True):
            row.canonical_id = canonical
            row.raw_label = canonical
            row.cells[0].arithmetic_checks = ["income:subtotal:current"]
        result.flags = ["statement_not_extracted:balance"]
        return result

    policy = Policy(
        negative_margin_denominator="compute_and_flag",
        include_lease_liabilities=True,
        day_count_basis=365,
    )
    return replace(
        original, locate=locate, structure=structure, metrics=lambda s: compute(s, policy)
    )


@pytest.mark.parametrize("scanned", [True, False])
def test_partial_summary_persists_marker_scan_profile_and_held_verdict(
    tmp_path: Path, scanned: bool
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        uploaded = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "partial.pdf"},
        )
        sha = uploaded.json()["sha256"]
        job_id = client.post(f"/documents/{sha}/analyses").json()["id"]
        store = JobStore(settings.root)
        job = store.claim()
        assert job is not None
        finished = run_job(store, job, partial_pipeline(sha, scanned=scanned), settings.max_pages)
        assert finished.status.value == "needs_review"
        source = store.load_analysis(job_id)
        assert source is not None and source.narrative is not None
        assert source.narrative_partial
        assert source.scanned_pages == ([1] if scanned else [])
        assert source.narrative.cited_metric_ids == {"net_margin"}
        response = client.get(f"/analyses/{job_id}")
        assert response.json()["narrative_partial"] is True
        assert response.json()["scanned_pages"] == source.scanned_pages
        page = client.get(f"/analyses/{job_id}/view").text
        assert "Partial summary: some statements could not be confirmed" in page
        assert '<h2 id="verdict-title">Held for review</h2>' in page
        assert "Net margin:" in page and "150.0%" in page
        assert ("Figures were read by OCR from a scanned page." in page) is scanned
        assert "Current ratio was not included because its source statement is unavailable." in page
        assert store.events(job_id)[-2].detail == "checked partial direct-template summary"
        # Persisted marker must agree with the checked grammar at rendering time.
        source.narrative_partial = False
        from fra_api.views import analysis_view

        assert analysis_view(source).narrative is None


def test_missing_page_profile_cannot_authorize_a_caveat_free_partial(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    store = JobStore(settings.root)
    with store.upload_writer(settings) as writer:
        writer.write(PDF)
        upload = writer.finish("unknown-pages.pdf")
    queued = store.enqueue(upload.sha256)
    job = store.claim()
    assert job is not None
    pipeline = partial_pipeline(upload.sha256)
    pipeline = replace(pipeline, locate=pipeline_for(upload.sha256, "en").locate)
    finished = run_job(store, job, pipeline, settings.max_pages)
    saved = store.load_analysis(queued.id)
    assert finished.status.value == "needs_review"
    assert saved is not None and saved.scanned_pages is None
    assert saved.narrative is None
    assert saved.narrative_suppression == ["scanned-page profile unavailable or invalid"]
