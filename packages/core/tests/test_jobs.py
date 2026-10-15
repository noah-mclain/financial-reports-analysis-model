"""The shared job table: uploads, transitions, events and the settings that place it."""

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fra_core.jobs import (
    JobSettings,
    JobStore,
    UploadRejected,
    UploadTooLarge,
    WorkerAlreadyRunning,
    load_job_settings,
)
from fra_core.paths import REPO_ROOT
from fra_core.schemas import Analysis, JobStage, JobStatus, Statement, StatementReview

PDF = b"%PDF-1.7\n%test\n"
SETTINGS = JobSettings(root=Path("unused"), max_upload_bytes=1000, max_pages=10, poll_seconds=1)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def store_at(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs", clock=Clock())


def analysis_for(job_id: str, sha256: str, review: str | None) -> Analysis:
    statements = []
    reviews = []
    if review is not None:
        statements = [
            Statement.model_validate(
                {
                    "id": "s",
                    "document_sha256": sha256,
                    "type": "balance",
                    "currency": "SAR",
                    "scale": 1,
                    "periods": [{"key": "2025", "end_date": "2025-12-31", "kind": "instant"}],
                }
            )
        ]
        reviews = [
            StatementReview(
                statement_id="s",
                status=review,  # type: ignore[arg-type]
                numeric_cells=0,
                checked_cells=0,
                flagged_cells=0,
            )
        ]
    return Analysis(
        job_id=job_id,
        upload_sha256=sha256,
        page_count=3,
        language="en",
        statements=statements,
        reviews=reviews,
    )


# ---- settings ---------------------------------------------------------------------------------


def test_the_checked_in_settings_load_with_the_root_under_the_repository() -> None:
    settings = load_job_settings(REPO_ROOT / "configs" / "jobs.toml", REPO_ROOT)
    assert settings.root == REPO_ROOT / "var" / "jobs"
    assert settings.max_upload_bytes == 50 * 1024 * 1024 and settings.max_pages == 400


def test_a_missing_or_unknown_setting_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "jobs.toml"
    path.write_text('[storage]\nroot = "x"\n[upload]\nmax_bytes = 1\n[worker]\npoll_seconds = 1\n')
    with pytest.raises(ValueError, match=r"upload\.max_pages is not set"):
        load_job_settings(path, tmp_path)
    path.write_text(
        '[storage]\nroot = "x"\nextra = 1\n[upload]\nmax_bytes = 1\nmax_pages = 1\n'
        "[worker]\npoll_seconds = 1\n"
    )
    with pytest.raises(ValueError, match=r"storage\.extra"):
        load_job_settings(path, tmp_path)


# ---- uploads ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [(b"", "empty"), (b"GIF89a", "not a PDF"), (PDF + b"x" * 1000, "the limit of 1000 bytes")],
)
def test_an_empty_oversized_or_non_pdf_upload_is_refused_and_leaves_nothing(
    tmp_path: Path, data: bytes, message: str
) -> None:
    store = store_at(tmp_path)
    with pytest.raises(UploadRejected, match=message):
        store.add_upload(data, "r.pdf", SETTINGS)
    assert list((tmp_path / "jobs" / "uploads").iterdir()) == []


def test_an_upload_is_checked_chunk_by_chunk_and_refused_at_the_first_bad_chunk(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    with store.upload_writer(SETTINGS) as writer:
        for chunk in (b"%P", b"DF-1.7", b"\n%x\n"):
            writer.write(chunk)
        upload = writer.finish("r.pdf")
    assert store.upload_path(upload.sha256).read_bytes() == b"%PDF-1.7\n%x\n"
    with store.upload_writer(SETTINGS) as writer, pytest.raises(UploadRejected, match="not a"):
        writer.write(b"%P")
        writer.write(b"NG")  # refused here, before any more is read
    with store.upload_writer(SETTINGS) as writer, pytest.raises(UploadTooLarge):
        writer.write(PDF)
        writer.write(b"x" * 1000)
    assert sorted(p.name for p in (tmp_path / "jobs" / "uploads").iterdir()) == [
        f"{upload.sha256}.pdf"
    ]


def test_the_same_bytes_uploaded_twice_are_one_upload_under_the_first_name(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    first = store.add_upload(PDF, "report.pdf", SETTINGS)
    again = store.add_upload(PDF, "renamed.pdf", SETTINGS)
    assert again == first and first.filename == "report.pdf"
    assert store.upload_path(first.sha256).read_bytes() == PDF
    assert store.get_upload(first.sha256) == first
    assert store.get_upload("0" * 64) is None


# ---- job transitions --------------------------------------------------------------------------


def test_a_job_runs_its_stages_and_ends_with_the_status_its_analysis_gives(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    job = store.enqueue(upload.sha256)
    assert job.status is JobStatus.QUEUED
    claimed = store.claim()
    assert claimed is not None and claimed.id == job.id and claimed.status is JobStatus.RUNNING
    store.start_stage(job.id, JobStage.LOCATE)
    assert store.get_job(job.id).stage is JobStage.LOCATE  # type: ignore[union-attr]
    store.finish_stage(job.id, JobStage.LOCATE, "3 pages")
    done = store.finish(analysis_for(job.id, upload.sha256, "passed"))
    assert done.status is JobStatus.COMPLETED and done.stage is None
    kinds = [(e.seq, e.kind, e.stage, e.detail) for e in store.events(job.id)]
    assert kinds == [
        (1, "queued", None, None),
        (2, "started", JobStage.LOCATE, None),
        (3, "finished", JobStage.LOCATE, "3 pages"),
        (4, "completed", None, None),
    ]
    assert [e.seq for e in store.events(job.id, after=2)] == [3, 4]
    assert store.load_analysis(job.id) == analysis_for(job.id, upload.sha256, "passed")


def test_a_held_statement_or_no_statement_ends_in_needs_review(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    for review in ("needs_review", None):
        job = store.enqueue(upload.sha256)
        store.claim()
        assert store.finish(analysis_for(job.id, upload.sha256, review)).status is (
            JobStatus.NEEDS_REVIEW
        )


def test_a_failed_job_keeps_its_stage_and_cannot_fail_or_finish_again(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    job = store.enqueue(upload.sha256)
    store.claim()
    store.start_stage(job.id, JobStage.CONVERT)
    failed = store.fail(job.id, "convert timed out")
    assert failed.status is JobStatus.FAILED and failed.stage is JobStage.CONVERT
    assert failed.error == "convert timed out"
    assert store.events(job.id)[-1].kind == "failed"
    with pytest.raises(ValueError, match="already failed"):
        store.fail(job.id, "again")
    with pytest.raises(ValueError, match="not running"):
        store.finish(analysis_for(job.id, upload.sha256, "passed"))


def test_stages_need_a_running_job_and_an_unknown_upload_cannot_be_queued(
    tmp_path: Path,
) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    job = store.enqueue(upload.sha256)
    with pytest.raises(ValueError, match="is queued, not running"):
        store.start_stage(job.id, JobStage.LOCATE)
    with pytest.raises(KeyError):
        store.enqueue("f" * 64)


def test_jobs_are_claimed_oldest_first_and_none_when_the_queue_is_empty(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    first, second = store.enqueue(upload.sha256), store.enqueue(upload.sha256)
    assert store.claim().id == first.id  # type: ignore[union-attr]
    assert store.claim().id == second.id  # type: ignore[union-attr]
    assert store.claim() is None


def test_two_workers_claiming_at_once_never_take_the_same_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    queued = {store.enqueue(upload.sha256).id for _ in range(20)}
    claimed: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        while (job := JobStore(tmp_path / "jobs").claim()) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(queued)


def test_a_worker_starting_fails_the_jobs_a_stopped_worker_left_running(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    running = store.enqueue(upload.sha256)
    store.claim()
    waiting = store.enqueue(upload.sha256)
    assert store.fail_interrupted() == [running.id]
    assert store.get_job(running.id).status is JobStatus.FAILED  # type: ignore[union-attr]
    assert store.get_job(waiting.id).status is JobStatus.QUEUED  # type: ignore[union-attr]


def test_an_analysis_with_a_statement_but_no_review_is_an_error() -> None:
    sha = "a" * 64
    analysis = analysis_for("b" * 32, sha, "passed").model_copy(update={"reviews": []})
    with pytest.raises(ValueError, match="without a review"):
        analysis.status()


def test_a_decline_or_a_hold_needs_review_even_when_every_statement_passed() -> None:
    sha = "a" * 64
    passed = analysis_for("b" * 32, sha, "passed")
    assert passed.status() is JobStatus.COMPLETED
    assert passed.model_copy(update={"hold": "industry_uncertain"}).status() is (
        JobStatus.NEEDS_REVIEW
    )


def test_a_second_worker_is_refused_while_the_first_holds_the_store(tmp_path: Path) -> None:
    first = JobStore(tmp_path / "jobs")
    second = JobStore(tmp_path / "jobs")
    with (
        first.worker_lock(),
        pytest.raises(WorkerAlreadyRunning, match=r"worker\.lock"),
        second.worker_lock(),
    ):
        pass
    with JobStore(tmp_path / "jobs").worker_lock():  # released when the first stopped
        pass


def test_an_analysis_file_counts_only_for_a_job_recorded_as_finished(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    job = store.enqueue(upload.sha256)
    store.claim()
    # A worker killed after writing the file and before committing the job's end leaves this.
    analysis = analysis_for(job.id, upload.sha256, "passed")
    store.analysis_path(job.id).write_text(analysis.model_dump_json(), encoding="utf-8")
    assert store.load_analysis(job.id) is None  # still running
    store.fail(job.id, "the worker stopped while this job was running")
    assert store.load_analysis(job.id) is None  # failed: the file does not stand
    assert store.load_analysis("f" * 32) is None


def test_a_finished_job_whose_analysis_file_is_missing_is_an_error(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    job = store.enqueue(upload.sha256)
    store.claim()
    store.finish(analysis_for(job.id, upload.sha256, "passed"))
    store.analysis_path(job.id).unlink()
    with pytest.raises(FileNotFoundError, match="is completed but"):
        store.load_analysis(job.id)


def test_document_flags_hold_an_otherwise_passing_analysis_across_storage(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    upload = store.add_upload(PDF, "r.pdf", SETTINGS)
    job = store.enqueue(upload.sha256)
    store.claim()
    analysis = analysis_for(job.id, upload.sha256, "passed").model_copy(
        update={"flags": ["range_not_converted:5-6"]}
    )
    assert store.finish(analysis).status is JobStatus.NEEDS_REVIEW
    restored = store.load_analysis(job.id)
    assert restored is not None and restored.flags == ["range_not_converted:5-6"]
    assert restored.status() is JobStatus.NEEDS_REVIEW
