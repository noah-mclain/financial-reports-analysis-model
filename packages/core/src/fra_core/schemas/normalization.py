"""Amount-free normalization inputs for shadow inspection, separate from source audit."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from fra_core.schemas.statement import StatementType

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

    role: Literal["target", "heading", "parent", "related"]
    line_item_id: str
    source_order: int = Field(ge=0)
    value_rows: tuple[ValueRowReference, ...] = ()
    label_region: Literal["unavailable"] = "unavailable"


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
