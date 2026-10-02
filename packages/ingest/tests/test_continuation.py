"""Statements continued across pages (spec 11, Data flow step 8)."""

from datetime import date
from decimal import Decimal

import pytest

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    StatementType,
    TextSource,
)
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.classify import Classification
from fra_ingest.continuation import continues_part, inherit_periods, merge_continuations
from fra_ingest.header import HeaderLayout
from fra_ingest.label_match import CLOSING_TOTAL_ID, LabelIndex
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.table_grid import Grid, GridCell

FY25 = Period(key="FY2025", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
FY24 = Period(key="FY2024", end_date=date(2024, 12, 31), kind=PeriodKind.DURATION, months=12)
FY23 = Period(key="FY2023", end_date=date(2023, 12, 31), kind=PeriodKind.DURATION, months=12)
INCOME = StatementType.INCOME
BALANCE = StatementType.BALANCE
INDEX = LabelIndex(load_taxonomy())


def part(
    page: int,
    periods: list[Period],
    centres: dict[str, float],
    labels: list[str],
    kind: StatementType = INCOME,
    *,
    valued: bool = False,
) -> PartialStatement:
    return PartialStatement(
        type=kind,
        confidence=0.8,
        first_page=page,
        last_page=page,
        page_width=800,
        periods=periods,
        column_centres=centres,
        line_items=[
            LineItem(
                id=f"p{page}-t0-r{i}", raw_label=label, cells=_cells(page, i) if valued else []
            )
            for i, label in enumerate(labels)
        ],
        table_refs=[f"d#/tables/{page}"],
    )


def _cells(page: int, row: int) -> list[Cell]:
    provenance = Provenance(
        page_no=page,
        bbox=BBox(left=0, top=0, right=1, bottom=1),
        table_ref=f"#/tables/{page}",
        row=row,
        col=1,
    )
    return [Cell(period_key="FY2025", reported=Decimal(1), raw_text="1", provenance=provenance)]


def test_consecutive_parts_with_matching_columns_merge() -> None:
    merged = merge_continuations(
        [
            part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"]),
            part(160, [FY25, FY24], {"FY2025": 255, "FY2024": 348}, ["Profit"]),
        ],
        INDEX,
    )
    assert len(merged) == 1
    assert (merged[0].first_page, merged[0].last_page) == (159, 160)
    assert [i.raw_label for i in merged[0].line_items] == ["Revenue", "Profit"]
    assert merged[0].table_refs == ["d#/tables/159", "d#/tables/160"]


def test_mismatched_periods_columns_types_or_pages_stay_apart() -> None:
    base = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    assert len(merge_continuations([base, part(160, [FY25], {"FY2025": 250}, ["x"])], INDEX)) == 2
    far = part(160, [FY25, FY24], {"FY2025": 400, "FY2024": 500}, ["x"])
    assert len(merge_continuations([base, far], INDEX)) == 2
    other = part(160, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"], StatementType.BALANCE)
    assert len(merge_continuations([base, other], INDEX)) == 2
    later = part(162, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"])
    assert len(merge_continuations([base, later], INDEX)) == 2


def _grid(
    page: int, rows: list[list[str]], xs: tuple[float, ...], table: str = "#/tables/4"
) -> Grid:
    cells = tuple(
        GridCell(
            text=t,
            row=r,
            col=c,
            bbox=BBox(left=x - 40, top=20 * r + 5, right=x + 40, bottom=20 * r + 15),
            page_no=page,
        )
        for r, row in enumerate(rows)
        for c, (t, x) in enumerate(zip(row, xs, strict=True))
        if t
    )
    return Grid(
        table_ref=table,
        docling_path="d",
        page_no=page,
        page_width=800,
        num_rows=len(rows),
        num_cols=len(xs),
        cells=cells,
    )


def test_an_unbound_column_inherits_the_previous_pages_period_by_position() -> None:
    grid = _grid(
        160, [["", "31 December 2025", "thousands X"], ["Profit", "10", "9"]], (100, 252, 351)
    )
    layout = HeaderLayout(
        header_rows=[0],
        label_col=0,
        value_cols={1: FY25},
        unbound_cols=[2],
        evidence=["period_unbound:2"],
    )
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    inherited = inherit_periods(layout, grid, previous)
    assert {c: p.key for c, p in inherited.value_cols.items()} == {1: "FY2025", 2: "FY2024"}
    assert inherited.unbound_cols == []
    assert "inherited:2:FY2024" in inherited.evidence


def test_nothing_is_inherited_from_a_distant_column() -> None:
    cells = (
        GridCell(
            text="9", row=1, col=2, bbox=BBox(left=560, top=25, right=640, bottom=35), page_no=160
        ),
    )
    grid = Grid(
        table_ref="#/tables/4",
        docling_path="d",
        page_no=160,
        page_width=800,
        num_rows=2,
        num_cols=3,
        cells=cells,
    )
    layout = HeaderLayout(header_rows=[0], label_col=0, value_cols={}, unbound_cols=[2])
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    assert inherit_periods(layout, grid, previous).unbound_cols == [2]


def _tail(
    page: int, xs: tuple[float, float], periods: tuple[Period, ...]
) -> tuple[HeaderLayout, Grid]:
    grid = _grid(
        page,
        [["", "2025", "2024"], ["Earnings per share", "1.5", "1.2"]],
        (100, *xs),
    )
    layout = HeaderLayout(
        header_rows=[0],
        label_col=0,
        value_cols={i + 1: p for i, p in enumerate(periods)},
    )
    return layout, grid


def test_a_tail_grid_on_the_next_page_with_the_same_periods_continues() -> None:
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    layout, grid = _tail(160, (255, 348), (FY25, FY24))
    assert continues_part(layout, grid, previous)


def test_a_tail_grid_on_the_same_page_as_the_part_end_continues() -> None:
    previous = part(160, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    layout, grid = _tail(160, (255, 348), (FY25, FY24))
    assert continues_part(layout, grid, previous)


def test_a_tail_grid_with_different_period_keys_does_not_continue() -> None:
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    layout, grid = _tail(160, (255, 348), (FY24, FY23))
    assert not continues_part(layout, grid, previous)


def test_a_column_200_points_off_does_not_continue() -> None:
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    layout, grid = _tail(160, (255, 548), (FY25, FY24))
    assert not continues_part(layout, grid, previous)


def test_a_grid_two_pages_later_does_not_continue() -> None:
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    layout, grid = _tail(161, (255, 348), (FY25, FY24))
    assert not continues_part(layout, grid, previous)


def test_a_grid_with_no_bound_columns_does_not_continue() -> None:
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    layout, grid = _tail(160, (255, 348), ())
    assert not continues_part(layout, grid, previous)


def test_an_inherited_column_keeps_position_order_and_the_parts_still_merge() -> None:
    previous = part(159, [FY24, FY25], {"FY2024": 250, "FY2025": 350}, ["Revenue"])
    grid = _grid(160, [["", "", "31 December 2025"], ["Profit", "10", "9"]], (100, 250, 350))
    layout = HeaderLayout(
        header_rows=[0],
        label_col=0,
        value_cols={2: FY25},
        unbound_cols=[1],
        evidence=["period_unbound:1"],
    )
    inherited = inherit_periods(layout, grid, previous)
    assert list(inherited.value_cols) == [1, 2]
    assert [p.key for p in inherited.value_cols.values()] == ["FY2024", "FY2025"]
    tail = build_part(
        grid,
        inherited,
        Classification(type=INCOME, confidence=0.8),
        source=TextSource.TEXT,
    )
    merged = merge_continuations([previous, tail], INDEX)
    assert len(merged) == 1
    assert (merged[0].first_page, merged[0].last_page) == (159, 160)


def test_a_period_with_no_column_centre_in_either_part_stays_apart() -> None:
    base = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    no_centre = part(160, [FY25, FY24], {"FY2025": 250}, ["x"])
    assert len(merge_continuations([base, no_centre], INDEX)) == 2
    base_missing = part(159, [FY25, FY24], {"FY2025": 250}, ["Revenue"])
    full = part(160, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"])
    assert len(merge_continuations([base_missing, full], INDEX)) == 2


def test_a_tail_table_on_the_same_page_merges_in_table_order() -> None:
    first = part(160, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    first = first.model_copy(update={"table_refs": ["d#/tables/3"]})
    tail = part(160, [FY25, FY24], {"FY2025": 252, "FY2024": 349}, ["Earnings per share"])
    tail = tail.model_copy(update={"table_refs": ["d#/tables/12"]})
    merged = merge_continuations([tail, first], INDEX)
    assert len(merged) == 1
    assert [i.raw_label for i in merged[0].line_items] == ["Revenue", "Earnings per share"]
    assert merged[0].table_refs == ["d#/tables/3", "d#/tables/12"]
    assert (merged[0].first_page, merged[0].last_page) == (160, 160)


def test_an_inherited_column_is_no_longer_reported_unbound() -> None:
    grid = _grid(
        160, [["", "31 December 2025", "thousands X"], ["Profit", "10", "9"]], (100, 252, 351)
    )
    layout = HeaderLayout(
        header_rows=[0],
        label_col=0,
        value_cols={1: FY25},
        unbound_cols=[2],
        evidence=["period_unbound:2"],
    )
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    assert "period_unbound:2" not in inherit_periods(layout, grid, previous).evidence


CENTRES: dict[str, float] = {"FY2025": 250, "FY2024": 350}
CLOSING_LABELS = ["Total equity and liabilities", "إجمالي حقوق الملكية والالتزامات"]


def _balance(page: int, labels: list[str], *, valued: bool = True) -> PartialStatement:
    return part(page, [FY25, FY24], CENTRES, labels, BALANCE, valued=valued)


def test_the_labels_used_for_the_closing_total_map_to_it() -> None:
    for label in CLOSING_LABELS:
        match = INDEX.match(label, BALANCE)
        assert match is not None and match.id == CLOSING_TOTAL_ID


@pytest.mark.parametrize("label", CLOSING_LABELS)
def test_a_closed_balance_sheet_is_not_continued_on_the_same_page(label: str) -> None:
    first = _balance(160, ["Total assets", label])
    first = first.model_copy(update={"table_refs": ["d#/tables/3"]})
    second = _balance(160, ["Cash", "Total assets", label])
    second = second.model_copy(update={"table_refs": ["d#/tables/9"]})
    merged = merge_continuations([first, second], INDEX)
    assert [m.table_refs for m in merged] == [["d#/tables/3"], ["d#/tables/9"]]
    assert [len(m.line_items) for m in merged] == [2, 3]


@pytest.mark.parametrize("label", CLOSING_LABELS)
def test_a_closed_balance_sheet_is_not_continued_on_the_next_page(label: str) -> None:
    merged = merge_continuations(
        [_balance(5, ["Total assets", label]), _balance(6, ["Cash", "Total assets", label])],
        INDEX,
    )
    assert [(m.first_page, m.last_page) for m in merged] == [(5, 5), (6, 6)]
    assert [m.table_refs for m in merged] == [["d#/tables/5"], ["d#/tables/6"]]


def test_a_balance_sheet_split_in_the_usual_order_merges_and_is_then_closed() -> None:
    assets = _balance(5, ["Cash", "Total assets"])
    rest = _balance(6, ["Total liabilities", "Total equity and liabilities"])
    third = _balance(7, ["Cash", "Total assets"])
    merged = merge_continuations([assets, rest, third], INDEX)
    assert [(m.first_page, m.last_page) for m in merged] == [(5, 6), (7, 7)]
    assert [i.raw_label for i in merged[0].line_items][-1] == "Total equity and liabilities"


def test_an_income_part_ending_in_a_profit_total_still_merges() -> None:
    first = part(159, [FY25, FY24], CENTRES, ["Revenue", "Net profit for the year"], valued=True)
    second = part(160, [FY25, FY24], CENTRES, ["Other comprehensive income"], valued=True)
    assert len(merge_continuations([first, second], INDEX)) == 1


def test_a_closing_total_followed_by_a_row_without_values_is_still_closed() -> None:
    items = [
        LineItem(id="a", raw_label="Total assets", cells=_cells(5, 0)),
        LineItem(id="b", raw_label=CLOSING_LABELS[0], cells=_cells(5, 1)),
        LineItem(id="c", raw_label="The notes are part of these statements"),
    ]
    closed = _balance(5, []).model_copy(update={"line_items": items})
    assert len(merge_continuations([closed, _balance(6, ["Cash"])], INDEX)) == 2


def test_a_closing_total_followed_by_a_valued_row_is_not_closed() -> None:
    items = [
        LineItem(id="a", raw_label=CLOSING_LABELS[0], cells=_cells(5, 0)),
        LineItem(id="b", raw_label="Cash", cells=_cells(5, 1)),
    ]
    open_part = _balance(5, []).model_copy(update={"line_items": items})
    assert len(merge_continuations([open_part, _balance(6, ["Cash"])], INDEX)) == 1


def test_a_closing_total_label_without_values_does_not_close_the_part() -> None:
    unvalued = _balance(5, ["Cash", CLOSING_LABELS[0]], valued=False)
    assert len(merge_continuations([unvalued, _balance(6, ["Cash"])], INDEX)) == 1


def test_an_unrecognised_closing_total_leaves_the_part_open() -> None:
    damaged = _balance(5, ["Cash", "Total equity and totel liablities"])
    assert len(merge_continuations([damaged, _balance(6, ["Cash"])], INDEX)) == 1
