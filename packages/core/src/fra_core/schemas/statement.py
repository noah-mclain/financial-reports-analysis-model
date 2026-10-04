"""A financial statement as extracted from a document, with provenance on every value.

Values are held as :class:`~decimal.Decimal` exactly as printed, before the statement scale is
applied. Pydantic serialises Decimal to a JSON string, so no precision is lost on the way to
disk. Analytics converts to float only when computing ratios; identity checks stay exact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas.caveat import Caveat

_ALLOWED_SCALES = (1, 1_000, 1_000_000, 1_000_000_000)


class StatementType(StrEnum):
    INCOME = "income"
    COMPREHENSIVE_INCOME = "comprehensive_income"
    BALANCE = "balance"
    CASH_FLOW = "cash_flow"
    EQUITY = "equity"


class Framework(StrEnum):
    """The reporting framework the statements were prepared under.

    The golden set contains both: Edita publishes FY2025 under each, and the two differ by
    design, so framework must be recorded rather than assumed.
    """

    IFRS = "IFRS"
    EAS = "EAS"
    UNKNOWN = "unknown"


class PeriodKind(StrEnum):
    DURATION = "duration"
    """A flow measured over time, such as revenue."""

    INSTANT = "instant"
    """A balance at a point in time, such as total assets."""


class MappingSource(StrEnum):
    LEXICON = "lexicon"
    ANCHOR = "anchor"
    EMBEDDING = "embedding"
    MODEL = "model"
    USER = "user"


class TextSource(StrEnum):
    TEXT = "text"
    OCR = "ocr"


class BBox(BaseModel):
    """A rectangle in PDF points with a top-left origin.

    docling reports boxes in either origin; they are converted on the way in with
    ``BoundingBox.to_top_left_origin(page_height)`` so that everything downstream, including
    the UI overlay, shares one convention.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: float
    top: float
    right: float
    bottom: float

    @model_validator(mode="after")
    def _ordered(self) -> BBox:
        if self.right <= self.left or self.bottom <= self.top:
            msg = f"bbox is not ordered top-left to bottom-right: {self!r}"
            raise ValueError(msg)
        return self

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top


class Provenance(BaseModel):
    """Where a value was read from. Every displayed number resolves to one of these."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_no: int = Field(ge=1)
    bbox: BBox
    table_ref: str = Field(description="docling self_ref, for example '#/tables/3'")
    row: int = Field(ge=0, description="TableCell.start_row_offset_idx")
    col: int = Field(ge=0, description="TableCell.start_col_offset_idx")
    source: TextSource = TextSource.TEXT


class Period(BaseModel):
    """One column of a statement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(description="Stable identifier, for example 'FY2025' or '2025-12-31'")
    end_date: date
    kind: PeriodKind
    months: int | None = Field(default=None, ge=1, le=36)
    restated: bool = False
    audited: bool | None = None

    @model_validator(mode="after")
    def _months_match_kind(self) -> Period:
        if self.kind is PeriodKind.DURATION and self.months is None:
            msg = f"duration period {self.key!r} needs a length in months"
            raise ValueError(msg)
        if self.kind is PeriodKind.INSTANT and self.months is not None:
            msg = f"instant period {self.key!r} cannot have a length"
            raise ValueError(msg)
        return self

    @property
    def is_annual(self) -> bool:
        return self.kind is PeriodKind.DURATION and self.months == 12


class Cell(BaseModel):
    """One value in one period, as printed."""

    model_config = ConfigDict(extra="forbid")

    period_key: str
    reported: Decimal | None = Field(
        default=None,
        description="As printed, sign applied, before the statement scale. None means the "
        "line does not apply to this period, which differs from a printed zero.",
    )
    raw_text: str
    provenance: Provenance
    flags: list[str] = Field(default_factory=list)


class LineItem(BaseModel):
    """One row of a statement, with its label as printed and its canonical mapping."""

    model_config = ConfigDict(extra="forbid")

    id: str
    raw_label: str
    canonical_id: str | None = None
    mapping_source: MappingSource | None = None
    mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_flag: Literal["unmapped", "ambiguous"] | None = Field(
        default=None,
        description="Set on a row with values that was not mapped: unmapped when nothing "
        "resolves it, ambiguous when more than one item or an anchor disagrees. Never a guess.",
    )
    mapping_evidence: str | None = Field(
        default=None,
        description="How the mapping was made, or why the row is flagged, for a reviewer",
    )
    depth: int = Field(default=0, ge=0, description="Indentation level within the statement")
    is_subtotal: bool = False
    parent_id: str | None = None
    note_ref: str | None = None
    cells: list[Cell] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_mapping_state(self) -> LineItem:
        if self.mapping_flag is not None and self.canonical_id is not None:
            msg = (
                f"line item {self.id!r}: mapping_flag {self.mapping_flag!r} and "
                f"canonical_id {self.canonical_id!r} exclude each other"
            )
            raise ValueError(msg)
        if self.mapping_source is not None and self.canonical_id is None:
            msg = f"line item {self.id!r}: mapping_source is set without a canonical_id"
            raise ValueError(msg)
        return self

    def value_for(self, period_key: str) -> Decimal | None:
        for cell in self.cells:
            if cell.period_key == period_key:
                return cell.reported
        return None


class Statement(BaseModel):
    """One primary statement, extracted from one document."""

    model_config = ConfigDict(extra="forbid")

    id: str
    document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    type: StatementType
    entity_name: str | None = None
    consolidated: bool | None = None
    framework: Framework = Framework.UNKNOWN
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217")
    scale: int = Field(description="Multiplier applied to every reported value")
    language: str = Field(default="en")
    periods: list[Period] = Field(min_length=1)
    line_items: list[LineItem] = Field(default_factory=list)
    source_pages: list[int] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    caveats: list[Caveat] = Field(
        default_factory=list,
        description="Assumptions about unit or currency that every amount rests on",
    )

    @model_validator(mode="after")
    def _check_scale_and_periods(self) -> Statement:
        if self.scale not in _ALLOWED_SCALES:
            msg = f"scale {self.scale} is not one of {_ALLOWED_SCALES}"
            raise ValueError(msg)
        keys = [p.key for p in self.periods]
        if len(keys) != len(set(keys)):
            msg = f"duplicate period keys in statement {self.id!r}: {keys}"
            raise ValueError(msg)
        known = set(keys)
        for item in self.line_items:
            for cell in item.cells:
                if cell.period_key not in known:
                    msg = (
                        f"line item {item.id!r} references unknown period "
                        f"{cell.period_key!r}; statement has {sorted(known)}"
                    )
                    raise ValueError(msg)
        return self

    def find(self, canonical_id: str) -> LineItem | None:
        """Return the line item mapped to ``canonical_id``, if there is exactly one."""
        matches = [i for i in self.line_items if i.canonical_id == canonical_id]
        return matches[0] if len(matches) == 1 else None
