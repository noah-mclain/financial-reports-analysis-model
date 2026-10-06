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


def finding_statement_data() -> dict[str, object]:
    return {
        "id": "s",
        "document_sha256": "a" * 64,
        "type": "income",
        "currency": "SAR",
        "scale": 1,
        "periods": [
            {"key": "FY2025", "end_date": "2025-12-31", "kind": "duration", "months": 12},
            {"key": "FY2024", "end_date": "2024-12-31", "kind": "duration", "months": 12},
        ],
    }


def test_mapping_findings_roundtrip_and_legacy_default() -> None:
    from fra_core.schemas import MappingFinding, Statement

    legacy = Statement.model_validate(finding_statement_data())
    assert legacy.mapping_findings == ()
    finding = MappingFinding(item_id="revenue", observed_period_keys=("FY2025", "FY2024"))
    data = {**finding_statement_data(), "mapping_findings": [finding]}
    result = Statement.model_validate(data)
    assert Statement.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError, match="frozen"):
        finding.item_id = "net_income"


@pytest.mark.parametrize(
    "keys", [(), ("",), (" ",), ("FY2025", "FY2025"), ("foreign",), ("FY2025",)]
)
def test_invalid_or_incomplete_finding_periods_are_rejected(keys: tuple[str, ...]) -> None:
    from fra_core.schemas import Statement

    with pytest.raises(ValidationError, match="period"):
        Statement.model_validate(
            {
                **finding_statement_data(),
                "mapping_findings": [
                    {"item_id": "revenue", "observed_period_keys": keys},
                ],
            }
        )


@pytest.mark.parametrize("item_id", ["", " "])
def test_a_finding_needs_a_named_item(item_id: str) -> None:
    from fra_core.schemas import MappingFinding

    with pytest.raises(ValidationError, match="item_id"):
        MappingFinding(item_id=item_id, observed_period_keys=("FY2025",))


def test_duplicate_or_stale_findings_are_rejected() -> None:
    from fra_core.schemas import Statement

    finding = {"item_id": "revenue", "observed_period_keys": ["FY2025", "FY2024"]}
    with pytest.raises(ValidationError, match=r"duplicate.*finding"):
        Statement.model_validate(
            {**finding_statement_data(), "mapping_findings": [finding, finding]}
        )
    with pytest.raises(ValidationError, match=r"mapped.*revenue"):
        Statement.model_validate(
            {
                **finding_statement_data(),
                "mapping_findings": [finding],
                "line_items": [{"id": "r", "raw_label": "Revenue", "canonical_id": "revenue"}],
            }
        )


def test_generic_or_extra_finding_fields_are_rejected() -> None:
    from fra_core.schemas import MappingFinding

    with pytest.raises(ValidationError):
        MappingFinding.model_validate(
            {"item_id": "revenue", "observed_period_keys": ["FY2025"], "reason": "needs_review"}
        )
    with pytest.raises(ValidationError):
        MappingFinding.model_validate(
            {"item_id": "revenue", "observed_period_keys": ["FY2025"], "bbox": [1, 2, 3, 4]}
        )
