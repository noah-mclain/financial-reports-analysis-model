"""Qualified cell-source metadata is additive and cannot carry arbitrary evidence."""

import pytest
from pydantic import ValidationError

from fra_core.schemas import BBox, LabelSource, LineItem, Provenance


def provenance() -> Provenance:
    return Provenance(
        page_no=1,
        bbox=BBox(left=0, top=0, right=20, bottom=10),
        table_ref="#/tables/0",
        row=0,
        col=0,
    )


@pytest.mark.parametrize(
    "status,reasons",
    [
        ("retained", ()),
        ("transformed", ("text_repaired",)),
        ("merged", ("merged_or_wrapped", "note_split")),
    ],
)
def test_available_source_roundtrip_and_frozen(status: str, reasons: tuple[str, ...]) -> None:
    source = LabelSource.model_validate(
        dict(
            status=status,
            reasons=reasons,
            provenance=provenance(),
            docling_path="docling/p1.json",
        )
    )
    item = LineItem(id="heading", raw_label="Heading", label_source=source)
    assert LineItem.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError):
        source.docling_path = "changed"
    assert LineItem.model_validate_json('{"id":"old","raw_label":"Legacy"}').label_source is None


@pytest.mark.parametrize(
    "data",
    [
        {"status": "retained"},
        {"status": "retained", "provenance": provenance(), "docling_path": " "},
        {
            "status": "retained",
            "provenance": provenance(),
            "docling_path": "d",
            "reasons": ("text_repaired",),
        },
        {"status": "transformed", "provenance": provenance(), "docling_path": "d"},
        {"status": "merged", "provenance": provenance(), "docling_path": "d"},
        {"status": "unavailable"},
        {"status": "ambiguous", "reasons": ("missing_bbox",)},
        {"status": "unavailable", "reasons": ("missing_bbox",), "provenance": provenance()},
        {"status": "unavailable", "reasons": ("missing_bbox",), "docling_path": "d"},
        {"status": "unavailable", "reasons": ("SECRET_AMOUNT",)},
        {"status": "unavailable", "reasons": ("missing_bbox",), "raw_text": "SECRET"},
    ],
)
def test_contradictory_or_untyped_metadata_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        LabelSource.model_validate(data)
