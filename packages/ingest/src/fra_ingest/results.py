"""What the locate stage produces. Everything here is serialised to the artifact store."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import PageMode, TextSource


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
