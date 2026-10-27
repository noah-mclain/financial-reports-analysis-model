"""The pages: what the results show and link to, the source viewer's box, and the display
formatting, which rounds and never computes."""

from __future__ import annotations

import ast
import io
import re
from datetime import date
from decimal import Decimal, localcontext
from html.parser import HTMLParser
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
from fra_core.schemas.caveat import CaveatId
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


def test_source_analysis_parameter_is_url_encoded() -> None:
    url = source_url("a" * 64, provenance(3), '" onclick="alert(1)')
    assert "analysis=%22+onclick%3D%22alert%281%29" in url
    assert '" onclick="alert(1)' not in url


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
    assert (
        view.summary_notice
        == "Summary withheld because its text or references could not be verified."
    )


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


class VisibleText(HTMLParser):
    """Collect rendered text separately from collapsed technical disclosures."""

    def __init__(self) -> None:
        super().__init__()
        self.visible: list[str] = []
        self.technical: list[str] = []
        self._details = 0
        self._technical_details = 0
        self._technical_stack: list[bool] = []
        self._head = False
        self._style = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self._head = True
        if tag in {"style", "script"}:
            self._style += 1
        if tag == "details":
            self._details += 1
            class_value = dict(attrs).get("class")
            technical = class_value is not None and "technical-details" in class_value.split()
            self._technical_stack.append(technical)
            if technical:
                self._technical_details += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self._details -= 1
            if self._technical_stack.pop():
                self._technical_details -= 1
        if tag == "head":
            self._head = False
        if tag in {"style", "script"}:
            self._style -= 1

    def handle_data(self, data: str) -> None:
        if not self._head and not self._style:
            (self.technical if self._technical_details else self.visible).append(data)


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
    parsed = VisibleText()
    parsed.feed(page)
    assert "Some digits may have been read incorrectly" in " ".join(parsed.visible)
    assert "digit_suspect" in parsed.technical
    assert "digit_suspect" not in " ".join(parsed.visible)
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", " ".join(parsed.visible))
    assert 'class="flag-marker" aria-label="Needs review"' in page
    assert "1.50×" in page and "total_current_assets / total_current_liabilities" in page
    assert "Total current assets / Total current liabilities" in page
    assert "total_current_assets / total_current_liabilities" not in " ".join(parsed.visible)
    assert "total_current_assets / total_current_liabilities" in " ".join(parsed.technical)
    assert "1 pages · detected language en" in page
    assert "No written summary yet" in page


def test_held_results_lead_with_a_plain_verdict_and_actionable_summary(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            reviews=[review("needs_review", ["subtotal_failed", "numbers_missing:2"])],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert page.index('id="verdict"') < page.index('id="summary"')
    assert "Held for review" in page
    assert "A statement subtotal did not match" in page
    assert "2 figures were not found where expected" in page
    assert "subtotal_failed" in page and "numbers_missing:2" in page
    assert "Review the flagged figures and statement checks" in page
    assert 'id="metrics-review-note"' in page
    assert "Figures are not verified" in page
    assert page.count('class="plain-reasons"') == 0
    assert 'href="#metric-current_ratio-2025-12-31"' in page


def test_informational_document_flag_does_not_hide_a_statement_hold_reason(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            flags=["low_selectivity"],
            reviews=[review("needs_review", ["subtotal_failed"])],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    verdict = page.split('<section class="verdict hold"', 1)[1].split("</section>", 1)[0]
    headline, _ = verdict.split('<details class="review-details"', 1)
    assert "A statement subtotal did not match" in headline
    assert "Low selectivity" not in headline
    notices = page.split('<details class="document-notices"', 1)[1]
    assert '<code dir="ltr">low_selectivity</code>' in notices


def test_many_held_reasons_keep_the_verdict_short_and_details_available(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    reasons = [f"unrecognized_reason_{index}" for index in range(40)]
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            reviews=[review("needs_review", reasons)],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    verdict = page.split('<section class="verdict hold"', 1)[1].split("</section>", 1)[0]
    assert "One or more checks are unresolved" in verdict
    headline, details = verdict.split('<details class="review-details"', 1)
    assert "Review note — Unrecognized reason 0" in headline
    assert "unrecognized_reason_0" not in headline
    assert "Review details" in verdict
    assert "unrecognized_reason_0" in details and "unrecognized_reason_39" in details
    assert verdict.count('<summary aria-label="Technical details for document review">') == 1


def test_review_reasons_are_plain_grouped_ordered_and_traceable(tmp_path: Path) -> None:
    reasons = [
        "identity_not_checked:missing_values",
        "subtotal_not_checked:20",
        "unchecked",
        "unparsed:17",
        "row_misaligned:4",
        "row_alignment_unresolved",
        "critical_item_unmapped:total_assets",
        "critical_item_unmapped:total_current_assets",
        "critical_item_unmapped:total_current_liabilities",
        "critical_item_unmapped:total_equity",
        "critical_item_unmapped:total_liabilities",
        "critical_item_unmapped:total_liabilities_and_equity",
        "critical_item_unmapped:equity_attributable_parent",
        "ambiguous_separator:2",
        "made_up_reason:some_value",
    ]
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            reviews=[review("needs_review", reasons)],
            hold=(
                "industry_uncertain:weak_verdict: bank, score 14.0, 3 distinct cues "
                "(verdict threshold 14.0, margin 2.0); evidence: page 3 'quoted phrase'"
            ),
            flags=["statement_not_extracted:income", "low_selectivity"],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    parser = VisibleText()
    parser.feed(page)
    visible = " ".join(parser.visible)
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", visible)
    assert (
        "The company may be a bank, but that could not be confirmed, so its statements are held for review."
        in visible
    )
    assert "14.0" not in visible and "quoted phrase" not in visible
    assert (
        "The balance check (assets = liabilities + equity) could not be run because some figures are missing"
        in visible
    )
    assert "identity_not_checked:missing_values" in parser.technical
    assert all(code in parser.technical for code in reasons)
    assert any(
        "industry_uncertain:weak_verdict: bank, score 14.0" in item for item in parser.technical
    )
    assert "statement_not_extracted:income" in parser.technical
    assert "low_selectivity" in parser.technical
    statement_reasons = page.split("<h4>Review reasons</h4>", 1)[1].split("</ul>", 1)[0]
    assert (
        "20 totals could not be recalculated from the lines above them, so they are not confirmed"
        in statement_reasons
    )
    assert "17 figures could not be read as numbers" in statement_reasons
    assert "4 rows may be shifted against their figures" in statement_reasons
    assert "2 figures use a separator that can be read two ways (comma or dot)" in statement_reasons
    assert (
        "7 key lines were not identified (total assets, total current assets, "
        "total current liabilities, total equity, total liabilities, total liabilities and "
        "equity, equity attributable to owners)." in statement_reasons
    )
    assert "Review note — Made up reason some value" in statement_reasons
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", statement_reasons)
    assert all(code not in statement_reasons for code in reasons)
    assert statement_reasons.index("balance check") < statement_reasons.index("totals could not")
    assert statement_reasons.index("totals could not") < statement_reasons.index(
        "figures could not be read"
    )
    assert statement_reasons.index("figures could not be read") < statement_reasons.index(
        "rows may be shifted"
    )
    assert statement_reasons.index("rows may be shifted") < statement_reasons.index("key lines")
    assert statement_reasons.index("key lines") < statement_reasons.index("Review note")


def test_review_reason_producers_have_dedicated_plain_labels() -> None:
    from typing import Literal

    from fra_api.views import flag_label, hold_label, plain_label, reason_label
    from fra_core.schemas import CheckResult, MappingFinding
    from fra_ingest.review import _HOLD_FLAGS, _WARNING_FLAGS, CRITICAL_CELL_FLAGS, review_statement

    produced = [
        "identity_failed",
        "tie_failed",
        "eps_failed",
        "digit_suspect:2",
        "period_outlier:2",
        "fraction_among_whole:2",
        "row_realigned:2",
        "numeric_columns_split:2",
        "ocr_parenthesis_repaired",
        "period_unbound:2",
        "period_conflict:differing_periods",
        "scale_conflict",
        "currency_conflict",
        "currency_missing",
        "scale_implausible",
        "value_without_box:r2c3",
        "likely_bank",
        "likely_other_financial:brokerage",
        "image_pages_not_read:2",
        "ocr_failed_pages:2",
        "garbled_text_layers_not_read:2",
        "no_statements_found",
        "convert_failed:pages_2_4",
        "ocr_unavailable:pages_2_4",
        "page_outside_range:pages_2_4",
        "page_not_converted:2",
        "memory_over_budget",
        "period_evidence_unavailable:2:raw_source_mismatch",
        "error:ValueError: private detail",
    ]
    produced.extend(f"{flag}:2" for flag in CRITICAL_CELL_FLAGS)
    produced.extend(_HOLD_FLAGS)
    produced.extend(_WARNING_FLAGS)
    fixture_statement = statement("a" * 64).model_copy(
        update={
            "line_items": [
                statement("a" * 64)
                .line_items[0]
                .model_copy(
                    update={
                        "cells": [
                            statement("a" * 64)
                            .line_items[0]
                            .cells[0]
                            .model_copy(update={"flags": [*CRITICAL_CELL_FLAGS, "numbers_missing"]})
                        ]
                    }
                )
            ],
            "flags": list(_HOLD_FLAGS) + list(_WARNING_FLAGS),
            "mapping_findings": (
                MappingFinding(item_id="total_assets", observed_period_keys=(PERIOD.key,)),
            ),
        }
    )
    check_kinds: list[
        tuple[
            Literal["subtotal", "balance_identity", "net_profit_tie", "eps"],
            Literal["pass", "fail", "skipped"],
        ]
    ] = [
        ("balance_identity", "skipped"),
        ("subtotal", "skipped"),
        ("subtotal", "fail"),
        ("balance_identity", "fail"),
        ("net_profit_tie", "fail"),
        ("eps", "fail"),
    ]
    generated = review_statement(
        fixture_statement,
        [
            CheckResult(
                id=f"{kind}-{status}",
                statement_id="s1",
                kind=kind,
                period_key=PERIOD.key,
                status=status,
                detail="missing_values" if status == "skipped" else "",
            )
            for kind, status in check_kinds
        ],
        primary=True,
    )
    produced.extend(generated.reasons)
    produced.extend(generated.warnings)
    producer_literals = (
        "identity_failed",
        "tie_failed",
        "eps_failed",
        "period_unbound",
        "period_conflict",
        "scale_conflict",
        "currency_conflict",
        "currency_missing",
        "scale_implausible",
        "value_without_box",
        "likely_other_financial",
        "image_pages_not_read",
        "ocr_failed_pages",
        "garbled_text_layers_not_read",
        "no_statements_found",
        "convert_failed",
        "ocr_unavailable",
        "page_outside_range",
        "page_not_converted",
        "memory_over_budget",
        "period_evidence_unavailable",
        "error:",
    )
    source_roots = [
        Path(__file__).parents[3] / "packages/ingest/src",
        Path(__file__).parents[3] / "packages/analytics/src",
        Path(__file__).parents[3] / "apps/worker/src",
    ]
    source_text = "\n".join(
        path.read_text() for root in source_roots for path in root.rglob("*.py")
    )
    assert all(literal in source_text for literal in producer_literals)
    produced.extend(producer_literals)
    for code in produced:
        label = reason_label(code)
        if code not in generated.reasons:
            # Flags and warnings render through flag_label on the page.
            flagged = flag_label(code)
            assert ":" not in flagged and not re.search(r"\b[a-z]+_[a-z_]+\b", flagged), (
                code,
                flagged,
            )
        assert "Review note" not in label, code
        assert ":" not in label and not re.search(r"\b[a-z]+_[a-z_]+\b", label), (code, label)
        assert not re.search(r"r\d+c\d+", label), (code, label)
    for code in _HOLD_FLAGS:
        assert hold_label(code) != f"Review note — {code}"
    assert flag_label("error:ValueError: private detail") == "A processing error occurred"
    assert plain_label("ebitda_margin") == "EBITDA margin"
    assert plain_label("eps_basic") == "EPS basic"
    assert plain_label("dso") == "DSO"
    assert plain_label("ebit") == "EBIT"
    assert plain_label("roe") == "ROE"
    assert plain_label("roa") == "ROA"
    assert "eBITDA" not in " ".join(
        item.label
        for item in __import__("fra_api.views", fromlist=["review_reasons"]).review_reasons(
            ("critical_item_unmapped:ebitda",)
        )
    )


def test_failed_reconciliation_reasons_are_first_in_review_lead() -> None:
    from fra_api.views import reason_label, review_reasons

    for failed in ("identity_failed", "tie_failed", "eps_failed"):
        reasons = review_reasons(("unparsed:1", failed, "subtotal_failed"))
        assert reasons[0].label == reason_label(failed)
        assert reasons[0].label != reason_label("unparsed:1")
        assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", reasons[0].label)


def test_metric_template_uses_defined_name_for_technical_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jinja2 import StrictUndefined

    from fra_api import pages

    monkeypatch.setattr(pages._templates, "undefined", StrictUndefined)
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, metrics=[metric(MetricUnit.RATIO, None)])
        response = client.get(f"/analyses/{job_id}/view")
    assert response.status_code == 200
    assert 'aria-label="Technical formula for Current ratio"' in response.text
    assert 'aria-label="Technical details for Current ratio checks"' in response.text


def test_all_page_templates_render_branches_with_strict_undefined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise each results-page branch and all other templates with strict Jinja lookup."""
    from jinja2 import StrictUndefined

    from fra_api import pages
    from fra_ingest.review import _WARNING_FLAGS

    monkeypatch.setattr(pages._templates, "undefined", StrictUndefined)
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        assert client.get("/").status_code == 200

        warning_review = review(
            "needs_review",
            [
                "identity_not_checked:missing_values",
                "subtotal_not_checked:2",
                "unparsed:1",
                "row_misaligned:1",
                "critical_item_unmapped:total_assets",
                "made_up_reason:sample",
            ],
        ).model_copy(update={"warnings": list(_WARNING_FLAGS)})
        narrated = Narrative(
            language="en",
            sections=[
                NarrativeSection(
                    heading=SectionHeading.OVERVIEW,
                    sentences=[Sentence(text="The report is available for review.")],
                )
            ],
        )
        cases: list[dict[str, object]] = [
            {},  # completed, with metric formula and inputs
            {"metrics": [metric(MetricUnit.RATIO, None)]},  # metric without inputs
            {
                "metrics": [
                    metric(MetricUnit.RATIO, 1.5).model_copy(
                        update={"flags": ["average_fallback_to_closing"]}
                    )
                ]
            },
            {"reviews": [warning_review], "hold": "industry_uncertain:no_verdict"},
            {"decline": Decline(code="bank", reason="declined:bank: evidence")},
            {"narrative": narrated, "narrative_partial": True},
            {"language": "ar", "statements": [statement("a" * 64, language="ar")]},
        ]
        responses = []
        for values in cases:
            job_id = finished_job(settings, client, **values)
            assert client.get(f"/jobs/{job_id}/view").status_code == 200
            responses.append(client.get(f"/analyses/{job_id}/view"))
        sha = responses[0].text.split("/documents/", 1)[1].split("/pages/", 1)[0]
        source_response = client.get(f"/documents/{sha}/pages/1")

    assert source_response.status_code == 200
    assert all(response.status_code == 200 for response in responses)
    for response in [source_response, *responses]:
        aria_values = re.findall(r'\b(?:aria-label|title)="([^"]*)"', response.text)
        assert all(value.strip() and "  " not in value for value in aria_values), aria_values


_PRODUCER_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:$|:)")
_PRODUCER_LINE = re.compile(r"flag|reason|warning|hold|decline|mapping_evidence|finding", re.I)
_PRODUCER_LITERAL = re.compile(r"['\"]([a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:[:][a-zA-Z0-9_]+)?)['\"]")


def producer_literals(producer_roots: tuple[Path, ...], root: Path) -> dict[str, set[str]]:
    """Every flag/reason-like snake_case literal in all Python files under the roots."""
    findings: dict[str, set[str]] = {}
    for producer_root in producer_roots:
        for path in sorted(producer_root.rglob("*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            constant_lines: set[int] = set()
            try:
                tree = ast.parse("\n".join(lines))
            except SyntaxError as exc:
                pytest.fail(f"Could not inspect producer source {path}: {exc}")
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = [target.id for target in targets if isinstance(target, ast.Name)]
                    value = node.value
                    if any(
                        re.search(r"FLAG|REASON|WARNING|HOLD", name, re.I) for name in names
                    ) and isinstance(value, (ast.Tuple, ast.Set, ast.Dict)):
                        constant_lines.update(
                            range(node.lineno, (node.end_lineno or node.lineno) + 1)
                        )
            for function in ast.walk(tree):
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    _PRODUCER_LINE.search(function.name)
                ):
                    constant_lines.update(
                        range(function.lineno, (function.end_lineno or function.lineno) + 1)
                    )
            for line_no, line in enumerate(lines, 1):
                if not _PRODUCER_LINE.search(line) and line_no not in constant_lines:
                    continue
                for value in _PRODUCER_LITERAL.findall(line):
                    if _PRODUCER_TOKEN.match(value):
                        findings.setdefault(value.split(":", 1)[0], set()).add(
                            f"{path.relative_to(root)}:{line_no} ({value})"
                        )
    return findings


def test_producer_label_inventory_covers_flag_and_reason_literals() -> None:
    """Catch newly added producer codes before they reach a page without a label."""
    from fra_api.views import FLAG_LABELS

    root = Path(__file__).parents[3]
    findings = producer_literals(
        (
            root / "packages/ingest/src/fra_ingest",
            root / "packages/analytics/src/fra_analytics",
            root / "packages/core/src/fra_core",
            root / "apps/worker/src/fra_worker",
        ),
        root,
    )

    reason_prefixes = {
        "identity_not_checked",
        "subtotal_not_checked",
        "critical_item_unmapped",
        "ambiguous_separator",
        "unparsed",
        "numbers_missing",
        "row_misaligned",
        "row_alignment_unresolved",
        "subtotal_failed",
        "duplicate_statement",
        "implausible_magnitude",
        "industry_uncertain",
        "no_verdict",
        "other_financial",
        "too_narrow",
        "near_threshold",
        "weak_verdict",
    }
    ignored_groups = {
        "Internal provenance or measurement keys, never a displayed flag": (
            "peak_footprint_gb",
            "period_evidence",
            "investment_holding",
            "canonical_id",
            "line_items",
            "mapping_findings",
            "mapping_flag",
            "holdout_availability",
            "holdout_for_scoring",
            "is_glyph_placeholder",
            "is_provenance_flag",
            "is_provenance_warning",
            "compute_and_flag",
        ),
        "Model-mapping decision reasons: shown only inside Technical details as mapping evidence": (
            "anchor_scope_unsettled",
            "explicit_model_abstention",
            "invalid_model_response",
            "model_abstention",
            "no_supported_mapping",
            "proposal_disagrees",
            "source_cell_uncertainty",
            "source_relationship_conflict",
            "source_uncertainty",
            "statement_type_unsupported",
            "target_value_missing",
            "unique_rule_mapping",
            "mapping_evidence",
        ),
        "Label-source and header-recovery diagnostics stored on the analysis, not rendered": (
            "ambiguous_cell",
            "ambiguous_columns",
            "ambiguous_page",
            "ambiguous_rows",
            "ambiguous_source_geometry",
            "ambiguous_table",
            "conflicting_context",
            "conflicting_periods",
            "differing_periods",
            "empty_reading",
            "geometry_invalid",
            "header_recovered",
            "header_recovery_context",
            "invalid_line_geometry",
            "invalid_source",
            "labels_missing_or_conflicting",
            "missing_bbox",
            "missing_cell",
            "no_attributable_reading",
            "outside_source_rectangle",
            "period_uncertain",
            "source_conflicting_title",
            "source_missing",
            "source_non_statement_page",
            "source_primary_ambiguous",
            "source_scope_unproven",
            "status_unpreserved",
            "unparseable_observation",
        ),
        "Check messages: the page shows only the eps_failed reason, not these details": (
            "profit_scale_unsettled",
            "share_scale_unsettled",
        ),
        "Decline code, worded by decline_label": ("unsupported_language",),
    }
    ignored = {name: why for why, names in ignored_groups.items() for name in names}
    known = set(FLAG_LABELS) | reason_prefixes
    unknown = {
        prefix: sorted(locations)
        for prefix, locations in findings.items()
        if prefix not in known and prefix not in ignored
    }
    assert not unknown, f"Producer literals lack a page label or justified ignore: {unknown}"


@pytest.mark.parametrize(
    "flag",
    ["range_partially_converted:3-5", "input_may_be_unmapped:revenue"],
)
def test_flags_built_from_f_strings_have_dedicated_labels(flag: str) -> None:
    """The scan reads only quoted literals; these producers build ``prefix:{value}`` strings."""
    from fra_api.views import FLAG_LABELS, flag_label

    assert flag.partition(":")[0] in FLAG_LABELS
    assert flag_label(flag) == FLAG_LABELS[flag.partition(":")[0]]


def test_producer_scan_reports_an_unlabeled_literal(tmp_path: Path) -> None:
    """The scan must see a brand-new flag in any module, whatever its file name."""
    source = tmp_path / "pkg" / "anything_at_all.py"
    source.parent.mkdir()
    source.write_text('flags.append("brand_new_flag:3")\n', encoding="utf-8")

    found = producer_literals((tmp_path / "pkg",), tmp_path)

    assert list(found) == ["brand_new_flag"]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("unparsed:1", "1 figure could not be read as a number"),
        ("numbers_missing:1", "1 figure was not found where expected"),
        ("unparsed:2", "2 figures could not be read as numbers"),
        (
            "subtotal_not_checked:1",
            "1 total could not be recalculated from the lines above it, so it is not confirmed",
        ),
        (
            "subtotal_not_checked:2",
            "2 totals could not be recalculated from the lines above them, so they are not confirmed",
        ),
    ],
)
def test_review_count_labels_have_singular_and_plural_grammar(reason: str, expected: str) -> None:
    from fra_api.views import reason_label

    assert reason_label(reason) == expected


@pytest.mark.parametrize(
    "reason",
    [
        "industry_uncertain:no_verdict: unknown, score 0.0, 0 distinct cues; evidence: none",
        "industry_uncertain:other_financial: other_financial/brokerage, score 14.0, 3 distinct cues; evidence: none",
        "industry_uncertain:too_narrow: corporate, score 14.0, 1 distinct cues; evidence: none",
        "industry_uncertain:near_threshold: corporate, score 12.0, 3 distinct cues; evidence: none",
        "industry_uncertain:weak_verdict: insurer, score 14.0, 3 distinct cues; evidence: none",
    ],
)
def test_industry_holds_hide_codes_scores_and_evidence(reason: str) -> None:
    from fra_api.views import hold_label

    label = hold_label(reason)
    assert "held for review" in label
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b|\d+\.\d+)", label)


@pytest.mark.parametrize(
    "code", ["no_verdict", "other_financial", "too_narrow", "near_threshold", "weak_verdict"]
)
def test_short_industry_hold_reasons_render_as_plain_language(tmp_path: Path, code: str) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            reviews=[review("needs_review", [f"industry_uncertain:{code}"])],
            hold=f"industry_uncertain:{code}",
        )
        response = client.get(f"/analyses/{job_id}/view")
    assert response.status_code == 200
    parser = VisibleText()
    parser.feed(response.text)
    visible = " ".join(parser.visible)
    assert "held for review" in visible
    assert f"industry_uncertain:{code}" not in visible
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", visible)


def test_industry_label_functions_cover_short_long_and_produced_review_codes() -> None:
    from fra_api.views import hold_label, reason_label

    # Collected from fra_ingest/results.py and industry.py, hold_for_industry, and the
    # review reason producers in fra_ingest/review.py and fra_analytics/review.py.
    produced = [
        "industry_uncertain:no_verdict",
        "industry_uncertain:other_financial",
        "industry_uncertain:too_narrow",
        "industry_uncertain:near_threshold",
        "industry_uncertain:weak_verdict",
        "industry_uncertain:weak_verdict: bank, score 14.0, 3 distinct cues; evidence: none",
        "identity_not_checked:missing_values",
        "identity_not_checked:identity_totals_not_found",
        "subtotal_not_checked:1",
        "subtotal_failed",
        "numbers_missing:1",
        "unparsed:1",
        "implausible_magnitude:1",
        "unchecked",
        "row_alignment_unresolved",
        "row_misaligned:1",
        "ambiguous_separator:1",
        "critical_item_unmapped:total_assets",
        "duplicate_statement",
    ]
    for code in produced:
        for label in (reason_label(code), hold_label(code)):
            assert isinstance(label, str)
            assert ":" not in label
            assert not re.search(r"\b\w+_\w+\b", label)


def test_hold_without_code_uses_readable_sentence() -> None:
    from fra_api.views import hold_label

    assert hold_label("Figures need review") == "Review note — Figures need review"


def test_registry_formulas_humanize_identifiers_but_keep_function_names() -> None:
    from fra_analytics.metrics.registry import REGISTRY
    from fra_api.views import plain_formula, plain_label

    assert plain_formula("net_income / avg(total_assets)") == "Net income / avg(Total assets)"
    for metric_spec in REGISTRY:
        formula = plain_formula(metric_spec.formula)
        assert not re.search(r"\b[a-z][a-z0-9]*_[a-z0-9_]*\b", formula)
        identifiers = re.findall(r"\b[a-z][a-z0-9_]*\b", metric_spec.formula)
        function_names = {name for name in identifiers if name in {"abs", "avg"}}
        for identifier in identifiers:
            if identifier not in function_names:
                assert plain_label(identifier) in formula
        for function_name in function_names:
            assert f"{function_name}(" in formula


def test_metrics_have_headlines_and_keep_all_details_in_collapsed_sections(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    metrics = [
        metric(MetricUnit.RATIO, float(index + 1)).model_copy(
            update={
                "metric_id": f"metric_{index}",
                "flags": ["average_fallback_to_closing"] if index == 0 else [],
            }
        )
        for index in range(12)
    ]
    metrics.extend(
        metric(MetricUnit.RATIO, None).model_copy(
            update={"metric_id": f"unavailable_{index}", "flags": ["missing_input:revenue"]}
        )
        for index in range(4)
    )
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, metrics=metrics)
        page = client.get(f"/analyses/{job_id}/view").text
    headline = page.split('<section id="metrics"', 1)[1].split("</section>", 1)[0]
    assert headline.count('class="metric-card"') == 8
    assert "Metric 0" in headline and "2025-12-31" in headline
    assert 'id="all-metrics"' in page and "not computed" in page
    assert page.count('class="metric-detail"') == 16
    assert page.count('class="metric-group"') == 16
    assert page.count('aria-label="Calculation details for') == 16
    assert page.count("<summary>View calculation details for ") == 16
    assert '<summary aria-label="Technical formula for Metric 0">' in page
    assert '<code dir="ltr">total_current_assets / total_current_liabilities</code>' in page
    first_group = page.split('class="metric-group"', 1)[1].split("</article>", 1)[0]
    assert first_group.index('class="metric-period-list"') < first_group.index(
        'class="metric-detail"'
    )
    assert 'id="metric-metric_0-2025-12-31"' in first_group
    assert "2025-12-31" in first_group and "100.0%" in first_group
    assert ".annotation-tooltip:hover > .annotation-tooltip-text" in page
    assert ".annotation-tooltip:focus-within > .annotation-tooltip-text" in page
    assert ".flag-marker:focus-visible" in page
    metric_links = re.findall(r'href="(#[^"]+)"', page)
    for target in metric_links:
        if target.startswith("#metric-"):
            assert f'id="{target[1:]}"' in page
    assert "total_current_assets / total_current_liabilities" in page
    assert "Total assets" in page
    assert "Used closing balance because a prior balance was unavailable" in page
    assert "average_fallback_to_closing" in page.split("Technical details", 1)[1]
    parsed = VisibleText()
    parsed.feed(page)
    assert "average_fallback_to_closing" not in " ".join(parsed.visible)
    assert "average_fallback_to_closing" in " ".join(parsed.technical)
    assert "missing_input:revenue" not in " ".join(parsed.visible)
    assert "missing_input:revenue" in " ".join(parsed.technical)
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", " ".join(parsed.visible))


def test_metric_details_group_by_identity_and_sort_periods_without_losing_records() -> None:
    older = Period(
        key="Prior reported period", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT
    )
    newer = Period(
        key="Current reported period", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT
    )

    def at_period(metric_value: MetricValue, metric_id: str, period: Period) -> MetricValue:
        return metric_value.model_copy(
            update={
                "metric_id": metric_id,
                "period_key": period.key,
                "inputs": {
                    role: [cell.model_copy(update={"period_key": period.key}) for cell in cells]
                    for role, cells in metric_value.inputs.items()
                },
            }
        )

    metrics = [
        at_period(metric(MetricUnit.RATIO, None), "margin", newer),
        at_period(metric(MetricUnit.RATIO, 0.312), "ratio", older),
        at_period(metric(MetricUnit.RATIO, 0.25), "margin", older),
        at_period(metric(MetricUnit.RATIO, 0.47), "ratio", newer),
    ]
    analysis = Analysis(
        job_id="1" * 32,
        upload_sha256="a" * 64,
        page_count=1,
        language="en",
        statements=[statement("a" * 64).model_copy(update={"periods": [older, newer, PERIOD]})],
        reviews=[review()],
        metrics=metrics,
    )
    view = analysis_view(analysis)
    assert [
        (group.metric_id, [item.period for item in group.metrics]) for group in view.metric_groups
    ] == [("margin", [older.key, newer.key]), ("ratio", [older.key, newer.key])]
    assert view.metric_groups[0].metrics[0].value == "25.0%"
    assert view.metric_groups[0].metrics[1].value is None
    assert [item.anchor for group in view.metric_groups for item in group.metrics] == [
        metric_anchor("margin", older.key),
        metric_anchor("margin", newer.key),
        metric_anchor("ratio", older.key),
        metric_anchor("ratio", newer.key),
    ]


def test_metric_group_rendering_keeps_period_rows_visible_and_details_named(tmp_path: Path) -> None:
    older = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
    newer = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
    older_value = metric(MetricUnit.CURRENCY, 13_039_259_000).model_copy(
        update={
            "period_key": older.key,
            "metric_id": "total_assets",
            "flags": ["scale_assumed_units"],
        }
    )
    older_value = older_value.model_copy(
        update={
            "formula": "total_assets",
            "inputs": {
                role: [cell.model_copy(update={"period_key": older.key}) for cell in cells]
                for role, cells in older_value.inputs.items()
            },
        }
    )
    newer_value = older_value.model_copy(
        update={
            "period_key": newer.key,
            "value": 1.47,
            "flags": [],
            "formula": "total_current_assets / total_current_liabilities",
            "inputs": {
                role: [cell.model_copy(update={"period_key": newer.key}) for cell in cells]
                for role, cells in older_value.inputs.items()
            },
        }
    )
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        periods_statement = statement("a" * 64).model_copy(update={"periods": [older, newer]})
        job_id = finished_job(
            settings,
            client,
            statements=[periods_statement],
            metrics=[
                newer_value,
                older_value,
                metric(MetricUnit.TIMES, 1.47),
                metric(MetricUnit.RATIO, 0.312).model_copy(update={"metric_id": "net_margin"}),
            ],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    group = page.split('class="metric-group"', 1)[1].split("</article>", 1)[0]
    old_row = group.split(f'id="{metric_anchor("total_assets", older.key)}"', 1)[1].split(
        "</section>", 1
    )[0]
    new_row = group.split(f'id="{metric_anchor("total_assets", newer.key)}"', 1)[1].split(
        "</section>", 1
    )[0]
    assert group.index(
        f'<section class="metric-period" id="{metric_anchor("total_assets", older.key)}"'
    ) < group.index(
        f'<section class="metric-period" id="{metric_anchor("total_assets", newer.key)}"'
    )
    assert "2024-12-31" in old_row and "13,039,259,000" in old_row
    assert "2025-12-31" in new_row and "1 SAR" in new_row
    assert old_row.index("2024-12-31") < old_row.index("13,039,259,000")
    assert "<details" not in old_row and "<details" not in new_row
    assert group.count('class="metric-detail"') == 1
    assert group.count("View calculation details for Total assets") == 1
    assert group.count(">Total assets</h4>") == 1
    assert 'aria-label="Calculation details for Total assets"' in group
    assert group.index("1 SAR") < group.index('class="metric-detail"')
    calculation = group.split('class="metric-detail"', 1)[1]
    assert ">Total assets calculation</h5>" in calculation
    old_calculation = calculation.split('data-period="2024-12-31"', 1)[1].split("</section>", 1)[0]
    new_calculation = calculation.split('data-period="2025-12-31"', 1)[1].split("</section>", 1)[0]
    assert ">2024-12-31</h6>" in old_calculation
    assert ">2025-12-31</h6>" in new_calculation
    assert "Scale not stated; figures treated as single units" in old_calculation
    assert "Scale not stated; figures treated as single units" not in new_calculation
    assert '<code dir="ltr">total_assets</code>' in old_calculation
    assert "total_current_assets / total_current_liabilities" not in old_calculation
    assert "Total current assets / Total current liabilities" in new_calculation
    assert (
        '<code dir="ltr">total_current_assets / total_current_liabilities</code>' in new_calculation
    )
    assert "(2024-12-31)" in old_calculation and "(2025-12-31)" not in old_calculation
    assert "(2025-12-31)" in new_calculation and "(2024-12-31)" not in new_calculation
    assert "average_fallback_to_closing" not in old_row.split("</summary>", 1)[0]
    assert "Scale not stated; figures treated as single units" in old_row
    next_group = page.split('aria-labelledby="current_ratio-group-title"', 1)[1].split(
        "</article>", 1
    )[0]
    assert "1.47×" in next_group and "13,039,259,000" not in next_group
    assert (
        next_group.count('class="metric-period"') == next_group.count('class="metric-detail"') == 1
    )
    percent_group = page.split('aria-labelledby="net_margin-group-title"', 1)[1].split(
        "</article>", 1
    )[0]
    assert "31.2%" in percent_group and "1.47×" not in percent_group


def test_statements_are_collapsed_and_flags_keep_plain_and_technical_names(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    unmapped_row = (
        statement("a" * 64)
        .line_items[0]
        .model_copy(
            update={
                "canonical_id": None,
                "mapping_flag": "unmapped",
                "raw_label": "<script>Revenue</script>",
                "cells": [
                    statement("a" * 64)
                    .line_items[0]
                    .cells[0]
                    .model_copy(update={"flags": ["digit_suspect"]})
                ],
            }
        )
    )
    flagged_statement = statement("a" * 64).model_copy(update={"line_items": [unmapped_row]})
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            statements=[flagged_statement],
            reviews=[review("needs_review", ["critical_item_unmapped:total_liabilities"])],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert '<details class="statement-detail"' in page
    assert "1 key line was not identified (total liabilities)." in page
    assert "critical_item_unmapped:total_liabilities" in page
    assert "Row not mapped" in page and "unmapped" in page.split("Technical details", 1)[1]
    parsed = VisibleText()
    parsed.feed(page)
    assert "unmapped" not in " ".join(parsed.visible)
    assert "unmapped" in " ".join(parsed.technical)
    assert "digit_suspect" not in " ".join(parsed.visible)
    assert "digit_suspect" in " ".join(parsed.technical)
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", " ".join(parsed.visible))
    assert "&lt;script&gt;Revenue&lt;/script&gt;" in page
    assert "<script>Revenue</script>" not in page


def test_untrusted_statement_reasons_are_escaped_in_labels_and_details(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    hostile = "<script>review</script>"
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            reviews=[review("needs_review", [hostile])],
        )
        page = client.get(f"/analyses/{job_id}/view").text
    assert "&lt;script&gt;review&lt;/script&gt;" in page
    assert "<script>review</script>" not in page


def test_source_page_links_back_to_the_matching_analysis_and_escapes_bad_ids(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        sha = client.get(f"/jobs/{job_id}").json()["upload_sha256"]
        page = client.get(f"/analyses/{job_id}/view").text
        assert f"analysis={job_id}" in page
        source = client.get(f"/documents/{sha}/pages/1?analysis={job_id}").text
        unrelated = client.get(
            f"/documents/{sha}/pages/1?analysis=%3Cscript%3Ebad%3C%2Fscript%3E"
        ).text
    assert f'href="/analyses/{job_id}/view"' in source
    assert '<nav class="page-index" aria-label="Source page navigation">' in source
    assert ">Return to results</a>" in source
    assert re.search(r"\.page-index a\s*\{[^}]*min-height:\s*44px;", source)
    assert 'href="javascript:history.back()"' not in source
    assert "<script>bad</script>" not in unrelated


def test_source_page_does_not_link_to_an_analysis_for_another_upload(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        other_upload = client.post(
            "/documents",
            content=one_page_pdf(PAGE_W + 1),
            headers={"Content-Type": "application/pdf", "X-Filename": "other.pdf"},
        ).json()["sha256"]
        source = client.get(f"/documents/{other_upload}/pages/1", params={"analysis": job_id}).text
    assert 'href="/analyses/' not in source
    assert 'href="/"' in source


def test_global_theme_control_is_available_on_every_page(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        sha = client.get(f"/jobs/{job_id}").json()["upload_sha256"]
        pages = [
            client.get("/").text,
            client.get(f"/jobs/{job_id}/view").text,
            client.get(f"/analyses/{job_id}/view").text,
            client.get(f"/documents/{sha}/pages/1?analysis={job_id}").text,
        ]

    for page in pages:
        assert '<button type="button" data-theme-toggle hidden' in page
        assert "Switch to ${next} mode" in page
        assert 'button.textContent = theme === "dark" ? "Dark" : "Light"' in page
        assert page.index("window.fraTheme") < page.index("<style>")
    assert all('aria-label="Theme"' in page for page in pages)


def test_theme_preferences_are_validated_persisted_and_follow_system_by_default() -> None:
    template = Path(__file__).parents[1] / "src/fra_api/templates/base.html"
    page = template.read_text()
    bootstrap = page.split("<style>", maxsplit=1)[0]
    assert 'const key = "fra-theme"' in bootstrap
    assert "localStorage.getItem(key)" in bootstrap
    assert 'stored === "light" || stored === "dark"' in bootstrap
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in bootstrap
    assert 'preference || (media.matches ? "dark" : "light")' in bootstrap
    assert "localStorage.setItem(key, preference)" in bootstrap
    assert 'media.addEventListener("change"' in bootstrap
    assert "if (!preference) apply()" in bootstrap
    assert "try {" in bootstrap and "catch {}" in bootstrap
    assert ':root[data-theme="light"]' in page
    assert ':root[data-theme="dark"]' in page
    assert "color-scheme: light" in page and "color-scheme: dark" in page


def test_theme_tokens_cover_nested_surfaces_controls_and_source_evidence() -> None:
    root = Path(__file__).parents[1] / "src/fra_api/templates"
    base = (root / "base.html").read_text()
    source = (root / "source.html").read_text()
    assert "--surface-nested:" in base and "--surface-technical:" in base
    assert "--control-bg:" in base and "--selection-bg:" in base
    assert ".technical-details" in base and "::selection" in base
    assert ".banner.bad" in base and ".summary-notice" in base
    assert "color-scheme: light" in base and "color-scheme: dark" in base
    assert "--color-scheme" not in base
    assert "var(--evidence-outline)" in source and "var(--evidence-fill)" in source
    assert "#d93025" not in source and "rgba(217,48,37,.12)" not in source


def test_notice_colors_use_shared_rules_for_system_and_explicit_themes(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        page = client.get("/").text
    css = page.split("<style>", maxsplit=1)[1].split("</style>", maxsplit=1)[0]
    rules = [
        (selector.strip(), declarations)
        for selector, declarations in re.findall(r"(?m)^\s*([^{}]+)\{([^{}]*)\}", css)
    ]

    def theme_tokens(declarations: str) -> dict[str, str]:
        names = ("banner-background", "banner-foreground", "notice-background", "notice-foreground")
        return {
            name: match.group(1).strip()
            for name in names
            if (match := re.search(rf"--{name}:\s*([^;]+);", declarations))
        }

    system_roots = [declarations for selector, declarations in rules if selector == ":root"]
    explicit_dark = [
        declarations for selector, declarations in rules if selector == ':root[data-theme="dark"]'
    ]
    explicit_light = [
        declarations for selector, declarations in rules if selector == ':root[data-theme="light"]'
    ]
    assert len(system_roots) == 2 and len(explicit_dark) == len(explicit_light) == 1
    assert theme_tokens(system_roots[0]) == theme_tokens(explicit_light[0])
    assert theme_tokens(system_roots[1]) == theme_tokens(explicit_dark[0])

    banner_rules = [
        (selector, declarations) for selector, declarations in rules if ".banner.bad" in selector
    ]
    notice_rules = [
        (selector, declarations)
        for selector, declarations in rules
        if "#analysis-results .summary-notice" in selector
    ]
    assert len(banner_rules) == len(notice_rules) == 1
    assert banner_rules[0][0] == ".banner.bad"
    assert "background: var(--banner-background)" in banner_rules[0][1]
    assert "color: var(--banner-foreground)" in banner_rules[0][1]
    assert notice_rules[0][0] == "#analysis-results .summary-notice"
    assert "background: var(--notice-background)" in notice_rules[0][1]
    assert "color: var(--notice-foreground)" in notice_rules[0][1]


def test_layout_has_scroll_cue_focus_styles_and_consistent_job_language(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client)
        analysis = client.get(f"/analyses/{job_id}/view").text
        job = client.get(f"/jobs/{job_id}/view").text
        upload = client.get("/").text
    assert "Swipe to see more" in analysis
    assert "min-height: 44px" in analysis and ":focus-visible" in analysis
    assert re.search(r"(?m)^\s*code\s*\{[^}]*overflow-wrap:\s*anywhere;", analysis)
    assert re.search(
        r'<div class="scroll-wrap">\s*'
        r'<p class="scroll-cue" id="timeline-scroll-cue">'
        r"Swipe or scroll to see all analysis stage details\.</p>\s*"
        r'<div class="scroll" role="region" tabindex="0" '
        r'aria-label="Analysis stage timeline" '
        r'aria-describedby="timeline-scroll-cue">\s*'
        r'<table id="timeline">',
        job,
    )
    assert "Swipe or scroll to see all analysis stage details." in job
    assert re.search(r"\.job-result-link a\s*\{[^}]*min-height:\s*44px;", job)
    assert "Analysis complete" in job and "Completed" not in job
    assert "Queued" in upload and "Analysis complete" in job


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
    assert '<h2 id="verdict-title">Held for review</h2>' in page
    assert "A financial statement could not be extracted" in page
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
    assert '<h2 id="verdict-title">Passed statement checks</h2>' in informational_page
    assert "low_selectivity" in informational_page
    assert "&lt;script&gt;notice&lt;/script&gt;" in hostile_page
    assert "<script>notice</script>" not in hostile_page


def test_document_without_flags_has_no_document_notices_section(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, flags=[])
        page = client.get(f"/analyses/{job_id}/view").text
    assert '<h2 id="verdict-title">Passed statement checks</h2>' in page
    assert '<details class="document-notices">' not in page


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
    assert (
        "Declined" in page and "This looks like a bank, which this tool does not analyse." in page
    )
    assert "Technical details" in page and "bank" in page.split("Technical details", 1)[1]
    parsed = VisibleText()
    parsed.feed(page)
    assert "declined:bank" not in " ".join(parsed.visible)
    assert "declined:bank: total deposits on page 3" in " ".join(parsed.technical)
    assert "statement_not_extracted:income" in page
    assert 'id="metrics"' not in page
    assert "No summary was produced because this document was declined." in page
    assert "Review the flagged figures and statement checks below" not in page


def test_unknown_decline_code_uses_generic_visible_sentence(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    reason = "declined:unknown_type: score 14.0; evidence: page 2 'phrase'"
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            statements=[],
            reviews=[],
            metrics=[],
            decline=Decline(code="unknown_type", reason=reason),
        )
        page = client.get(f"/analyses/{job_id}/view").text
    parser = VisibleText()
    parser.feed(page)
    visible = " ".join(parser.visible)
    technical = " ".join(parser.technical)
    assert "This document was not analysed." in visible
    assert reason not in visible and "14.0" not in visible and "phrase" not in visible
    assert reason in technical and "unknown_type" in technical
    assert not re.search(r"(?:\w+:|\b\w+_\w+\b)", visible)


def test_arabic_decline_summary_uses_arabic_and_keeps_the_decline_reason(
    tmp_path: Path,
) -> None:
    settings = settings_at(tmp_path)
    reason = "تقارير البنوك غير مدعومة بسبب قواعد التصنيف."
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            statements=[],
            reviews=[],
            metrics=[],
            language="ar",
            decline=Decline(code="unsupported_language", reason=reason),
        )
        page = client.get(f"/analyses/{job_id}/view").text
    summary = page.split('<section id="summary"', 1)[1].split("</section>", 1)[0]
    assert reason in page
    assert 'dir="rtl" lang="ar"' in summary
    assert "لم يُنتج ملخص لأن المستند رُفض." in summary
    assert "Review the flagged figures" not in summary


def test_a_document_hold_remains_visible_with_document_notices(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(settings, client, hold="identity mismatch", flags=["unknown_reason"])
        page = client.get(f"/analyses/{job_id}/view").text
    assert "identity mismatch" in page and "Held for review" in page
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
    clean_statement = statement("a" * 64)
    clean_row = clean_statement.line_items[0]
    clean_statement = clean_statement.model_copy(
        update={
            "line_items": [
                clean_row.model_copy(
                    update={"cells": [clean_row.cells[0].model_copy(update={"flags": []})]}
                )
            ]
        }
    )
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        flagged = finished_job(
            settings,
            client,
            flags=["range_not_converted:5-6"],
            statements=[clean_statement],
            reviews=[review()],
        )
        empty = finished_job(settings, client, statements=[], reviews=[], metrics=[])
        flagged_page = client.get(f"/analyses/{flagged}/view").text
        empty_page = client.get(f"/analyses/{empty}/view").text
    assert "One or more checks are unresolved" in flagged_page
    assert "did not pass their own checks" not in flagged_page
    assert "Figures are not verified" in flagged_page
    assert 'aria-describedby="metric-current_ratio-2025-12-31-review-description"' in flagged_page
    assert 'title="This figure is flagged or belongs to a held statement"' in flagged_page
    assert "⚑ marks items needing review" in flagged_page
    assert "No statement passed review" in empty_page
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
    assert 'id="metric-current_ratio-2025-12-31"' in page
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
    assert f'data-caveat="{caveat.id}"' not in page
    assert "<script>عملة</script>" not in page
    assert "&lt;script&gt;عملة&lt;/script&gt;" in page
    parsed = VisibleText()
    parsed.feed(page)
    assert caveat_id in " ".join(parsed.technical)
    assert "currency: SAR" in " ".join(parsed.technical)
    assert caveat_id not in " ".join(parsed.visible)
    assert ("لم تذكر القائمة" if language == "ar" else "The statement") in page
    assert f'dir="{"rtl" if language == "ar" else "ltr"}" lang="{language}"' in page
    ratio_group = page.split('aria-labelledby="margin-group-title"', 1)[1].split("</article>", 1)[0]
    ratio_row = ratio_group.split('id="metric-margin-2025-12-31"', 1)[1].split("</section>", 1)[0]
    assert "caveat-mark" not in ratio_row
    ratio_detail = ratio_group.split('class="metric-detail"', 1)[1]
    # Inputs remain caveated currency amounts even when the resulting ratio is unaffected.
    assert "caveat-mark" in ratio_detail.split("<h5>Inputs</h5>", 1)[1].split("<h5>Flags", 1)[0]
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    cash_detail = page.split('aria-labelledby="current_ratio-group-title"', 1)[1].split(
        "</article>", 1
    )[0]
    cash_row = cash_detail.split('class="metric-period"', 1)[1].split("</section>", 1)[0]
    review_tooltip = cash_row.split('role="tooltip">', 1)[1].split("</span>", 1)[0]
    assert "held for review" not in review_tooltip
    assert ("لم تذكر القائمة" if language == "ar" else "The statement") in review_tooltip
    headline = page.split('class="metric-grid"', 1)[1].split('id="all-metrics"', 1)[0]
    assert "flag-marker" in headline
    note_anchor = f"metric-current_ratio-2025-12-31-caveat-statement-s1-{caveat_id}"
    assert f'href="#{note_anchor}"' in cash_detail
    assert f'id="{note_anchor}"' in cash_detail


def test_metric_caveats_keep_same_id_notes_from_each_source_statement(tmp_path: Path) -> None:
    caveat_id: CaveatId = "currency_from_domicile"
    sar_statement = statement("a" * 64).model_copy(
        update={
            "caveats": [Caveat(id=caveat_id, evidence={"source": "SAR filing"})],
        }
    )
    usd_statement = statement("a" * 64).model_copy(
        update={
            "id": "s2",
            "currency": "USD",
            "caveats": [Caveat(id=caveat_id, evidence={"source": "USD filing"})],
        }
    )
    computed = metric(MetricUnit.CURRENCY, 1.25)
    sar_input = computed.inputs["numerator"][0]
    usd_input = sar_input.model_copy(update={"statement_id": "s2", "currency": "USD"})
    computed = computed.model_copy(
        update={"inputs": {"numerator": [sar_input, usd_input]}, "caveats": [caveat_id]}
    )
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        job_id = finished_job(
            settings,
            client,
            statements=[sar_statement, usd_statement],
            reviews=[review(), review().model_copy(update={"statement_id": "s2"})],
            metrics=[computed],
        )
        page = client.get(f"/analyses/{job_id}/view").text

    anchor = "metric-current_ratio-2025-12-31-caveat"
    sar_anchor = f"{anchor}-statement-s1-currency_from_domicile"
    usd_anchor = f"{anchor}-statement-s2-currency_from_domicile"
    input_items = page.split("<h5>Inputs</h5>", 1)[1].split("<h5>Flags", 1)[0]
    assert input_items.count(f'href="#{sar_anchor}"') == 1
    assert input_items.count(f'href="#{usd_anchor}"') == 1
    assert input_items.index(f'href="#{sar_anchor}"') < input_items.index(f'href="#{usd_anchor}"')
    assert "SAR filing" in page and "USD filing" in page
    descriptions = re.findall(r'aria-describedby="([^"]+)"', page)
    assert len(descriptions) == len(set(descriptions))
    for description in descriptions:
        assert page.count(f'id="{description}" role="tooltip"') == 1
    for note_anchor in (sar_anchor, usd_anchor):
        labels = re.findall(
            rf'href="#{note_anchor}" aria-label="([^"]+)" aria-describedby="([^"]+)"', page
        )
        assert labels
        for text, description in labels:
            assert f'id="{description}" role="tooltip">{text}</span>' in page
    assert page.count(f'id="{sar_anchor}"') == page.count(f'id="{usd_anchor}"') == 1
    assert f'href="#{sar_anchor}"' in page and f'href="#{usd_anchor}"' in page
    parsed = VisibleText()
    parsed.feed(page)
    technical = " ".join(parsed.technical)
    assert f'id="{sar_anchor}"' in page and f'id="{usd_anchor}"' in page
    assert "currency_from_domicile" in technical
    assert "source: SAR filing" in technical and "source: USD filing" in technical
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))


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
    summary = page.split('<section id="summary"', 1)[1].split("</section>", 1)[0]
    assert summary.count('class="caveat-mark"') == 2
    parser = VisibleText()
    parser.feed(summary)
    assert "The statement does not name its currency." in " ".join(parser.visible)
    assert summary.count('class="caveat-mark"') == 2
    assert summary.count('aria-label="') >= 2
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
        assert (
            "Reports identified as mainly Arabic, banks and insurers are declined with the reason"
            in upload
        )
        assert (
            "Poor OCR can miss Arabic text, and other non-English languages may not be detected"
            in upload
        )
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
    assert "Declined" in page
    parsed = VisibleText()
    parsed.feed(page)
    assert reason not in " ".join(parsed.visible)
    assert "unsupported_language" in " ".join(parsed.technical)
    assert 'id="metrics"' not in page and 'class="statement-detail"' not in page
    assert "1.50×" not in page


def test_button_text_contrasts_with_both_theme_accents(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        page = client.get("/")
    assert page.status_code == 200
    css = page.text
    light_root = re.search(r":root\s*\{([^}]*)\}", css)
    dark_root = re.search(r"prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", css)
    buttons = re.findall(r"(?m)^\s*button\s*\{([^}]*)\}", css)
    assert light_root is not None and dark_root is not None and buttons

    def color(declarations: str, name: str) -> str:
        match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", declarations)
        assert match is not None
        return match.group(1)

    def contrast_ratio(foreground: str, background: str) -> float:
        def luminance(value: str) -> float:
            channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [
                channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                for channel in channels
            ]
            return sum(
                weight * channel
                for weight, channel in zip((0.2126, 0.7152, 0.0722), linear, strict=True)
            )

        lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
        return (lighter + 0.05) / (darker + 0.05)

    button_color = next(
        (
            match
            for declarations in buttons
            if "background: var(--accent)" in declarations
            if (match := re.search(r"color:\s*var\(--([\w-]+)\)", declarations))
        ),
        None,
    )
    assert button_color is not None
    foreground_name = button_color.group(1)
    for theme in (light_root.group(1), dark_root.group(1)):
        assert contrast_ratio(color(theme, foreground_name), color(theme, "accent")) >= 4.5
