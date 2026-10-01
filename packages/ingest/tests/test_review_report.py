"""Every extracted cell drawn on its page image (spec 12, The review report)."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fra_core.schemas import (
    BBox,
    Caveat,
    Cell,
    CheckResult,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.results import StructureResult, TableDecision
from fra_ingest.review import StatementReview
from fra_ingest.review_report import (
    cell_class,
    render_report,
    resolve_artifacts,
    write_review_report,
)

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
SHA = "ab" * 32


def item(n: int, label: str, value: str | None, *flags: str) -> LineItem:
    box = BBox(left=300, top=100 + 20 * n, right=380, bottom=110 + 20 * n)
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value) if value else None,
        raw_text=value or "",
        provenance=Provenance(page_no=5, bbox=box, table_ref="#/tables/0", row=n, col=2),
        flags=list(flags),
    )
    return LineItem(id=f"p5-t0-r{n}", raw_label=label, cells=[cell])


ITEMS = [
    item(1, 'Inventories <b>&"net"', "10"),
    item(2, "Cash", "5"),
    item(3, "Total current assets", "16", "digit_suspect"),
    item(4, "Other", "7"),
    item(5, "Lost", None, "numbers_missing", "bbox_synthesized"),
]
STATEMENT = Statement(
    id="abababababab-balance-1",
    document_sha256=SHA,
    type=StatementType.BALANCE,
    currency="EGP",
    scale=1,
    periods=[P],
    line_items=ITEMS,
    source_pages=[5],
    flags=["subtotal_failed", "needs_review"],
)
CHECKS = [
    CheckResult(
        id="c1",
        statement_id=STATEMENT.id,
        kind="subtotal",
        period_key=P.key,
        status="fail",
        expected=Decimal(15),
        actual=Decimal(16),
        difference=Decimal(1),
        tolerance=Decimal(1),
        line_item_ids=["p5-t0-r1", "p5-t0-r2", "p5-t0-r3"],
        detail="single_digit:10^0",
    )
]
RESULT = StructureResult(
    version="4",
    sha256=SHA,
    convert_version="1",
    settings_hash="h",
    statements=[STATEMENT],
    tables=[
        TableDecision(
            table_ref="#/tables/0",
            docling_path="docling/p4-9.json",
            page_no=5,
            type=StatementType.BALANCE,
            confidence=0.9,
            statement_id=STATEMENT.id,
            evidence=["title:balance"],
        )
    ],
    reviews=[
        StatementReview(
            statement_id=STATEMENT.id,
            status="needs_review",
            reasons=["subtotal_failed", "digit_suspect:1"],
            numeric_cells=4,
            checked_cells=0,
            flagged_cells=2,
        )
    ],
)


def html() -> str:
    return render_report(RESULT, CHECKS, {5: (600.0, 800.0)}, {5: "pages/5.png"}, "edita")


def test_cell_classes_follow_checks_and_flags() -> None:
    passing = [CHECKS[0].model_copy(update={"status": "pass"})]
    assert cell_class(ITEMS[0].cells[0], ITEMS[0].id, CHECKS) == "failed"
    assert cell_class(ITEMS[0].cells[0], ITEMS[0].id, passing) == "checked"
    assert cell_class(ITEMS[2].cells[0], ITEMS[2].id, passing) == "flagged"
    assert cell_class(ITEMS[3].cells[0], ITEMS[3].id, CHECKS) == "unchecked"
    assert cell_class(ITEMS[4].cells[0], ITEMS[4].id, CHECKS) == "flagged"


def test_every_cell_has_a_box_inside_its_page() -> None:
    boxes = re.findall(
        r'class="box ([^"]*)"[^>]*style="left:([\d.]+)%;top:([\d.]+)%;width:([\d.]+)%;height:([\d.]+)%"',
        html(),
    )
    assert len(boxes) == 5
    for _, left, top, width, height in boxes:
        assert float(left) >= 0 and float(left) + float(width) <= 100
        assert float(top) >= 0 and float(top) + float(height) <= 100
    assert boxes[0][1:3] == ("50.00", "15.00")
    assert "synthesized" in boxes[4][0]


def test_text_from_the_document_is_escaped() -> None:
    page = html()
    assert "Inventories &lt;b&gt;&amp;&quot;net&quot;" in page
    assert "<b>&" not in page


def test_the_image_is_linked_relatively_and_the_review_is_shown() -> None:
    page = html()
    assert '<img src="pages/5.png"' in page
    assert "needs_review" in page and "digit_suspect:1" in page and "single_digit:10^0" in page
    assert "0 of 4" in page


def test_a_page_without_an_image_is_listed_without_its_overlay() -> None:
    page = render_report(RESULT, CHECKS, {5: (600.0, 800.0)}, {}, "edita")
    assert "<img" not in page and "page image missing" in page and "Total current assets" in page


def test_a_unique_prefix_finds_the_artifacts(tmp_path: Path) -> None:
    (tmp_path / SHA).mkdir()
    (tmp_path / ("cd" * 32)).mkdir()
    assert resolve_artifacts(tmp_path, "abab") == tmp_path / SHA
    with pytest.raises(IngestError) as unknown:
        resolve_artifacts(tmp_path, "ee")
    assert unknown.value.reason == "unknown_document"
    (tmp_path / ("ab" * 31 + "cd")).mkdir()
    with pytest.raises(IngestError) as ambiguous:
        resolve_artifacts(tmp_path, "abab")
    assert ambiguous.value.reason == "ambiguous_document"


def test_a_missing_artifact_is_named(tmp_path: Path) -> None:
    (tmp_path / SHA).mkdir()
    with pytest.raises(IngestError) as missing:
        write_review_report(SHA, IngestConfig(artifact_root=tmp_path))
    assert missing.value.reason == "artifact_missing"
    assert "statements.raw.json" in missing.value.detail


def test_the_report_is_written_beside_the_artifacts(tmp_path: Path) -> None:
    out = tmp_path / SHA
    (out / "docling").mkdir(parents=True)
    (out / "statements.raw.json").write_text(RESULT.model_dump_json(), encoding="utf-8")
    (out / "table_checks.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in CHECKS]), encoding="utf-8"
    )
    (out / "convert.json").write_text(
        json.dumps(
            {
                "version": "1",
                "sha256": SHA,
                "locate_version": "2",
                "docling_version": "2",
                "device": "cpu",
                "settings_hash": "h",
                "ranges": [],
                "page_images": {"5": "pages/5.png"},
            }
        ),
        encoding="utf-8",
    )
    (out / "docling" / "p4-9.json").write_text(
        json.dumps(
            {
                "tables": [],
                "texts": [],
                "pages": {"5": {"page_no": 5, "size": {"width": 600, "height": 800}}},
            }
        ),
        encoding="utf-8",
    )
    path = write_review_report(SHA[:8], IngestConfig(artifact_root=tmp_path))
    assert path == out / "review.html" and "Total current assets" in path.read_text(
        encoding="utf-8"
    )


def test_a_caveat_is_shown_with_its_evidence() -> None:
    caveat = Caveat(
        id="scale_assumed_units", evidence={"currency_source": "header", "median_figure": "10"}
    )
    with_caveat = RESULT.model_copy(
        update={"statements": [STATEMENT.model_copy(update={"caveats": [caveat]})]}
    )
    page = render_report(with_caveat, CHECKS, {5: (600.0, 800.0)}, {5: "pages/5.png"}, "edita")
    assert "caveats: scale_assumed_units (currency_source header, median_figure 10)" in page
    assert "caveats:" not in html()
