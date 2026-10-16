"""Whether a statement passed its checks or is held for a person to look at.

Produced by ``fra_ingest.review``; read by the worker and the API, which do not import ingest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StatementReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str
    status: Literal["passed", "needs_review"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    numeric_cells: int = Field(ge=0)
    checked_cells: int = Field(ge=0)
    flagged_cells: int = Field(ge=0)
