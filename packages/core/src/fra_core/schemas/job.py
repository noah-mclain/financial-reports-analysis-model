"""Uploads, analysis jobs, their stage events, and the analysis a finished job leaves.

A job moves ``queued`` -> ``running`` (with its current stage) -> ``completed``,
``needs_review`` or ``failed`` (docs/blueprint/02-architecture.md, Job states). The API writes
uploads and queued jobs; the worker runs them and writes everything after.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas.metric import MetricValue
from fra_core.schemas.narrative import Narrative
from fra_core.schemas.review import StatementReview
from fra_core.schemas.statement import Statement

_SHA256 = r"^[0-9a-f]{64}$"
_JOB_ID = r"^[0-9a-f]{32}$"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


FINISHED = frozenset({JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW, JobStatus.FAILED})


class JobStage(StrEnum):
    """The stages a job shows on its timeline, in the order they run."""

    LOCATE = "locate"
    CONVERT = "convert"
    STRUCTURE = "structure"
    METRICS = "metrics"
    NARRATE = "narrate"


class Upload(BaseModel):
    """A PDF as received, identified by its content: the same bytes uploaded twice are one
    upload, under the first file name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=_SHA256)
    filename: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    uploaded_at: datetime


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=_JOB_ID)
    upload_sha256: str = Field(pattern=_SHA256)
    status: JobStatus
    stage: JobStage | None = Field(
        default=None, description="The stage running, or the one that failed; None otherwise"
    )
    error: str | None = Field(default=None, description="Why the job failed, for a person")
    created_at: datetime
    updated_at: datetime


class JobEvent(BaseModel):
    """One step on a job's timeline. ``seq`` orders a job's events from 1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(pattern=_JOB_ID)
    seq: int = Field(ge=1)
    at: datetime
    kind: Literal["queued", "started", "finished", "completed", "needs_review", "failed"]
    stage: JobStage | None = None
    detail: str | None = None


class Decline(BaseModel):
    """Why a document was declined (a bank or an insurer, D7), as the reader sees it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Analysis(BaseModel):
    """What a finished job leaves: the statements with their reviews, the metrics computed
    from them, and why the document was declined or held, when it was."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=_JOB_ID)
    upload_sha256: str = Field(pattern=_SHA256)
    page_count: int = Field(ge=1)
    language: str
    scanned_pages: list[int] | None = Field(
        default=None,
        description="Actual image-page profile; None means unavailable in old analyses",
    )
    statements: list[Statement] = Field(default_factory=list)
    reviews: list[StatementReview] = Field(default_factory=list)
    flags: list[str] = Field(
        default_factory=list,
        description="Document-level locate/structure reasons, in stage order without duplicates",
    )
    decline: Decline | None = None
    hold: str | None = Field(
        default=None, description="Why every statement is held, when one reason holds them all"
    )
    metrics: list[MetricValue] = Field(default_factory=list)
    narrative: Narrative | None = Field(
        default=None, description="None until narration is built; never a placeholder text"
    )

    narrative_suppression: list[str] = Field(default_factory=list)
    narrative_partial: bool = False

    def status(self) -> JobStatus:
        """``completed`` only when there are statements and every one passed review; a
        decline, a hold, an unresolved document flag, a statement held for review or no
        statement at all is ``needs_review``. Only ``low_selectivity`` is informational:
        it describes conversion cost, not incomplete extraction. Unknown flags hold the job
        conservatively until their meaning is explicitly classified here."""
        reviewed = {r.statement_id: r.status for r in self.reviews}
        missing = [s.id for s in self.statements if s.id not in reviewed]
        if missing:
            msg = f"analysis {self.job_id}: statements without a review: {missing}"
            raise ValueError(msg)
        passed = bool(self.statements) and all(reviewed[s.id] == "passed" for s in self.statements)
        document_held = any(flag != "low_selectivity" for flag in self.flags)
        if self.decline is None and self.hold is None and not document_held and passed:
            return JobStatus.COMPLETED
        return JobStatus.NEEDS_REVIEW
