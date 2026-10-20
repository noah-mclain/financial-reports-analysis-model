"""The shadow request exposes printed categorical inputs, never audit data."""

import pytest
from pydantic import ValidationError

from fra_core.schemas.normalization import (
    MAX_RELATED_LABELS,
    NormalizationAudit,
    NormalizationContext,
    NormalizationPayload,
    NormalizationRequest,
    NormalizationSourceRow,
)
from fra_core.schemas.statement import StatementType


def test_payload_is_strict_and_preserves_printed_text() -> None:
    payload = NormalizationPayload(statement=StatementType.BALANCE, label="  Lease liabilities  ")
    assert payload.label == "  Lease liabilities  "
    assert payload.context.status == "unavailable"
    for extra in ("canonical_id", "cells", "sec_tag", "audit", "sign"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            NormalizationPayload.model_validate(
                {"statement": StatementType.BALANCE, "label": "Lease liabilities", extra: "secret"}
            )
    with pytest.raises(ValidationError):
        NormalizationPayload(statement=StatementType.BALANCE, label=123)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        payload.label = "changed"


def test_context_bounds_and_states_are_enforced() -> None:
    for label in ("", "  ", "x" * 513):
        with pytest.raises(ValidationError):
            NormalizationPayload(statement=StatementType.INCOME, label=label)
    with pytest.raises(ValidationError):
        NormalizationContext(status="unavailable", heading="Current liabilities")
    with pytest.raises(ValidationError):
        NormalizationContext(status="conflicting", parent="Total liabilities")
    with pytest.raises(ValidationError):
        NormalizationContext(status="inferred")
    with pytest.raises(ValidationError):
        NormalizationContext(
            status="inferred",
            heading="Current liabilities",
            related=("Loans",) * (MAX_RELATED_LABELS + 1),
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        NormalizationContext.model_validate({"kind": "basic"})


def test_payload_round_trip_is_deterministic() -> None:
    payload = NormalizationPayload(
        statement=StatementType.INCOME,
        label="ربحية السهم",
        context=NormalizationContext(
            status="inferred", heading="ربحية السهم", related=("أساسي", "مخفض")
        ),
    )
    encoded = payload.model_dump_json()
    assert NormalizationPayload.model_validate_json(encoded).model_dump_json() == encoded


def test_request_and_nested_boundary_reject_extras_and_exclude_audit() -> None:
    request = NormalizationRequest(
        payload=NormalizationPayload(statement=StatementType.BALANCE, label="Lease liabilities"),
        audit=NormalizationAudit(
            document_sha256="a" * 64,
            statement_id="s",
            rows=(NormalizationSourceRow(role="target", line_item_id="r1", source_order=0),),
        ),
    )
    assert request.model_dump(include={"audit"}) == {}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        NormalizationRequest.model_validate(
            {"payload": request.payload, "audit": request.audit, "canonical_id": "answer"}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        NormalizationPayload.model_validate(
            {
                "statement": StatementType.BALANCE,
                "label": "Lease liabilities",
                "context": {"canonical_id": "answer"},
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        NormalizationSourceRow.model_validate(
            {"role": "heading", "line_item_id": "h", "source_order": 0, "bbox": [1, 2, 3, 4]}
        )
