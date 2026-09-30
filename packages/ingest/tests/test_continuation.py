"""Statements continued across pages (spec 11, Data flow step 8)."""

from datetime import date

from fra_core.schemas import BBox, LineItem, Period, PeriodKind, StatementType
from fra_ingest.continuation import continues_part, inherit_periods, merge_continuations
from fra_ingest.header import HeaderLayout
from fra_ingest.parts import PartialStatement
from fra_ingest.table_grid import Grid, GridCell

FY25 = Period(key="FY2025", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
FY24 = Period(key="FY2024", end_date=date(2024, 12, 31), kind=PeriodKind.DURATION, months=12)
FY23 = Period(key="FY2023", end_date=date(2023, 12, 31), kind=PeriodKind.DURATION, months=12)
INCOME = StatementType.INCOME


def part(
    page: int,
    periods: list[Period],
    centres: dict[str, float],
    labels: list[str],
    kind: StatementType = INCOME,
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
            LineItem(id=f"p{page}-t0-r{i}", raw_label=label) for i, label in enumerate(labels)
        ],
        table_refs=[f"d#/tables/{page}"],
    )


def test_consecutive_parts_with_matching_columns_merge() -> None:
    merged = merge_continuations(
        [
            part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"]),
            part(160, [FY25, FY24], {"FY2025": 255, "FY2024": 348}, ["Profit"]),
        ]
    )
    assert len(merged) == 1
    assert (merged[0].first_page, merged[0].last_page) == (159, 160)
    assert [i.raw_label for i in merged[0].line_items] == ["Revenue", "Profit"]
    assert merged[0].table_refs == ["d#/tables/159", "d#/tables/160"]


def test_mismatched_periods_columns_types_or_pages_stay_apart() -> None:
    base = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    assert len(merge_continuations([base, part(160, [FY25], {"FY2025": 250}, ["x"])])) == 2
    far = part(160, [FY25, FY24], {"FY2025": 400, "FY2024": 500}, ["x"])
    assert len(merge_continuations([base, far])) == 2
    other = part(160, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"], StatementType.BALANCE)
    assert len(merge_continuations([base, other])) == 2
    later = part(162, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"])
    assert len(merge_continuations([base, later])) == 2


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
