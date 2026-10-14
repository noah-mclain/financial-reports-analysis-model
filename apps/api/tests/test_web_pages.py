"""The pages: what the results show and link to, the source viewer's box, and the display
formatting, which rounds and never computes."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from api_support import settings_at
from fastapi.testclient import TestClient

from fra_api.app import create_app
from fra_api.views import (
    amount,
    analysis_view,
    metric_anchor,
    metric_text,
    sentence_parts,
    source_url,
    statement_view,
)
from fra_core.jobs import JobSettings, JobStore
from fra_core.profile import Profile
from fra_core.schemas import (
    Analysis,
    BBox,
    Cell,
    Claim,
    Decline,
    LineItem,
    MetricInput,
    MetricUnit,
    MetricValue,
    Narrative,
    NarrativeSection,
    Period,
    PeriodKind,
    Provenance,
    Sentence,
    Statement,
    StatementReview,
    StatementType,
)
from fra_core.schemas.narrative import SectionHeading

PERIOD = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
PAGE_W, PAGE_H = 600.0, 800.0


def one_page_pdf() -> bytes:
    document = pdfium.PdfDocument.new()
    document.new_page(PAGE_W, PAGE_H)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


PDF = one_page_pdf()


def provenance(page: int = 1) -> Provenance:
    return Provenance(
        page_no=page,
        bbox=BBox(left=60, top=80, right=180, bottom=100),
        table_ref="#/tables/0",
        row=1,
        col=1,
    )


def statement(sha256: str, language: str = "en", label: str = "Total assets") -> Statement:
    return Statement(
        id="s1",
        document_sha256=sha256,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        language=language,
        periods=[PERIOD],
        source_pages=[1],
        line_items=[
            LineItem(
                id="r1",
                raw_label=label,
                canonical_id="total_assets",
                is_subtotal=True,
                cells=[
                    Cell(
                        period_key=PERIOD.key,
                        reported=Decimal("-1234567"),
                        raw_text="(1,234,567)",
                        provenance=provenance(),
                        flags=["digit_suspect"],
                    )
                ],
            )
        ],
    )


def review(status: str = "passed", reasons: list[str] | None = None) -> StatementReview:
    return StatementReview(
        statement_id="s1",
        status=status,  # type: ignore[arg-type]
        reasons=reasons or [],
        numeric_cells=1,
        checked_cells=1,
        flagged_cells=0,
    )


def metric(unit: MetricUnit, value: float | None, sha256: str = "a" * 64) -> MetricValue:
    cell = MetricInput(
        statement_id="s1",
        line_item_id="r1",
        canonical_id="total_assets",
        period_key=PERIOD.key,
        reported=Decimal("1234567"),
        scale=1000,
        currency="SAR",
        provenance=provenance(),
    )
    return MetricValue(
        metric_id="current_ratio",
        period_key=PERIOD.key,
        value=value,
        unit=unit,
        formula="total_current_assets / total_current_liabilities",
        inputs={"numerator": [cell]} if value is not None else {},
        formula_version="1",
        flags=[] if value is not None else ["missing_input:total_current_liabilities"],
    )


# ---- display formatting -----------------------------------------------------------------------


def test_amounts_are_grouped_and_negatives_shown_in_parentheses() -> None:
    assert amount(Decimal("1234567")) == "1,234,567"
    assert amount(Decimal("-1234.5")) == "(1,234.5)"
    assert amount(Decimal("0")) == "0"


@pytest.mark.parametrize(
    ("unit", "value", "text"),
    [
        (MetricUnit.RATIO, 0.1234, "12.3%"),
        (MetricUnit.TIMES, 1.456, "1.46×"),
        (MetricUnit.DAYS, 45.6, "46 days"),
        (MetricUnit.CURRENCY, 1234567.0, "1,234,567 SAR"),
        (MetricUnit.RATIO, None, None),
    ],
)
def test_a_metric_is_rounded_for_display_by_its_unit(
    unit: MetricUnit, value: float | None, text: str | None
) -> None:
    assert metric_text(metric(unit, value)) == text


def test_a_cell_links_to_its_page_and_box() -> None:
    assert source_url("a" * 64, provenance(3)) == (
        f"/documents/{'a' * 64}/pages/3?box=60.0%2C80.0%2C180.0%2C100.0"
    )


def test_an_arabic_statement_is_right_to_left_and_keeps_its_printed_labels() -> None:
    view = statement_view(statement("a" * 64, "ar", "إجمالي الموجودات"), review())
    assert view.rtl and view.units == "SAR thousands"
    row = view.rows[0]
    assert row.label == "إجمالي الموجودات" and row.subtotal
    (cell,) = row.cells[0]
    assert cell.text == "(1,234,567)" and cell.printed == "(1,234,567)"
    assert cell.flags == ("digit_suspect",)


def test_periods_are_shown_newest_first_whatever_order_they_were_printed_in() -> None:
    older = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
    printed = statement("a" * 64).model_copy(update={"periods": [older, PERIOD]})
    view = statement_view(printed, review())
    assert view.periods == ("2025-12-31", "2024-12-31")
    cells = view.rows[0].cells
    assert [c.text for c in cells[0]] == ["(1,234,567)"]  # the 2025 cell
    assert cells[1] == ()  # nothing was read for 2024


def test_two_cells_read_for_one_period_are_both_shown_and_flagged() -> None:
    printed = statement("a" * 64)
    item = printed.line_items[0]
    second = item.cells[0].model_copy(
        update={"reported": Decimal("7"), "raw_text": "7", "flags": []}
    )
    item = item.model_copy(update={"cells": [*item.cells, second]})
    view = statement_view(printed.model_copy(update={"line_items": [item]}), review())
    found = view.rows[0].cells[0]
    assert [c.text for c in found] == ["(1,234,567)", "7"]
    assert [c.flags for c in found] == [("digit_suspect", "duplicate_cell"), ("duplicate_cell",)]


def test_the_same_figure_claimed_twice_links_each_occurrence_to_its_own_metric() -> None:
    text = "The current ratio is 1.50 and the quick ratio is 1.50."
    claims = [
        Claim(metric_id="current_ratio", period_key=PERIOD.key, text_span="1.50"),
        Claim(metric_id="quick_ratio", period_key=PERIOD.key, text_span="1.50"),
    ]
    anchors = frozenset(metric_anchor(c.metric_id, c.period_key) for c in claims)
    parts = sentence_parts(Sentence(text=text, claims=claims), anchors)
    links = [(p.text, p.href) for p in parts if p.href]
    assert links == [
        ("1.50", f"#{metric_anchor('current_ratio', PERIOD.key)}"),
        ("1.50", f"#{metric_anchor('quick_ratio', PERIOD.key)}"),
    ]
    assert "".join(p.text for p in parts) == text
    third = [*claims, Claim(metric_id="current_ratio", period_key=PERIOD.key, text_span="1.50")]
    with pytest.raises(ValueError, match="is not in"):
        sentence_parts(Sentence(text=text, claims=third), anchors)


def test_the_analysis_lists_computed_metrics_first_and_refuses_an_unreviewed_statement() -> None:
    sha = "a" * 64
    analysis = Analysis(
        job_id="b" * 32,
        upload_sha256=sha,
        page_count=1,
        language="en",
        statements=[statement(sha)],
        reviews=[review()],
        metrics=[metric(MetricUnit.RATIO, None), metric(MetricUnit.TIMES, 2.0)],
    )
    view = analysis_view(analysis)
    assert [m.value for m in view.metrics] == ["2.00×", None] and view.computed == 1
    with pytest.raises(ValueError, match="without a review"):
        analysis_view(analysis.model_copy(update={"reviews": []}))


# ---- pages ------------------------------------------------------------------------------------


def finished_job(settings: JobSettings, client: TestClient, **analysis: object) -> str:
    sha256 = client.post(
        "/documents",
        content=PDF,
        headers={"Content-Type": "application/pdf", "X-Filename": "report.pdf"},
    ).json()["sha256"]
    job_id: str = client.post(f"/documents/{sha256}/analyses").json()["id"]
    store = JobStore(settings.root)
    store.claim()
    store.finish(
        Analysis.model_validate(
            {
                "job_id": job_id,
                "upload_sha256": sha256,
                "page_count": 1,
                "language": "en",
                "statements": [statement(sha256)],
                "reviews": [review()],
                "metrics": [metric(MetricUnit.TIMES, 1.5, sha256)],
                **analysis,
            }
        )
    )
    return job_id


def test_the_upload_page_states_the_limits_and_the_profile(tmp_path: Path) -> None:
    with TestClient(create_app(Profile.DOCKER, settings_at(tmp_path))) as client:
        page = client.get("/").text
    assert "Up to 0 MB and 400 pages" in page  # 10,000 bytes in the test settings
    assert "docker profile" in page and 'id="upload"' in page


def test_the_results_show_statements_metrics_and_say_there_is_no_summary(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        page = client.get(f"/analyses/{job_id}/view").text
    assert "Statement of financial position" in page and "SAR thousands" in page
    assert "(1,234,567)" in page and "/pages/1?box=60.0%2C80.0%2C180.0%2C100.0" in page
    assert "flags: digit_suspect</div>" in page  # named under the figure, not only on hover
    assert "1.50×" in page and "total_current_assets / total_current_liabilities" in page
    assert "No written summary yet" in page


def test_a_declined_document_shows_the_reason_and_no_metrics(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    decline = Decline(code="bank", reason="declined:bank: total deposits on page 3")
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings, client, statements=[], reviews=[], metrics=[], decline=decline
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert "Declined (bank)" in page and "total deposits on page 3" in page
    assert "<h2>Metrics</h2>" not in page


def test_a_narrative_is_shown_with_how_it_was_checked(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    narrative = Narrative(
        language="en",
        model_id="m",
        sections=[
            NarrativeSection(
                heading=SectionHeading.LIQUIDITY,
                sentences=[Sentence(text="The current ratio is 1.50.")],
            )
        ],
    )
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, narrative=narrative)
        page = client.get(f"/analyses/{job_id}/view").text
    assert "The current ratio is 1.50." in page and "checked against the computed metrics" in page
    assert "No written summary yet" not in page


def test_a_printed_label_is_escaped_not_run(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        sha_free = finished_job(settings, client)
        store = JobStore(settings.root)
        analysis = store.load_analysis(sha_free)
        assert analysis is not None
        hostile = statement(analysis.upload_sha256, label="<script>alert(1)</script>")
        job_id = client.post(f"/documents/{analysis.upload_sha256}/analyses").json()["id"]
        store.claim()
        store.finish(analysis.model_copy(update={"job_id": job_id, "statements": [hostile]}))
        page = client.get(f"/analyses/{job_id}/view").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_the_results_of_an_unfinished_job_redirect_to_its_timeline(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        sha256 = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
        ).json()["sha256"]
        job_id = client.post(f"/documents/{sha256}/analyses").json()["id"]
        response = client.get(f"/analyses/{job_id}/view", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/jobs/{job_id}/view"
        timeline = client.get(f"/jobs/{job_id}/view").text
        assert all(f'data-stage="{s}"' in timeline for s in ("locate", "convert", "structure"))
        assert client.get(f"/jobs/{'0' * 32}/view").status_code == 404


def test_a_page_renders_as_a_png_and_the_viewer_places_the_box_by_page_share(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        sha256 = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
        ).json()["sha256"]
        image = client.get(f"/documents/{sha256}/pages/1.png")
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        assert (settings.root / "pages" / sha256 / "1.png").is_file()  # cached
        viewer = client.get(f"/documents/{sha256}/pages/1", params={"box": "60,80,180,100"})
        # 60/600, 80/800, 120/600, 20/800 of the page
        assert "left:10.000%;top:10.000%;width:20.000%;height:2.500%" in viewer.text
        assert client.get(f"/documents/{sha256}/pages/2").status_code == 404
        assert client.get(f"/documents/{sha256}/pages/2.png").status_code == 404
        assert client.get(f"/documents/{sha256}/pages/1", params={"box": "a,b"}).status_code == 400
        assert (
            client.get(f"/documents/{sha256}/pages/1", params={"box": "9,9,1,1"}).status_code == 400
        )
        assert client.get(f"/documents/{'0' * 64}/pages/1").status_code == 404


# ---- review fixes: concurrent renders, clipped boxes, claim links, the upload label -------------


def test_two_renders_of_one_page_at_once_both_succeed(tmp_path: Path) -> None:
    import threading

    from fra_api.pages import render_page

    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(PDF)
    target = tmp_path / "pages" / "1.png"
    errors: list[BaseException] = []

    def render() -> None:
        try:
            render_page(pdf, 1, target)
        except BaseException as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=render) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert target.read_bytes().startswith(b"\x89PNG")
    assert sorted(p.name for p in target.parent.iterdir()) == ["1.png"]  # no temporary left


def test_a_box_past_the_page_edge_is_cut_to_the_page_and_one_off_it_is_not_drawn(
    tmp_path: Path,
) -> None:
    from fra_api.pages import place_box

    inside = place_box((60, 80, 180, 100), PAGE_W, PAGE_H)
    assert inside is not None and inside["clipped"] is False
    past = place_box((540, 780, 700, 820), PAGE_W, PAGE_H)
    assert past is not None and past["clipped"] is True
    assert float(past["left"]) + float(past["width"]) == pytest.approx(100.0)
    assert float(past["top"]) + float(past["height"]) == pytest.approx(100.0)
    assert place_box((700, 900, 800, 950), PAGE_W, PAGE_H) is None
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        sha256 = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
        ).json()["sha256"]
        cut = client.get(f"/documents/{sha256}/pages/1", params={"box": "540,780,700,820"})
        assert "cut to the page" in cut.text
        off = client.get(f"/documents/{sha256}/pages/1", params={"box": "700,900,800,950"})
        assert "outside this page" in off.text and 'id="region"' not in off.text


def claimed(text: str, span: str, metric_id: str = "current_ratio") -> Narrative:
    return Narrative(
        language="en",
        model_id="m",
        sections=[
            NarrativeSection(
                heading=SectionHeading.LIQUIDITY,
                sentences=[
                    Sentence(
                        text=text,
                        claims=[Claim(metric_id=metric_id, period_key=PERIOD.key, text_span=span)],
                    )
                ],
            )
        ],
    )


def test_a_claimed_figure_in_the_summary_links_to_its_metric_row(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    narrative = claimed("The current ratio is 1.50 at year end.", "1.50")
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, narrative=narrative)
        page = client.get(f"/analyses/{job_id}/view").text
    assert 'href="#metric-current_ratio-2025-12-31"' in page and ">1.50</a>" in page
    assert '<tr id="metric-current_ratio-2025-12-31">' in page
    assert "The current ratio is " in page and " at year end." in page


def test_a_claim_without_its_span_or_its_metric_is_an_error() -> None:
    from fra_api.views import narrative_view

    anchors = frozenset({"metric-current_ratio-2025-12-31"})
    with pytest.raises(ValueError, match="is not in"):
        narrative_view(claimed("The ratio rose.", "1.50"), anchors)
    with pytest.raises(ValueError, match="names no computed metric"):
        narrative_view(claimed("ROE is 12%.", "12%", metric_id="roe"), anchors)


def test_the_upload_field_has_a_label(tmp_path: Path) -> None:
    with TestClient(create_app(Profile.NATIVE, settings_at(tmp_path))) as client:
        page = client.get("/").text
    assert '<label for="file">' in page and 'id="file"' in page
