"""Upload, queue, follow and read a job through the API; the worker's side is played by the
job store directly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from api_support import settings_at
from fastapi.testclient import TestClient

from fra_api.app import create_app
from fra_core.jobs import JobSettings, JobStore
from fra_core.profile import Profile
from fra_core.schemas import Analysis, JobStage

PDF = b"%PDF-1.7\n%api test\n"
HEADERS = {"Content-Type": "application/pdf", "X-Filename": "almarai-2025.pdf"}


def client_for(settings: JobSettings) -> TestClient:
    return TestClient(create_app(Profile.NATIVE, settings))


def upload(client: TestClient, data: bytes = PDF, headers: dict[str, str] = HEADERS) -> str:
    response = client.post("/documents", content=data, headers=headers)
    assert response.status_code == 201, response.text
    sha256: str = response.json()["sha256"]
    return sha256


def finish(settings: JobSettings, job_id: str, sha256: str) -> None:
    """What the worker does: claim the job, run one stage, leave an analysis."""
    store = JobStore(settings.root)
    claimed = store.claim()
    assert claimed is not None and claimed.id == job_id
    store.start_stage(job_id, JobStage.LOCATE)
    store.finish_stage(job_id, JobStage.LOCATE, "3 pages")
    store.finish(Analysis(job_id=job_id, upload_sha256=sha256, page_count=3, language="en"))


def test_an_upload_is_stored_once_by_content_and_can_be_read_back(
    tmp_path: Path,
) -> None:
    job_settings = settings_at(tmp_path)
    with client_for(job_settings) as client:
        sha256 = upload(client)
        again = upload(client, headers={**HEADERS, "X-Filename": "other.pdf"})
        assert again == sha256
        body = client.get(f"/documents/{sha256}").json()
        assert body["filename"] == "almarai-2025.pdf" and body["size_bytes"] == len(PDF)
        assert client.get(f"/documents/{'0' * 64}").status_code == 404


def test_an_upload_that_is_not_a_pdf_within_the_limit_is_refused_with_the_reason(
    tmp_path: Path,
) -> None:
    job_settings = settings_at(tmp_path)
    with client_for(job_settings) as client:
        wrong_type = client.post(
            "/documents", content=PDF, headers={**HEADERS, "Content-Type": "image/png"}
        )
        assert wrong_type.status_code == 415
        not_pdf = client.post("/documents", content=b"GIF89a", headers=HEADERS)
        assert not_pdf.status_code == 400 and "not a PDF" in not_pdf.json()["detail"]
        too_big = client.post("/documents", content=PDF + b"x" * 10_000, headers=HEADERS)
        assert too_big.status_code == 413
        no_name = client.post(
            "/documents", content=PDF, headers={"Content-Type": "application/pdf"}
        )
        assert no_name.status_code == 422


def test_a_job_is_queued_for_an_upload_and_its_status_read(tmp_path: Path) -> None:
    job_settings = settings_at(tmp_path)
    with client_for(job_settings) as client:
        sha256 = upload(client)
        queued = client.post(f"/documents/{sha256}/analyses")
        assert queued.status_code == 202
        job = queued.json()
        assert job["status"] == "queued" and job["upload_sha256"] == sha256
        assert client.get(f"/jobs/{job['id']}").json()["status"] == "queued"
        assert client.post(f"/documents/{'0' * 64}/analyses").status_code == 404
        assert client.get(f"/jobs/{'0' * 32}").status_code == 404


def test_the_analysis_is_refused_until_the_job_finishes_then_served(
    tmp_path: Path,
) -> None:
    job_settings = settings_at(tmp_path)
    with client_for(job_settings) as client:
        sha256 = upload(client)
        job_id = client.post(f"/documents/{sha256}/analyses").json()["id"]
        early = client.get(f"/analyses/{job_id}")
        assert early.status_code == 409 and "queued" in early.json()["detail"]
        finish(job_settings, job_id, sha256)
        assert client.get(f"/jobs/{job_id}").json()["status"] == "needs_review"
        analysis = client.get(f"/analyses/{job_id}").json()
        assert analysis["job_id"] == job_id and analysis["narrative"] is None


def test_the_event_stream_sends_every_event_and_ends_after_the_last(
    tmp_path: Path,
) -> None:
    job_settings = settings_at(tmp_path)
    with client_for(job_settings) as client:
        sha256 = upload(client)
        job_id = client.post(f"/documents/{sha256}/analyses").json()["id"]
        finish(job_settings, job_id, sha256)
        response = client.get(f"/jobs/{job_id}/events")
        assert response.headers["content-type"].startswith("text/event-stream")
        blocks = [b for b in response.text.split("\n\n") if b]
        assert [b.splitlines()[1] for b in blocks] == [
            "event: queued",
            "event: started",
            "event: finished",
            "event: needs_review",
        ]
        assert blocks[0].splitlines()[0] == "id: 1"
        resumed = client.get(f"/jobs/{job_id}/events", headers={"Last-Event-ID": "2"})
        assert [b.splitlines()[0] for b in resumed.text.split("\n\n") if b] == ["id: 3", "id: 4"]
        assert client.get(f"/jobs/{'0' * 32}/events").status_code == 404
        # A browser reconnects after the stream ends: nothing is left, and it closes at once.
        assert client.get(f"/jobs/{job_id}/events", headers={"Last-Event-ID": "4"}).text == ""


def test_a_percent_encoded_arabic_file_name_is_stored_decoded(tmp_path: Path) -> None:
    job_settings = settings_at(tmp_path)
    with client_for(job_settings) as client:
        name = "تقرير-المراعي.pdf"
        sha256 = upload(client, headers={**HEADERS, "X-Filename": quote(name)})
        assert client.get(f"/documents/{sha256}").json()["filename"] == name


def test_the_api_never_imports_ingest_or_docling() -> None:
    """docs/blueprint/03-repository-layout.md, 3.2: checked in a fresh interpreter, since this
    test process has imported ingest for other tests."""
    code = (
        "import sys, fra_api.app, fra_api.jobs, fra_api.healthcheck\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'fra_ingest', 'docling', 'torch', 'mlx'})\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_analysis_json_preserves_document_flags(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with client_for(settings) as client:
        sha = upload(client)
        job_id = client.post(f"/documents/{sha}/analyses").json()["id"]
        store = JobStore(settings.root)
        store.claim()
        data = Analysis(job_id=job_id, upload_sha256=sha, page_count=3, language="en").model_dump()
        data["flags"] = ["statement_not_found:income", "range_not_converted:5-6"]
        store.finish(Analysis.model_validate(data))
        response = client.get(f"/analyses/{job_id}")
        assert response.status_code == 200
        assert response.json()["flags"] == data["flags"]
        assert client.get(f"/jobs/{job_id}").json()["status"] == "needs_review"
