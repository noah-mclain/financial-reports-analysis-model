"""What structure writes to statements.raw.json (spec 11, Data model)."""

from datetime import date
from decimal import Decimal

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.results import StructureResult, TableDecision
from fra_ingest.review import StatementReview

SHA = "e" * 64


def test_a_structure_result_round_trips() -> None:
    period = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
    cell = Cell(
        period_key=period.key,
        reported=Decimal("-15177063"),
        raw_text="(15,177,063)",
        provenance=Provenance(
            page_no=159,
            bbox=BBox(left=1, top=2, right=3, bottom=4),
            table_ref="#/tables/4",
            row=3,
            col=2,
        ),
    )
    statement = Statement(
        id="s1",
        document_sha256=SHA,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[period],
        line_items=[LineItem(id="p159-t4-r3", raw_label="Cost of Sales", cells=[cell])],
    )
    result = StructureResult(
        version="1",
        sha256=SHA,
        convert_version="1",
        settings_hash="h",
        statements=[statement],
        tables=[
            TableDecision(
                table_ref="#/tables/3",
                docling_path="docling/p155-164.json",
                page_no=158,
                type=None,
                confidence=0.0,
                statement_id=None,
                evidence=["no_period_header"],
            )
        ],
    )
    again = StructureResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert again.statements[0].line_items[0].cells[0].reported == Decimal("-15177063")


def test_reviews_default_to_none_and_round_trip() -> None:
    bare = StructureResult(version="4", sha256=SHA, convert_version="1", settings_hash="h")
    assert bare.reviews == []
    review = StatementReview(
        statement_id="s1",
        status="needs_review",
        reasons=["identity_failed"],
        numeric_cells=10,
        checked_cells=4,
        flagged_cells=1,
    )
    stored = bare.model_copy(update={"reviews": [review]}).model_dump_json()
    assert StructureResult.model_validate_json(stored).reviews == [review]
