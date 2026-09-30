"""What the locate and convert stages produce. Everything here is serialised to the artifact
store."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas import Document, PageMode, Statement, StatementType, TextSource

# A page is a candidate for a type when it names the type and scores at least this much.
CANDIDATE_THRESHOLD = 4.5


class PageText(BaseModel):
    """The text of one page, split into the header region and the rest."""

    model_config = ConfigDict(extra="forbid")

    page_no: int = Field(ge=1)
    mode: PageMode
    source: TextSource | None = Field(
        description="None when an image page could not be read because no OCR engine ran"
    )
    header_text: str = ""
    body_text: str = ""
    char_count: int = Field(ge=0, description="Non-whitespace characters in the text layer")
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)
    arabic_chars: int = Field(default=0, ge=0)
    latin_chars: int = Field(default=0, ge=0)
    visual_arabic: bool = Field(
        default=False,
        description="Arabic words extracted with their letters reversed (Almarai AR)",
    )
    ocr_language: str | None = Field(
        default=None, description="The OCR language whose read was kept, for image pages"
    )
    ocr_seconds: float = Field(default=0.0, ge=0.0)
    flags: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.header_text}\n{self.body_text}"


class PageScore(BaseModel):
    """Evidence that a page holds each statement type, kept for review and tuning."""

    model_config = ConfigDict(extra="forbid")

    page_no: int = Field(ge=1)
    type_scores: dict[StatementType, float]
    title_types: list[StatementType] = Field(default_factory=list)
    cue_types: list[StatementType] = Field(
        default_factory=list, description="Only filled when the header carries no title"
    )
    title_hits: list[str] = Field(default_factory=list)
    numeric_tokens: int = Field(default=0, ge=0)
    structure: list[str] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)
    continuation: bool = False

    def is_candidate(
        self, statement_type: StatementType, threshold: float = CANDIDATE_THRESHOLD
    ) -> bool:
        named = statement_type in self.title_types or statement_type in self.cue_types
        return named and self.type_scores.get(statement_type, 0.0) >= threshold


class StatementRange(BaseModel):
    """Consecutive pages holding one statement, before padding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: StatementType
    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    score: float
    rank: int = Field(ge=1, description="1 is the best candidate for this type")

    def padded(self, pad: int, page_count: int) -> tuple[int, int]:
        return max(1, self.first_page - pad), min(page_count, self.last_page + pad)


IndustryKind = Literal["corporate", "bank", "insurer", "other_financial", "unknown"]
IndustrySubkind = Literal[
    "investment_holding",
    "brokerage",
    "exchange_operator",
    "consumer_finance",
    "asset_manager",
    "other",
]


class IndustrySignal(BaseModel):
    """What kind of company issued the document. Stored now; the decline rule is week 2."""

    model_config = ConfigDict(extra="forbid")

    kind: IndustryKind
    subkind: IndustrySubkind | None = Field(
        default=None, description="Set only when kind is other_financial"
    )
    score: float = 0.0
    evidence: list[tuple[int, str]] = Field(default_factory=list)


class LocateResult(BaseModel):
    """Output of the locate stage, written to ``<artifact root>/<sha256>/locate.json``."""

    model_config = ConfigDict(extra="forbid")

    version: str
    document: Document
    pages: list[PageScore]
    ranges: list[StatementRange]
    convert_ranges: list[tuple[int, int]] = Field(
        description="Padded, merged ranges of the enabled types: the pages docling converts"
    )
    industry: IndustrySignal
    flags: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

    @property
    def candidate_share(self) -> float:
        pages = sum(last - first + 1 for first, last in self.convert_ranges)
        return pages / self.document.page_count


RangeOcr = Literal["pdf_aware", "full_page", "skipped"]
RangeStatus = Literal["ok", "partial", "failed", "skipped"]


class RangePlan(BaseModel):
    """How docling reads one range of ``convert_ranges``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    ocr: RangeOcr
    ocr_language: str | None = Field(
        description="One language: Vision reads only the first it is given (R21). None when "
        "the range is skipped or no OCR engine is configured"
    )
    image_pages: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> RangePlan:
        if self.last_page < self.first_page:
            msg = f"range {self.first_page}-{self.last_page} ends before it starts"
            raise ValueError(msg)
        return self

    @property
    def label(self) -> str:
        return f"{self.first_page}-{self.last_page}"


class RangeConversion(BaseModel):
    """What happened to one range."""

    model_config = ConfigDict(extra="forbid")

    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    ocr: RangeOcr
    ocr_language: str | None
    docling_path: str | None = Field(
        default=None, description="Relative to the artifact directory; None when nothing was saved"
    )
    tables: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.0, ge=0.0)
    status: RangeStatus
    flags: list[str] = Field(default_factory=list)


class ConvertResult(BaseModel):
    """Output of the convert stage, written to ``<artifact root>/<sha256>/convert.json``."""

    model_config = ConfigDict(extra="forbid")

    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locate_version: str
    docling_version: str
    device: str
    settings_hash: str
    ranges: list[RangeConversion]
    page_images: dict[int, str] = Field(default_factory=dict)
    peak_footprint_gb: float | None = None
    flags: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

    @property
    def all_failed(self) -> bool:
        attempted = [r for r in self.ranges if r.status != "skipped"]
        return bool(attempted) and all(r.status == "failed" for r in attempted)


class TableDecision(BaseModel):
    """What structure decided about one docling table, and why."""

    model_config = ConfigDict(extra="forbid")

    table_ref: str
    docling_path: str
    page_no: int = Field(ge=1)
    type: StatementType | None
    confidence: float = Field(ge=0.0, le=1.0)
    statement_id: str | None = None
    evidence: list[str] = Field(default_factory=list)


class StructureResult(BaseModel):
    """Output of the structure stage.

    Written to ``<artifact root>/<sha256>/statements.raw.json``.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    convert_version: str
    settings_hash: str
    statements: list[Statement] = Field(default_factory=list)
    tables: list[TableDecision] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
