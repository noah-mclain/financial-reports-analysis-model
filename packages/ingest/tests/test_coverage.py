"""Which figures no passing check vouches for, and how much of a statement they weigh."""

from decimal import Decimal
from pathlib import Path

import pytest
from test_review import PASSING, ROWS, check, item, statement

from fra_ingest.config import IngestConfig
from fra_ingest.coverage import UncheckedCell, document_unchecked, tally, unchecked_cells
from fra_ingest.errors import IngestError
from fra_ingest.review import review_statement


def test_unchecked_cells_are_exactly_the_numeric_cells_the_review_does_not_count() -> None:
    source = statement(ROWS)
    cells = unchecked_cells(source, PASSING)
    review = review_statement(source, PASSING, primary=True)
    assert len(cells) == review.numeric_cells - review.checked_cells == 1
    only = cells[0]
    assert (only.item_id, only.period_key, only.reported) == ("r4", "2025-12-31", Decimal("7"))


def test_a_failed_or_skipped_check_vouches_for_nothing() -> None:
    failed = [check("subtotal", "fail", ["r1", "r2", "r3"])]
    assert len(unchecked_cells(statement(ROWS), failed)) == 4


def test_weight_is_the_share_of_the_largest_figure_of_the_period() -> None:
    cells = unchecked_cells(statement([item(1, "100"), item(2, "-25")]), [])
    assert [c.weight for c in cells] == [Decimal("1"), Decimal("0.25")]


def test_cells_without_a_number_are_not_reported() -> None:
    assert unchecked_cells(statement([item(1, None), item(2, "4")]), []) == [
        UncheckedCell(
            statement_id="s",
            item_id="r2",
            label="row 2",
            canonical_id=None,
            period_key="2025-12-31",
            reported=Decimal("4"),
            weight=Decimal("1"),
        )
    ]


def test_tally_counts_a_statement_once_however_many_periods_an_item_has() -> None:
    cells = [
        UncheckedCell(
            statement_id="s",
            item_id="r1",
            label="row 1",
            canonical_id=None,
            period_key=period,
            reported=Decimal("1"),
            weight=Decimal("1"),
        )
        for period in ("2025-12-31", "2024-12-31")
    ]
    [row] = tally([cells], key=lambda c: c.label)
    assert (row.statements, row.cells) == (1, 2)


def test_tally_groups_by_item_across_statements_and_orders_by_weight() -> None:
    a = unchecked_cells(statement([item(1, "100"), item(2, "10")]), [])
    b = unchecked_cells(statement([item(1, "100"), item(2, "30")]), [])
    rows = tally([a, b], key=lambda c: c.label)
    assert [(r.key, r.statements, r.cells) for r in rows] == [("row 1", 2, 2), ("row 2", 2, 2)]
    assert rows[0].key == "row 1" and rows[0].mean_weight == Decimal("1")
    assert rows[1].key == "row 2" and rows[1].mean_weight == Decimal("0.2")


def test_an_unreadable_artifact_is_an_ingest_error_not_a_traceback(tmp_path: Path) -> None:
    out = tmp_path / ("ab" * 32)
    out.mkdir()
    (out / "statements.raw.json").write_text("{not json", "utf-8")
    (out / "table_checks.json").write_text("[]", "utf-8")
    config = IngestConfig(artifact_root=tmp_path)
    with pytest.raises(IngestError) as caught:
        document_unchecked("ab", config)
    assert caught.value.reason == "artifact_unreadable"
