"""The job table the API and the worker share: SQLite in WAL mode (ADR 0006), no broker.

The API stores uploads and queues jobs; the worker claims a queued job, records each stage as
it starts and finishes, and ends the job with its analysis or its error. Every transition
checks the state it leaves, so a job cannot be finished twice or failed after it finished.
Uploaded PDFs and analyses are files beside the database, named by sha256 and job id.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tomllib
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import (
    FINISHED,
    Analysis,
    Job,
    JobEvent,
    JobStage,
    JobStatus,
    Upload,
)

PDF_MAGIC = b"%PDF-"
_TOML_FIELDS: dict[tuple[str, str], str] = {
    ("storage", "root"): "root",
    ("upload", "max_bytes"): "max_upload_bytes",
    ("upload", "max_pages"): "max_pages",
    ("worker", "poll_seconds"): "poll_seconds",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    sha256 TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    upload_sha256 TEXT NOT NULL REFERENCES uploads(sha256),
    status TEXT NOT NULL,
    stage TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, created_at);
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    stage TEXT,
    detail TEXT,
    PRIMARY KEY (job_id, seq)
);
"""


class JobSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    max_upload_bytes: int = Field(gt=0)
    max_pages: int = Field(gt=0)
    poll_seconds: float = Field(gt=0)


def load_job_settings(path: Path, repo_root: Path) -> JobSettings:
    """The settings in ``path`` (configs/jobs.toml). Every key is required and no other is
    allowed; a relative storage root is under ``repo_root``."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {section for section, _ in _TOML_FIELDS}
    unknown = sorted(set(data) - known) + sorted(
        f"{section}.{key}"
        for section, table in data.items()
        if section in known
        for key in table
        if (section, key) not in _TOML_FIELDS
    )
    if unknown:
        raise ValueError(f"{path}: unknown settings {unknown}")
    values: dict[str, Any] = {}
    for (section, key), field in _TOML_FIELDS.items():
        if key not in data.get(section, {}):
            raise ValueError(f"{path}: {section}.{key} is not set")
        values[field] = data[section][key]
    root = Path(values["root"])
    values["root"] = root if root.is_absolute() else repo_root / root
    return JobSettings.model_validate(values)


class UploadRejected(ValueError):
    """An upload that is not a PDF within the limits; the message says which limit."""


def check_upload(data: bytes, settings: JobSettings) -> None:
    """Refuse an empty file, one over the size limit, or one that does not start as a PDF.
    The page limit needs the PDF read, so the worker checks it."""
    if not data:
        raise UploadRejected("the file is empty")
    if len(data) > settings.max_upload_bytes:
        raise UploadRejected(
            f"the file is {len(data)} bytes; the limit is {settings.max_upload_bytes}"
        )
    if not data.startswith(PDF_MAGIC):
        raise UploadRejected("the file is not a PDF")


def _now() -> datetime:
    return datetime.now(UTC)


def _write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class JobStore:
    """The job database and its files under ``root``. A connection per call, so the API's
    threads and the worker process can share it."""

    def __init__(self, root: Path, clock: Callable[[], datetime] = _now) -> None:
        self.root = root
        self._clock = clock
        self._db = root / "jobs.sqlite"
        (root / "uploads").mkdir(parents=True, exist_ok=True)
        (root / "analyses").mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db, timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock at once, so two claims never interleave."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ---- files ---------------------------------------------------------------------------

    def upload_path(self, sha256: str) -> Path:
        return self.root / "uploads" / f"{sha256}.pdf"

    def analysis_path(self, job_id: str) -> Path:
        return self.root / "analyses" / f"{job_id}.json"

    # ---- uploads -------------------------------------------------------------------------

    def add_upload(self, data: bytes, filename: str, settings: JobSettings) -> Upload:
        """Store a checked PDF (``check_upload``). The same bytes again return the first
        upload, file name included."""
        check_upload(data, settings)
        sha256 = hashlib.sha256(data).hexdigest()
        path = self.upload_path(sha256)
        if not path.is_file():
            _write_atomically(path, data)
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO uploads VALUES (?, ?, ?, ?)",
                (sha256, filename, len(data), self._clock().isoformat()),
            )
            row = conn.execute("SELECT * FROM uploads WHERE sha256 = ?", (sha256,)).fetchone()
        return Upload.model_validate(dict(row))

    def get_upload(self, sha256: str) -> Upload | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE sha256 = ?", (sha256,)).fetchone()
        return Upload.model_validate(dict(row)) if row else None

    # ---- jobs ----------------------------------------------------------------------------

    def enqueue(self, upload_sha256: str) -> Job:
        if self.get_upload(upload_sha256) is None:
            raise KeyError(f"no upload {upload_sha256}")
        job_id = uuid.uuid4().hex
        now = self._clock().isoformat()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                (job_id, upload_sha256, JobStatus.QUEUED.value, now, now),
            )
            self._event(conn, job_id, "queued", None, None)
        return self._require(job_id)

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate(dict(row)) if row else None

    def events(self, job_id: str, after: int = 0) -> list[JobEvent]:
        """The job's events after ``seq`` ``after``, in order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq",
                (job_id, after),
            ).fetchall()
        return [JobEvent.model_validate(dict(r)) for r in rows]

    def claim(self) -> Job | None:
        """The oldest queued job, now running; None when nothing is queued."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at, id LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            self._set(conn, row["id"], JobStatus.RUNNING, None, None)
        return self._require(row["id"])

    def start_stage(self, job_id: str, stage: JobStage) -> None:
        with self._transaction() as conn:
            self._expect(conn, job_id, JobStatus.RUNNING)
            self._set(conn, job_id, JobStatus.RUNNING, stage, None)
            self._event(conn, job_id, "started", stage, None)

    def finish_stage(self, job_id: str, stage: JobStage, detail: str | None = None) -> None:
        with self._transaction() as conn:
            self._expect(conn, job_id, JobStatus.RUNNING)
            self._set(conn, job_id, JobStatus.RUNNING, None, None)
            self._event(conn, job_id, "finished", stage, detail)

    def finish(self, analysis: Analysis) -> Job:
        """Save the analysis, then end the job as ``analysis.status()`` says."""
        status = analysis.status()
        with self._transaction() as conn:
            self._expect(conn, analysis.job_id, JobStatus.RUNNING)
            _write_atomically(
                self.analysis_path(analysis.job_id), analysis.model_dump_json().encode("utf-8")
            )
            self._set(conn, analysis.job_id, status, None, None)
            self._event(conn, analysis.job_id, status.value, None, None)
        return self._require(analysis.job_id)

    def fail(self, job_id: str, error: str) -> Job:
        """End an unfinished job as failed, keeping the stage it was in."""
        with self._transaction() as conn:
            row = conn.execute("SELECT status, stage FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"no job {job_id}")
            if JobStatus(row["status"]) in FINISHED:
                raise ValueError(f"job {job_id} is already {row['status']}")
            stage = JobStage(row["stage"]) if row["stage"] else None
            self._set(conn, job_id, JobStatus.FAILED, stage, error)
            self._event(conn, job_id, "failed", stage, error)
        return self._require(job_id)

    def fail_interrupted(self) -> list[str]:
        """Fail every job left running, for a worker starting after one stopped mid-job."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = ?", (JobStatus.RUNNING.value,)
            ).fetchall()
        ids = [r["id"] for r in rows]
        for job_id in ids:
            self.fail(job_id, "the worker stopped while this job was running")
        return ids

    def load_analysis(self, job_id: str) -> Analysis | None:
        path = self.analysis_path(job_id)
        if not path.is_file():
            return None
        return Analysis.model_validate_json(path.read_text(encoding="utf-8"))

    # ---- internals -----------------------------------------------------------------------

    def _require(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"no job {job_id}")
        return job

    def _expect(self, conn: sqlite3.Connection, job_id: str, status: JobStatus) -> None:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"no job {job_id}")
        if row["status"] != status.value:
            raise ValueError(f"job {job_id} is {row['status']}, not {status.value}")

    def _set(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        status: JobStatus,
        stage: JobStage | None,
        error: str | None,
    ) -> None:
        conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, error = ?, updated_at = ? WHERE id = ?",
            (
                status.value,
                stage.value if stage else None,
                error,
                self._clock().isoformat(),
                job_id,
            ),
        )

    def _event(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        kind: str,
        stage: JobStage | None,
        detail: str | None,
    ) -> None:
        (seq,) = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO job_events VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                seq,
                self._clock().isoformat(),
                kind,
                stage.value if stage else None,
                detail,
            ),
        )
