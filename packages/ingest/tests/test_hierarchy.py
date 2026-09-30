"""Depth, sections and subtotal cues (spec 11, Data flow step 9)."""

from fra_ingest.hierarchy import RowInput, infer_hierarchy


def row(
    n: int, label: str, indent: float | None, values: bool = True, hint: bool = False
) -> RowInput:
    return RowInput(row=n, label=label, indent=indent, has_values=values, subtotal_hint=hint)


def test_depth_from_indentation_within_tolerance() -> None:
    nodes = infer_hierarchy(
        [
            row(1, "Current assets", 50, values=False),
            row(2, "Inventories", 60),
            row(3, "Cash", 61.5),
            row(4, "Total current assets", 50),
        ]
    )
    assert [n.depth for n in nodes] == [0, 1, 1, 0]


def test_sections_subtotals_and_parents() -> None:
    nodes = infer_hierarchy(
        [
            row(1, "Current assets", 50, values=False),
            row(2, "Inventories", 60),
            row(3, "Total current assets", 50),
            row(4, "إجماليالموجودات", 50),
            row(5, "Gross profit", 50, hint=True),
        ]
    )
    by_row = {n.row: n for n in nodes}
    assert by_row[1].is_section and not by_row[2].is_section
    assert by_row[2].parent_row == 1
    assert by_row[3].is_subtotal and by_row[4].is_subtotal and by_row[5].is_subtotal
    assert not by_row[2].is_subtotal


def test_rows_without_boxes_sit_at_depth_zero() -> None:
    nodes = infer_hierarchy([row(1, "Revenue", None), row(2, "Cost of sales", None)])
    assert [n.depth for n in nodes] == [0, 0]


def test_depth_at_nearest_level() -> None:
    nodes = infer_hierarchy(
        [
            row(1, "A", 50),
            row(2, "B", 52),
            row(3, "C", 55),
        ]
    )
    assert [n.depth for n in nodes] == [0, 0, 1]


def test_a_row_with_values_and_no_label_is_a_subtotal() -> None:
    nodes = infer_hierarchy(
        [
            row(0, "Inventories", 60, True),
            row(1, "Cash", 60, True),
            row(2, "", None, True),
            row(3, "", None, False),
        ]
    )
    assert [n.is_subtotal for n in nodes] == [False, False, True, False]
