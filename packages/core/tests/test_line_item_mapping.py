"""A line item's mapping fields are one state: mapped, flagged, or not yet mapped."""

import pytest
from pydantic import ValidationError

from fra_core.schemas import LineItem, MappingSource


def test_a_row_stored_before_mapping_existed_still_validates() -> None:
    # The shape of a version-10 structure result: none of the mapping fields are present.
    item = LineItem.model_validate({"id": "r1", "raw_label": "Revenue"})
    assert item.canonical_id is None and item.mapping_flag is None


def test_a_mapped_row_carries_an_id_and_a_source() -> None:
    item = LineItem(
        id="r1", raw_label="Revenue", canonical_id="revenue", mapping_source=MappingSource.LEXICON
    )
    assert item.mapping_source is MappingSource.LEXICON


def test_a_flag_and_a_canonical_id_exclude_each_other() -> None:
    with pytest.raises(ValidationError, match=r"r1.*mapping_flag.*canonical_id"):
        LineItem(id="r1", raw_label="Revenue", canonical_id="revenue", mapping_flag="unmapped")


def test_a_source_without_an_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"r1.*mapping_source"):
        LineItem(id="r1", raw_label="Revenue", mapping_source=MappingSource.LEXICON)


def test_a_flagged_row_has_no_source() -> None:
    with pytest.raises(ValidationError, match=r"r1.*mapping_source"):
        LineItem(
            id="r1",
            raw_label="Revenue",
            mapping_flag="ambiguous",
            mapping_source=MappingSource.ANCHOR,
        )
