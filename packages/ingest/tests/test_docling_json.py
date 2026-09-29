"""The docling JSON fields structure reads (spec 11, Components; R31)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support import artifact_dir

from fra_ingest.docling_json import DlBox, DlDocument, load_docling_json


def test_a_top_left_box_is_kept() -> None:
    box = DlBox.model_validate({"l": 10, "t": 20, "r": 30, "b": 40, "coord_origin": "TOPLEFT"})
    bbox = box.to_bbox(page_height=800)
    assert bbox is not None
    assert (bbox.left, bbox.top, bbox.right, bbox.bottom) == (10, 20, 30, 40)


def test_a_bottom_left_box_is_flipped() -> None:
    box = DlBox.model_validate({"l": 10, "t": 700, "r": 30, "b": 680, "coord_origin": "BOTTOMLEFT"})
    bbox = box.to_bbox(page_height=800)
    assert bbox is not None
    assert (bbox.top, bbox.bottom) == (100, 120)


def test_a_degenerate_box_gives_none() -> None:
    box = DlBox.model_validate({"l": 10, "t": 20, "r": 10, "b": 40, "coord_origin": "TOPLEFT"})
    assert box.to_bbox(page_height=800) is None


def test_a_minimal_document_loads_and_ignores_other_fields() -> None:
    document = DlDocument.model_validate(
        {
            "schema_name": "DoclingDocument",
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {
                                "l": 0,
                                "t": 90,
                                "r": 50,
                                "b": 10,
                                "coord_origin": "BOTTOMLEFT",
                            },
                            "charspan": [0, 0],
                        }
                    ],
                    "data": {
                        "num_rows": 1,
                        "num_cols": 1,
                        "table_cells": [
                            {
                                "text": "7",
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1,
                                "start_col_offset_idx": 0,
                                "end_col_offset_idx": 1,
                                "fillable": False,
                            }
                        ],
                        "grid": [],
                    },
                }
            ],
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "text",
                    "text": "As at 31 December 2025",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {
                                "l": 0,
                                "t": 99,
                                "r": 50,
                                "b": 95,
                                "coord_origin": "BOTTOMLEFT",
                            },
                            "charspan": [0, 22],
                        }
                    ],
                }
            ],
            "pages": {"3": {"page_no": 3, "size": {"width": 600, "height": 100}}},
        }
    )
    assert document.page_size(3).height == 100
    assert [t.text for t in document.texts_on(3)] == ["As at 31 December 2025"]
    assert document.texts_on(4) == []
    cell = document.tables[0].data.table_cells[0]
    assert (cell.text, cell.bbox, cell.column_header) == ("7", None, False)


@pytest.mark.golden
@pytest.mark.parametrize(
    ("name", "range_file", "tables"),
    [("almarai-2025-en-annualreport.pdf", "p155-164.json", 14)],
)
def test_real_docling_output_loads(
    golden: Callable[[str], Path], name: str, range_file: str, tables: int
) -> None:
    path = artifact_dir(golden(name)) / "docling" / range_file
    if not path.exists():
        pytest.skip("run make eval-convert first")
    document = load_docling_json(path)
    assert len(document.tables) == tables
    for table in document.tables:
        page = table.prov[0].page_no
        assert document.page_size(page).height > 0
        boxes = [
            c.bbox.to_bbox(document.page_size(page).height)
            for c in table.data.table_cells
            if c.bbox
        ]
        assert boxes and all(b is not None for b in boxes)


@pytest.mark.golden
def test_scanned_arabic_docling_output_loads(golden: Callable[[str], Path]) -> None:
    path = artifact_dir(golden("juhayna-2025-ar-consolidated.pdf")) / "docling" / "p4-9.json"
    if not path.exists():
        pytest.skip("run make eval-convert first")
    document = load_docling_json(path)
    assert document.tables
    assert any(t.prov[0].page_no == 5 for t in document.tables)
