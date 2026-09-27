"""What the locate stage produces. Everything here is serialised to the artifact store."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import PageMode, StatementType, TextSource


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

    def is_candidate(self, statement_type: StatementType, threshold: float = 4.5) -> bool:
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
