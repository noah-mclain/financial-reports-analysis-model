"""Label-cell lineage survives structure without changing numerical or model behavior."""

from collections.abc import Collection
from decimal import Decimal

import pytest
from support import inputs
from test_parts import ROWS, grid
from test_structure import document

from fra_core.schemas import BBox, Statement, StatementType, TextSource
from fra_ingest.classify import Classification
from fra_ingest.column_assignment import split_numeric_columns
from fra_ingest.config import IngestConfig
from fra_ingest.header import parse_header
from fra_ingest.normalization_context import (
    build_normalization_request,
    build_normalization_source_row,
)
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.results import StructureResult
from fra_ingest.row_alignment import realign_rows
from fra_ingest.structure import structure_document
from fra_ingest.table_grid import Grid
from fra_ingest.visual_order import repair_grid


def source_grid() -> Grid:
    return grid(ROWS).model_copy(update={"page_height": 1000, "source_page_unambiguous": True})


def build(g: Grid, merged: Collection[int] = ()) -> PartialStatement:
    return build_part(
        g,
        parse_header(g, StatementType.BALANCE, None),
        Classification(type=StatementType.BALANCE, confidence=0.8),
        source=TextSource.TEXT,
        merged_rows=merged,
    )


def change_label(g: Grid, **updates: object) -> Grid:
    return g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update=updates) if (c.row, c.col) == (2, 0) else c for c in g.cells
            )
        }
    )


def test_heading_and_value_labels_use_their_own_cells() -> None:
    g = source_grid()
    heading, item = build(g).line_items[:2]
    assert heading.cells == []
    for row, entry in [(1, heading), (2, item)]:
        source = entry.label_source
        assert source is not None and source.status == "retained"
        assert source.provenance is not None
        cell = g.cell(row, 0)
        assert cell is not None
        assert source.provenance.bbox == cell.bbox
        assert (source.provenance.row, source.provenance.col) == (row, 0)
        assert source.docling_path == g.docling_path
    assert item.label_source is not None and item.label_source.provenance is not None
    assert item.label_source.provenance.bbox != item.cells[0].provenance.bbox


def test_missing_label_box_never_borrows_an_amount_box() -> None:
    item = build(change_label(source_grid(), bbox=None)).line_items[1]
    assert item.cells[0].provenance.bbox is not None
    assert item.label_source is not None
    assert item.label_source.status == "unavailable"
    assert item.label_source.reasons == ("missing_bbox",)
    assert item.label_source.provenance is None


def test_missing_label_cell_is_explicit_and_heading_audit_has_no_value_rows() -> None:
    g = source_grid()
    missing = g.model_copy(update={"cells": tuple(c for c in g.cells if (c.row, c.col) != (2, 0))})
    item = build(missing).line_items[1]
    assert item.label_source is not None and item.label_source.reasons == ("missing_cell",)
    heading = build(g).line_items[0]
    audit = build_normalization_source_row(heading, 0, "heading")
    assert heading.label_source is not None
    assert audit.label_region == heading.label_source
    assert audit.value_rows == ()


def test_all_covering_candidates_count_even_blank_overlaps() -> None:
    g = source_grid()
    cell = g.cell(2, 0)
    assert cell is not None
    g = g.model_copy(update={"cells": (*g.cells, cell.model_copy(update={"text": ""}))})
    assert g.cell(2, 0) == cell  # existing value/text selection remains first-match
    source = build(g).line_items[1].label_source
    assert source is not None and source.status == "ambiguous"
    assert source.provenance is None and source.reasons == ("ambiguous_cell",)


@pytest.mark.parametrize(
    "updates",
    [
        {"page_height": None},
        {"source_page_unambiguous": False},
        {"docling_path": " "},
    ],
)
def test_legacy_or_ambiguous_grid_withholds_source(updates: dict[str, object]) -> None:
    source = build(source_grid().model_copy(update=updates)).line_items[1].label_source
    assert source is not None and source.status in ("unavailable", "ambiguous")
    assert source.provenance is None and source.docling_path is None


@pytest.mark.parametrize(
    "updates",
    [
        {"bbox": BBox.model_construct(left=float("nan"), top=0, right=20, bottom=10)},
        {"bbox": BBox.model_construct(left=0, top=0, right=float("inf"), bottom=10)},
        {"bbox": BBox.model_construct(left=20, top=0, right=0, bottom=10)},
        {"bbox": BBox(left=-1, top=0, right=20, bottom=10)},
        {"bbox": BBox(left=0, top=0, right=801, bottom=10)},
        {"bbox": BBox(left=0, top=990, right=20, bottom=1001)},
        {"source_row": -1},
        {"source_col": -1},
        {"source_row": 100},
        {"page_no": 157},
    ],
)
def test_invalid_geometry_or_origins_are_withheld(updates: dict[str, object]) -> None:
    source = build(change_label(source_grid(), **updates)).line_items[1].label_source
    assert source is not None and source.status in ("unavailable", "ambiguous")
    assert source.provenance is None


def test_source_zero_offsets_and_moved_amounts_remain_independent() -> None:
    g = change_label(source_grid(), source_row=0, source_col=0)
    g = g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"source_row": 4, "source_col": 3, "flags": ("row_realigned",)})
                if (c.row, c.col) == (2, 2)
                else c
                for c in g.cells
            )
        }
    )
    item = build(g).line_items[1]
    assert item.label_source is not None and item.label_source.provenance is not None
    assert (item.label_source.provenance.row, item.label_source.provenance.col) == (0, 0)
    assert (item.cells[0].provenance.row, item.cells[0].provenance.col) == (4, 3)
    assert item.cells[0].reported == Decimal("26058632")
    split = g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"flags": (*c.flags, "numeric_columns_split")})
                if c.col == 2
                else c
                for c in g.cells
            )
        }
    )
    assert build(split).line_items[1].label_source == item.label_source


def test_actual_value_repairs_preserve_label_sources() -> None:
    from test_column_assignment import merged_grid
    from test_row_alignment import shifted

    g = shifted().model_copy(update={"page_height": 1000, "source_page_unambiguous": True})
    layout = parse_header(g, StatementType.BALANCE, None)
    before = build(g).line_items[-1].label_source
    fixed = realign_rows(g, layout)
    assert fixed.moved
    item = build(fixed.grid).line_items[-1]
    assert item.label_source == before and item.label_source is not None
    assert item.label_source.provenance is not None and item.label_source.provenance.row == 3
    assert item.cells[0].provenance.row == 2

    g = merged_grid().model_copy(update={"page_height": 1000, "source_page_unambiguous": True})
    layout = parse_header(g, StatementType.COMPREHENSIVE_INCOME, None)
    fixed_grid = split_numeric_columns(g, layout)
    assert fixed_grid != g
    classification = Classification(type=StatementType.COMPREHENSIVE_INCOME, confidence=0.8)
    parts = [build_part(v, layout, classification, source=TextSource.OCR) for v in (g, fixed_grid)]
    assert [i.label_source for i in parts[0].line_items] == [
        i.label_source for i in parts[1].line_items
    ]


def test_arabic_repair_and_note_split_keep_original_physical_cell() -> None:
    g = change_label(source_grid(), text="إ ج م ا ل ي ا ل م و ج و د ا ت")
    repaired = repair_grid(g, visual=True)
    item = build(repaired).line_items[1]
    assert item.raw_label != g.text(2, 0)
    assert item.label_source is not None and item.label_source.provenance is not None
    assert item.label_source.status == "transformed"
    assert "text_repaired" in item.label_source.reasons
    original = g.cell(2, 0)
    assert original is not None
    assert item.label_source.provenance.bbox == original.bbox
    note_grid = change_label(source_grid(), text="Inventories (7)")
    note_grid = note_grid.model_copy(
        update={"cells": tuple(c for c in note_grid.cells if (c.row, c.col) != (2, 1))}
    )
    split = build(note_grid).line_items[1]
    assert split.raw_label == "Inventories" and split.note_ref == "(7)"
    assert split.label_source is not None and "note_split" in split.label_source.reasons


@pytest.mark.parametrize("flag", ["words_reversed", "word_order_uncertain"])
def test_uncertain_table_reading_never_claims_retained_text(flag: str) -> None:
    source = build(source_grid().model_copy(update={"flags": (flag,)})).line_items[0].label_source
    assert source is not None and source.status == "transformed"
    assert "reading_uncertain" in source.reasons


def test_spans_keep_only_the_owned_whole_cell() -> None:
    g = change_label(source_grid(), row_span=2)
    g = g.model_copy(update={"cells": tuple(c for c in g.cells if (c.row, c.col) != (3, 0))})
    own, projected = build(g).line_items[1:3]
    assert own.label_source is not None and own.label_source.status == "merged"
    assert own.label_source.provenance is not None
    original = g.cell(2, 0)
    assert original is not None
    assert own.label_source.provenance.bbox == original.bbox
    assert projected.label_source is not None and projected.label_source.status == "ambiguous"
    assert projected.label_source.provenance is None
    wrapped = build(source_grid(), merged=[2]).line_items[1].label_source
    assert wrapped is not None and wrapped.status == "merged"
    valued_span = build(change_label(source_grid(), col_span=4)).line_items[1].label_source
    assert valued_span is not None and valued_span.status == "ambiguous"
    # A heading spanning otherwise empty columns still locates only its entire cell.
    heading_grid = source_grid()
    heading_grid = heading_grid.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"col_span": 4}) if (c.row, c.col) == (1, 0) else c
                for c in heading_grid.cells
            )
        }
    )
    heading = build(heading_grid).line_items[0]
    assert not heading.cells and heading.label_source is not None
    assert heading.label_source.status == "merged"


def test_structure_continuation_roundtrip_and_audit_boundary() -> None:
    doc = document()
    first = doc.model_copy(update={"tables": doc.tables[:1]})
    second = doc.model_copy(update={"tables": doc.tables[1:]})
    given = inputs([("docling/first.json", first), ("docling/second.json", second)])
    result, checks = structure_document(given, IngestConfig())
    assert result.version == "53" and len(result.statements) == 1
    statement = result.statements[0]
    paths = {i.label_source.docling_path for i in statement.line_items if i.label_source}
    assert paths == {"docling/first.json", "docling/second.json"}
    assert {
        i.label_source.provenance.page_no
        for i in statement.line_items
        if i.label_source and i.label_source.provenance
    } == {1, 2}
    assert StructureResult.model_validate_json(result.model_dump_json()) == result
    assert Statement.model_validate_json(statement.model_dump_json()) == statement
    old = statement.model_dump(mode="json")
    for item in old["line_items"]:
        del item["label_source"]
    legacy = Statement.model_validate(old)
    assert all(i.label_source is None for i in legacy.line_items)
    for item in statement.line_items:
        request = build_normalization_request(statement, item.id)
        before = build_normalization_request(legacy, item.id)
        assert request.model_payload_json() == before.model_payload_json()
        assert request.model_dump_json() == before.model_dump_json()
        assert request.audit.rows[0].label_region == item.label_source
        audit = request.audit.model_dump_json()
        assert not any(
            x in audit for x in ("reported", "raw_text", "canonical_id", "mapping_", "note_ref")
        )
        assert "audit" not in request.model_dump_json()
    # Disable only page certainty: this cannot change mappings, hierarchy, reviews or numbers.
    uncertain = [
        (
            p,
            d.model_copy(
                update={
                    "tables": [
                        t.model_copy(update={"prov": [*t.prov, t.prov[0]]}) for t in d.tables
                    ]
                }
            ),
        )
        for p, d in given.documents
    ]
    other, other_checks = structure_document(
        given.model_copy(update={"documents": uncertain}), IngestConfig()
    )

    def without_sources(r: StructureResult) -> dict[str, object]:
        return r.model_dump(
            exclude={"statements": {"__all__": {"line_items": {"__all__": {"label_source"}}}}}
        )

    assert without_sources(other) == without_sources(result)
    assert other_checks == checks
