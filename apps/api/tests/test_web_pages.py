"""The pages: what the results show and link to, the source viewer's box, and the display
formatting, which rounds and never computes."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from api_support import settings_at
from fastapi.testclient import TestClient
from PIL import Image

from fra_api.app import create_app
from fra_api.pages import MAX_RENDER_SIDE, render_page
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
    Caveat,
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


def one_page_pdf(width: float = PAGE_W, height: float = PAGE_H) -> bytes:
    document = pdfium.PdfDocument.new()
    document.new_page(width, height)
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


@pytest.mark.parametrize("precision", [3, 28])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123456789012345678901234567890.123", "123,456,789,012,345,678,901,234,567,890.123"),
        ("-123456789012345678901234567890.123", "(123,456,789,012,345,678,901,234,567,890.123)"),
        ("-1234.5000", "(1,234.5000)"),
        ("1.23400E+8", "123,400,000"),
        ("1.23400E-8", "0.0000000123400"),
        ("0.0000", "0.0000"),
        ("-0.00", "0.00"),
    ],
)
def test_printed_amounts_keep_every_digit_without_using_decimal_precision(
    precision: int, value: str, expected: str
) -> None:
    with localcontext() as context:
        context.prec = precision
        assert amount(Decimal(value)) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.25, "1.25 SAR per share"),
        (-1.257, "(1.257) SAR per share"),
        (0.0, "0.0 SAR per share"),
        (0.0000125, "0.0000125 SAR per share"),
    ],
)
def test_per_share_values_keep_fractional_precision_and_name_the_unit(
    value: float, expected: str
) -> None:
    assert metric_text(metric(MetricUnit.PER_SHARE, value)) == expected


@pytest.mark.parametrize("value", [1e308, -1e308, 0.0, 0.1234, -0.1234, 5e-324])
def test_percentage_display_stays_finite_without_changing_the_metric(value: float) -> None:
    computed = metric(MetricUnit.RATIO, value)
    with localcontext() as context:
        context.prec = 3
        text = metric_text(computed)
    assert text is not None and text.endswith("%")
    assert "inf" not in text.lower() and "nan" not in text.lower()
    # The finite extreme is 1 followed by 310 zeros; ordinary ratios retain one decimal.
    expected = ("-" if value < 0 else "") + "1" + "0" * 310 + ".0%"
    if abs(value) == 1e308:
        assert text == expected
    else:
        assert text == f"{value * 100:.1f}%"
    assert computed.value == value


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


def test_a_claimed_figure_is_never_found_inside_a_longer_figure() -> None:
    text = "The current ratio is 1.50, the quick ratio 1.5 and revenue 1,250."
    claims = [
        Claim(metric_id="quick_ratio", period_key=PERIOD.key, text_span="1.5"),
        Claim(metric_id="current_ratio", period_key=PERIOD.key, text_span="1.50"),
        Claim(metric_id="revenue_growth", period_key=PERIOD.key, text_span="250"),
    ]
    anchors = frozenset(metric_anchor(c.metric_id, c.period_key) for c in claims)
    two = Sentence(text=text, claims=claims[:2])
    links = [(p.text, p.href) for p in sentence_parts(two, anchors) if p.href]
    assert links == [
        ("1.50", f"#{metric_anchor('current_ratio', PERIOD.key)}"),
        ("1.5", f"#{metric_anchor('quick_ratio', PERIOD.key)}"),
    ]
    with pytest.raises(ValueError, match="'250' is not in"):  # only inside 1,250
        sentence_parts(Sentence(text=text, claims=claims), anchors)


def test_a_page_declared_huge_is_rendered_no_longer_than_the_limit(tmp_path: Path) -> None:
    pdf = tmp_path / "huge.pdf"
    pdf.write_bytes(one_page_pdf(14_400, 7_200))  # the PDF maximum: 200 by 100 inches
    target = tmp_path / "1.png"
    render_page(pdf, 1, target)
    with Image.open(target) as image:
        assert image.size == (MAX_RENDER_SIDE, MAX_RENDER_SIDE // 2)  # not 28,800 by 14,400


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


def test_a_stored_narrative_with_an_unexpected_language_is_withheld_not_a_server_error() -> None:
    sha = "a" * 64
    narrative = claimed("The ratio is 2.00x.", "2.00x").model_copy(update={"language": "fr"})
    analysis = Analysis(
        job_id="b" * 32,
        upload_sha256=sha,
        page_count=1,
        language="en",
        statements=[statement(sha)],
        reviews=[review()],
        metrics=[metric(MetricUnit.TIMES, 2.0)],
        narrative=narrative,
    )
    view = analysis_view(analysis)
    assert view.narrative is None
    assert view.summary_notice == "Summary withheld: its text or references could not be verified."


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


def test_document_flags_show_ordered_reasons_without_overriding_review_status(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            flags=["statement_not_extracted:income", "range_not_converted"],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert 'class="badge needs_review">needs_review</span>' in page
    assert '<h2 id="document-notices">Document notices</h2>' in page
    assert page.index("statement_not_extracted:income") < page.index("range_not_converted")


def test_document_informational_flag_keeps_completed_status_and_unknown_flag_is_escaped(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        informational = finished_job(settings, client, flags=["low_selectivity"])
        informational_page = client.get(f"/analyses/{informational}/view").text
        hostile = finished_job(settings, client, flags=["<script>notice</script>"])
        hostile_page = client.get(f"/analyses/{hostile}/view").text
    assert 'class="badge completed">completed</span>' in informational_page
    assert "low_selectivity" in informational_page
    assert "&lt;script&gt;notice&lt;/script&gt;" in hostile_page
    assert "<script>notice</script>" not in hostile_page


def test_document_without_flags_has_no_document_notices_section(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, flags=[])
        page = client.get(f"/analyses/{job_id}/view").text
    assert 'class="badge completed">completed</span>' in page
    assert "Document notices" not in page


def test_a_declined_document_shows_the_reason_and_no_metrics(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    decline = Decline(code="bank", reason="declined:bank: total deposits on page 3")
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            statements=[],
            reviews=[],
            metrics=[],
            decline=decline,
            flags=["statement_not_extracted:income"],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert "Declined (bank)" in page and "total deposits on page 3" in page
    assert "statement_not_extracted:income" in page
    assert "<h2>Metrics</h2>" not in page


def test_a_document_hold_remains_visible_with_document_notices(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, hold="identity mismatch", flags=["unknown_reason"])
        page = client.get(f"/analyses/{job_id}/view").text
    assert "Every statement is held for review." in page and "identity mismatch" in page
    assert "unknown_reason" in page


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
    assert (
        "The current ratio is 1.50." in page
        and "Stored summary; not verified as a direct template." in page
    )
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


def test_a_job_held_only_by_a_document_notice_does_not_say_figures_failed(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        flagged = finished_job(settings, client, flags=["range_not_converted:5-6"])
        empty = finished_job(settings, client, statements=[], reviews=[], metrics=[])
        flagged_page = client.get(f"/analyses/{flagged}/view").text
        empty_page = client.get(f"/analyses/{empty}/view").text
    assert "Every statement passed its own checks" in flagged_page
    assert "did not pass their own checks" not in flagged_page
    assert "No statement was extracted from this document." in empty_page
    assert "did not pass their own checks" not in empty_page


def test_a_page_past_the_page_limit_is_never_rendered(tmp_path: Path) -> None:
    settings = settings_at(tmp_path).model_copy(update={"max_pages": 1})
    document = pdfium.PdfDocument.new()
    document.new_page(PAGE_W, PAGE_H)
    document.new_page(PAGE_W, PAGE_H)
    buffer = io.BytesIO()
    document.save(buffer)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        sha256 = client.post(
            "/documents",
            content=buffer.getvalue(),
            headers={"Content-Type": "application/pdf", "X-Filename": "two.pdf"},
        ).json()["sha256"]
        assert client.get(f"/documents/{sha256}/pages/1.png").status_code == 200
        for path in (f"/documents/{sha256}/pages/2.png", f"/documents/{sha256}/pages/2"):
            response = client.get(path)
            assert response.status_code == 404 and "past the limit of 1" in response.text
    assert not (settings.root / "pages" / sha256 / "2.png").exists()


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
    assert 'href="#metric-current_ratio-2025-12-31"' in page and ">1.50</bdi></a>" in page
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


@pytest.mark.parametrize("token", ["nan", "NaN", "inf", "+Inf", "-Infinity", "1e999"])
@pytest.mark.parametrize("position", range(4))
def test_nonfinite_source_boxes_are_rejected_before_css(
    tmp_path: Path, token: str, position: int
) -> None:
    coordinates = ["0", "0", "10", "10"]
    coordinates[position] = token
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        analysis = JobStore(settings.root).load_analysis(job_id)
        assert analysis is not None
        response = client.get(
            f"/documents/{analysis.upload_sha256}/pages/1",
            params={"box": ",".join(coordinates)},  # includes URL-encoded + and commas
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "box coordinates must be finite"
    assert "nan%" not in response.text and "inf%" not in response.text


def test_finite_box_boundaries_and_negative_edge_clipping_remain_valid(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        analysis = JobStore(settings.root).load_analysis(job_id)
        assert analysis is not None
        url = f"/documents/{analysis.upload_sha256}/pages/1"
        full = client.get(url, params={"box": "0,0,600,800"})
        cut = client.get(url, params={"box": "-10,-10,10,10"})
    assert full.status_code == cut.status_code == 200
    assert "left:0.000%;top:0.000%;width:100.000%;height:100.000%" in full.text
    assert "cut to the page" not in full.text
    assert "cut to the page" in cut.text


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.parametrize(
    "caveat_id", ["scale_assumed_units", "currency_from_domicile", "currency_inferred"]
)
def test_statement_caveats_keep_evidence_and_mark_only_affected_values(
    tmp_path: Path, language: str, caveat_id: str
) -> None:
    caveat = Caveat.model_validate(
        {
            "id": caveat_id,
            "evidence": {"currency": 'SAR "<script>عملة</script>"'},
        }
    )
    printed = statement("a" * 64, language).model_copy(update={"caveats": [caveat]})
    item = printed.line_items[0]
    # The same row can contain a printed percentage, an amount, and an unread cell.
    percent = item.cells[0].model_copy(update={"reported": Decimal("25"), "flags": ["percent"]})
    unread = item.cells[0].model_copy(update={"reported": None})
    printed = printed.model_copy(
        update={"line_items": [item.model_copy(update={"cells": [item.cells[0], percent, unread]})]}
    )
    shown = statement_view(printed, review())
    assert shown.caveats[0].id == caveat.id
    assert shown.caveats[0].evidence == caveat.evidence
    assert shown.status == "passed"
    assert len(shown.rows[0].cells[0][0].caveats) == 1
    assert shown.rows[0].cells[0][1].caveats == ()
    assert shown.rows[0].cells[0][2].caveats == ()
    settings = settings_at(tmp_path)
    cash = metric(MetricUnit.CURRENCY, 1.25).model_copy(update={"caveats": [caveat.id]})
    ratio = metric(MetricUnit.RATIO, 0.25).model_copy(update={"metric_id": "margin"})
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, statements=[printed], metrics=[cash, ratio])
        page = client.get(f"/analyses/{job_id}/view").text
    assert f'data-caveat="{caveat.id}"' in page
    assert "<script>عملة</script>" not in page
    assert "&lt;script&gt;عملة&lt;/script&gt;" in page
    assert "&#34;" in page  # evidence is safe in the hover title
    assert ("لم تذكر القائمة" if language == "ar" else "The statement") in page
    assert f'dir="{"rtl" if language == "ar" else "ltr"}" lang="{language}"' in page
    ratio_row = page.split('<tr id="metric-margin-2025-12-31">')[1].split("</tr>")[0]
    assert "caveat-mark" not in ratio_row.split("<td><code>")[0]
    # Inputs remain caveated currency amounts even when the resulting ratio is unaffected.
    assert "caveat-mark" in ratio_row.split("<td><code>")[1]


def test_summary_footnotes_follow_existing_claims_and_are_listed_once(tmp_path: Path) -> None:
    caveat = Caveat(id="currency_from_domicile", evidence={"currency": "SAR"})
    printed = statement("a" * 64).model_copy(update={"caveats": [caveat]})
    cash = metric(MetricUnit.CURRENCY, 1.25).model_copy(update={"caveats": [caveat.id]})
    ratio = metric(MetricUnit.RATIO, 0.25).model_copy(update={"metric_id": "margin"})
    narrative = claimed("The amount is 1.25.", "1.25")
    first = narrative.sections[0].sentences[0]
    narrative.sections[0].sentences += [
        first.model_copy(),
        Sentence(
            text="The margin is 25%.",
            claims=[Claim(metric_id="margin", period_key=PERIOD.key, text_span="25%")],
        ),
    ]
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings, client, statements=[printed], metrics=[cash, ratio], narrative=narrative
        )
        page = client.get(f"/analyses/{job_id}/view").text
    summary = page.split("<h2>Summary</h2>")[1].split("<h2>Statements</h2>")[0]
    assert summary.count('class="caveat-mark"') == 2
    assert summary.count("The statement does not name its currency.") == 1
    assert 'href="#metric-current_ratio-2025-12-31"' in summary
    ratio_sentence = summary.split('href="#metric-margin-2025-12-31"')[1].split("</p>")[0]
    assert "caveat-mark" not in ratio_sentence


def test_uncaveated_statements_and_metric_values_have_no_footnote_marks(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        page = client.get(f"/analyses/{job_id}/view").text
    assert 'class="caveat-mark"' not in page
    assert "data-caveat=" not in page


def test_summary_caveats_use_the_summary_language_and_preserve_cross_statement_scope() -> None:
    caveat = Caveat(id="currency_inferred", evidence={"currency": "SAR"})
    printed = statement("a" * 64).model_copy(update={"caveats": [caveat]})
    computed = metric(MetricUnit.RATIO, 0.25).model_copy(update={"caveats": [caveat.id]})
    narrative = claimed("الهامش 25%.", "25%").model_copy(update={"language": "ar"})
    analysis = Analysis(
        job_id="b" * 32,
        upload_sha256="a" * 64,
        page_count=1,
        language="ar",
        statements=[printed],
        reviews=[review()],
        metrics=[computed],
        narrative=narrative,
    )
    shown = analysis_view(analysis)
    assert len(shown.metrics[0].notes) == 1  # keep analytics' explicit caveat on this ratio
    assert shown.footnotes[0].language == "ar"
    assert (
        shown.footnotes[0].text
        == "لم تذكر القائمة عملتها. اعتُمد \u2066SAR\u2069 لأنه العملة الأكثر ذكراً في المستند."
    )
    assert shown.footnotes[0].evidence == caveat.evidence


def direct_summary_analysis(language: str = "en") -> Analysis:
    from fra_model import SummaryBuilt, build_summary

    printed = statement("a" * 64, language)
    cell = printed.line_items[0].cells[0]
    cell.reported = Decimal("1234567")
    cell.flags = []
    source = Analysis(
        job_id="b" * 32,
        upload_sha256="a" * 64,
        page_count=1,
        language=language,
        statements=[printed],
        reviews=[review()],
        metrics=[metric(MetricUnit.TIMES, 1.5)],
    )
    result = build_summary(source)
    assert isinstance(result, SummaryBuilt)
    source.narrative = result.narrative
    return source


@pytest.mark.parametrize("language", ["en", "ar"])
def test_direct_summary_is_checked_again_at_render_and_links_to_sources(language: str) -> None:
    source = direct_summary_analysis(language)
    view = analysis_view(source)
    assert view.narrative is not None
    assert view.summary_notice is None
    assert any(
        part.href == "#metric-current_ratio-2025-12-31"
        for section in view.narrative
        for parts in section.sentences
        for part in parts
    )
    assert "/pages/1?" in view.metrics[0].inputs[0].source
    assert ("قالب" if language == "ar" else "Direct template") in view.origin_text
    assert source.narrative is not None
    source.narrative.sections[0].sentences[0].text += " rose 20%."
    withheld = analysis_view(source)
    assert withheld.narrative is None and withheld.summary_notice


def test_arabic_direct_template_page_has_isolated_numeric_links_and_honest_copy(
    tmp_path: Path,
) -> None:
    from fra_model import SummaryBuilt, build_summary

    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        store = JobStore(settings.root)
        saved = store.load_analysis(job_id)
        assert saved is not None
        source = direct_summary_analysis("ar")
        source.job_id = job_id
        source.upload_sha256 = saved.upload_sha256
        source.statements[0].document_sha256 = saved.upload_sha256
        result = build_summary(source)
        assert isinstance(result, SummaryBuilt)
        source.narrative = result.narrative
        # JobStore.finish accepts only a running job; replace the stored artifact atomically
        # through its existing path for this rendering test.
        path = settings.root / "analyses" / f"{job_id}.json"
        assert path.is_file()
        path.write_text(source.model_dump_json())
        page = client.get(f"/analyses/{job_id}/view").text
    assert 'dir="rtl" lang="ar"' in page
    assert '<bdi dir="ltr">1.50×</bdi>' in page
    assert "قالب مباشر" in page and "Written by the model" not in page


def test_arabic_decline_page_shows_plain_reason_and_no_financial_outputs(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    reason = (
        "This report is mainly in Arabic. Only English financial reports are analyzed, "
        "so no statements, metrics or summary were produced."
    )
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        upload = " ".join(client.get("/").text.split())
        assert "in English, digital or scanned" in upload
        assert "Arabic reports, banks and insurers are declined with the reason" in upload
        job_id = finished_job(
            settings,
            client,
            language="ar",
            statements=[],
            reviews=[],
            metrics=[],
            decline=Decline(code="unsupported_language", reason=reason),
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert "Declined (unsupported_language)" in page and reason in page
    assert 'class="badge needs_review">needs_review</span>' in page
    assert 'id="metrics"' not in page and 'class="statement"' not in page
    assert "1.50×" not in page
