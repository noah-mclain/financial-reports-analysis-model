"""Amount-free normalization inputs for shadow inspection, separate from source audit."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from fra_core.schemas.statement import LabelSource, StatementType
from fra_core.taxonomy import Taxonomy

MAX_RELATED_LABELS = 6


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("printed label must not be blank")
    return value


PrintedLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=512), AfterValidator(_nonblank)
]


class NormalizationContext(BaseModel):
    """Printed labels with inferred relationships; unavailable/conflicting context is withheld.

    No confirmed state is offered: Statement parent links alone do not establish one.
    Related labels have no inferred categorical meaning (including EPS share kind).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["inferred", "conflicting", "unavailable"] = "unavailable"
    heading: PrintedLabel | None = None
    parent: PrintedLabel | None = None
    related: tuple[PrintedLabel, ...] = Field(default=(), max_length=MAX_RELATED_LABELS)

    @model_validator(mode="after")
    def _state(self) -> NormalizationContext:
        present = self.heading is not None or self.parent is not None or bool(self.related)
        if self.status != "inferred" and present:
            raise ValueError(f"{self.status} context must withhold labels")
        if self.status == "inferred" and self.heading is None and self.parent is None:
            raise ValueError("inferred context needs a printed heading or parent")
        return self


class NormalizationPayload(BaseModel):
    """The complete model-visible boundary: literal labels and statement kind only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    statement: StatementType
    label: PrintedLabel
    context: NormalizationContext = Field(default_factory=NormalizationContext)


class ValueRowReference(BaseModel):
    """Retained value-cell row identity; this is not a label region."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    page_no: int = Field(ge=1)
    table_ref: str
    row: int = Field(ge=0)


class NormalizationSourceRow(BaseModel):
    """Statement order is retained even where headings have no value provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: Literal["target", "heading", "parent", "related", "observed"]
    line_item_id: str
    source_order: int = Field(ge=0)
    value_rows: tuple[ValueRowReference, ...] = ()
    label_region: LabelSource | Literal["unavailable"] = "unavailable"


class NormalizationAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statement_id: str
    rows: tuple[NormalizationSourceRow, ...] = Field(min_length=1)


class NormalizationRequest(BaseModel):
    """Audit is accessible explicitly and excluded from ordinary request serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    payload: NormalizationPayload
    audit: NormalizationAudit = Field(exclude=True)

    def model_payload_json(self) -> str:
        return self.payload.model_dump_json()


class NormalizationAnswer(BaseModel):
    """The one normalization reply schema, shared by the prompt, dataset and scorer."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    canonical_id: str | None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_normalization_answer(
    response: str, statement: StatementType, taxonomy: Taxonomy
) -> tuple[Literal["valid", "invalid"], str | None]:
    """Schema/catalog validity only; a valid catalog ID is not mapping evidence."""
    try:
        raw = json.loads(response, object_pairs_hook=_unique_object)
        answer = NormalizationAnswer.model_validate(raw)
        if answer.canonical_id is not None:
            item = taxonomy.by_id(answer.canonical_id)
            if item is None or item.statement != statement:
                return "invalid", None
        return "valid", answer.canonical_id
    except (ValueError, TypeError):
        return "invalid", None


class NormalizationObservation(BaseModel):
    """Observed caption and retained value-row references; parent linkage is inferred."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_row: NormalizationSourceRow
    printed_label: str
    inferred_parent_id: str | None


class NormalizationDecision(BaseModel):
    """Shadow categorical decision, never numerical verification or primary-source approval.

    Supplied anchor check IDs record inputs, not a claim that each check caused resolution.
    Observations cover every row the mapper inspected; label regions remain unavailable.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )

    state: Literal["supported", "model_abstention", "unresolved", "rejected", "invalid"]
    canonical_id: str | None = None
    reason: str = Field(min_length=1)
    needed_evidence: tuple[str, ...] = ()
    mapper_evidence: str | None = None
    audit: NormalizationAudit
    observations: tuple[NormalizationObservation, ...] = Field(min_length=1)
    supplied_anchor_check_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _accepted_state(self) -> NormalizationDecision:
        if (self.state == "supported") != (self.canonical_id is not None):
            raise ValueError("only a supported decision must have an accepted canonical ID")
        if self.canonical_id is not None and not self.canonical_id.strip():
            raise ValueError("accepted canonical ID must not be blank")
        if self.state == "supported" and self.needed_evidence:
            raise ValueError("supported categorical mapping cannot require more evidence")
        if self.state == "unresolved" and not self.needed_evidence:
            raise ValueError("unresolved decision must name missing evidence")
        return self
