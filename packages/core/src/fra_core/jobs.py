"""The job table the API and the worker share: SQLite in WAL mode (ADR 0006), no broker.

The API stores uploads and queues jobs; the worker claims a queued job, records each stage as
it starts and finishes, and ends the job with its analysis or its error. Every transition
checks the state it leaves, so a job cannot be finished twice or failed after it finished.
Uploaded PDFs and analyses are files beside the database, named by sha256 and job id.
"""

from __future__ import annotations

import fcntl
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
    ("upload", "max_multipart_parts"): "max_multipart_parts",
    ("upload", "max_multipart_overhead_bytes"): "max_multipart_overhead_bytes",
    ("upload", "max_concurrent_uploads"): "max_concurrent_uploads",
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
    max_multipart_parts: int = Field(default=32, gt=0)
    max_multipart_overhead_bytes: int = Field(default=64 * 1024, gt=0)
    max_concurrent_uploads: int = Field(default=8, gt=0)
    poll_seconds: float = Field(gt=0)


def load_job_settings(path: Path, repo_root: Path) -> JobSettings:
    """The settings in ``path`` (configs/jobs.toml). Multipart and concurrent-upload limits
    have defaults; other keys are required and unknown keys fail. A relative storage root
    is under ``repo_root``."""
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
            if not JobSettings.model_fields[field].is_required():
                continue
            raise ValueError(f"{path}: {section}.{key} is not set")
        values[field] = data[section][key]
    root = Path(values["root"])
    values["root"] = root if root.is_absolute() else repo_root / root
    return JobSettings.model_validate(values)


class WorkerAlreadyRunning(RuntimeError):
    """Another worker holds this store's worker lock."""


class UploadRejected(ValueError):
    """An upload that is not a PDF within the limits; the message says which limit."""


class UploadTooLarge(UploadRejected):
    """An upload over the size limit, refused as soon as it passes it."""


class UploadWriter:
    """Receives an upload chunk by chunk: hashed and written to a temporary file as it
    arrives, and refused as soon as it passes the size limit or does not start as a PDF, so
    an upload is never held in memory whole. The page limit needs the PDF read, so the
    worker checks it. Use it as a context manager: leaving without ``finish`` removes the
    temporary file."""

    def __init__(self, store: JobStore, settings: JobSettings) -> None:
        self._store = store
        self._limit = settings.max_upload_bytes
        self._tmp = store.root / "uploads" / f".{uuid.uuid4().hex}.tmp"
        self._file = self._tmp.open("wb")
        self._hash = hashlib.sha256()
        self._size = 0
        self._head = b""

    def __enter__(self) -> UploadWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            try:
                self._file.close()
            finally:
                self._tmp.unlink(missing_ok=True)
        except Exception as cleanup_error:
            original = exc[1] if len(exc) > 1 else None
            if not isinstance(original, BaseException):
                raise
            original.add_note(f"upload cleanup failed: {cleanup_error!r}")

    def write(self, chunk: bytes) -> None:
        self._size += len(chunk)
        if self._size > self._limit:
            raise UploadTooLarge(f"the file passes the limit of {self._limit} bytes")
        if len(self._head) < len(PDF_MAGIC):
            self._head += chunk[: len(PDF_MAGIC) - len(self._head)]
            if not PDF_MAGIC.startswith(self._head):
                raise UploadRejected("the file is not a PDF")
        self._hash.update(chunk)
        self._file.write(chunk)

    def finish(self, filename: str) -> Upload:
        """Store the upload. The same bytes again return the first upload, file name
        included."""
        if self._size == 0:
            raise UploadRejected("the file is empty")
        if self._head != PDF_MAGIC:
            raise UploadRejected("the file is not a PDF")
        self._file.close()
        sha256 = self._hash.hexdigest()
        path = self._store.upload_path(sha256)
        if path.is_file():
            self._tmp.unlink()
        else:
            os.replace(self._tmp, path)
        return self._store._record_upload(sha256, filename, self._size)


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

    def upload_writer(self, settings: JobSettings) -> UploadWriter:
        return UploadWriter(self, settings)

    def add_upload(self, data: bytes, filename: str, settings: JobSettings) -> Upload:
        """Store an upload already in memory, checked as ``UploadWriter`` checks it."""
        with self.upload_writer(settings) as writer:
            writer.write(data)
            return writer.finish(filename)

    def _record_upload(self, sha256: str, filename: str, size: int) -> Upload:
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO uploads VALUES (?, ?, ?, ?)",
                (sha256, filename, size, self._clock().isoformat()),
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

    @contextmanager
    def worker_lock(self) -> Iterator[int]:
        """The store's one worker slot, held for as long as the block runs. A second worker
        is refused at once (``WorkerAlreadyRunning``), so a job left running belongs to a
        worker that stopped, never to a live one. Yields the descriptor for deliberate
        inheritance by a conversion child via ``pass_fds``. Close it without an explicit
        unlock: the operating system keeps exclusion until the last inherited descriptor
        closes, even if the parent crashes. The descriptor is valid only inside this block."""
        path = self.root / "worker.lock"
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkerAlreadyRunning(f"another worker holds {path}") from None
            yield handle.fileno()

    def fail_interrupted(self) -> list[str]:
        """Fail every job left running, for a worker starting after one stopped mid-job. Call
        it only inside ``worker_lock``: outside it, a running job may belong to a live worker."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = ?", (JobStatus.RUNNING.value,)
            ).fetchall()
        ids = [r["id"] for r in rows]
        for job_id in ids:
            self.fail(job_id, "the worker stopped while this job was running")
        return ids

    def load_analysis(self, job_id: str) -> Analysis | None:
        """The analysis of a job recorded as completed or needs_review; None otherwise. The file
        is written before the job's end is committed, so a worker killed in between leaves a
        file for a job that later fails: only the recorded status says the analysis stands."""
        job = self.get_job(job_id)
        if job is None or job.status not in (JobStatus.COMPLETED, JobStatus.NEEDS_REVIEW):
            return None
        path = self.analysis_path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"job {job_id} is {job.status.value} but {path} is missing")
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
