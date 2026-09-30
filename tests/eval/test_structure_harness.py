"""The structure eval's arithmetic (spec 11, Scoring)."""

from datetime import date
from decimal import Decimal

from harness.structure import metadata_ok, pair_misses, value_rows

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

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def statement(
    values: list[str | None],
    scale: int = 1000,
    currency: str = "SAR",
    flags: list[str] | None = None,
) -> Statement:
    items = [
        LineItem(
            id=f"r{i}",
            raw_label=f"row {i}",
            cells=[]
            if v is None
            else [
                Cell(
                    period_key=P.key,
                    reported=Decimal(v),
                    raw_text=v,
                    provenance=Provenance(
                        page_no=1, bbox=BOX, table_ref="#/tables/0", row=i, col=1
                    ),
                )
            ],
        )
        for i, v in enumerate(values)
    ]
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.BALANCE,
        currency=currency,
        scale=scale,
        periods=[P],
        line_items=items,
        flags=flags or [],
    )


def test_rows_compare_after_scale_and_ignore_rows_without_values() -> None:
    assert value_rows(statement(["10", None, "5"])) == value_rows(
        statement(["5000", "10000"], scale=1)
    )


def test_pair_misses_count_each_side() -> None:
    assert pair_misses(statement(["10", "5"]), statement(["10", "6"])) == (1, 1)
    assert pair_misses(statement(["10", "5"]), statement(["5", "10"])) == (0, 0)


def test_metadata_is_correct_or_flagged() -> None:
    assert metadata_ok(statement(["1"]), 1000, "SAR")
    assert not metadata_ok(statement(["1"], currency="EGP"), 1000, "SAR")
    assert metadata_ok(statement(["1"], currency="XXX", flags=["currency_missing"]), 1000, "SAR")


def test_unconfirmed_manifest_scale_is_not_scored() -> None:
    assert metadata_ok(statement(["1"], scale=1), None, "SAR")
    assert not metadata_ok(statement(["1"], scale=1, currency="EGP"), None, "SAR")
