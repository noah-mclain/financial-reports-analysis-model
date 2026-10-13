"""Upload, queue and follow analysis jobs (docs/blueprint/02-architecture.md, 2.8).

``POST /documents`` takes the PDF as the request body (``Content-Type: application/pdf``) with
its file name in the ``X-Filename`` header, so no multipart parser is needed. The page limit
needs the PDF read, which the API never does: the worker checks it and fails the job.
``GET /jobs/{id}/events`` is a server-sent event stream that ends after the job's last event;
a client that reconnects sends ``Last-Event-ID`` and receives only what it missed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from fra_core.jobs import JobSettings, JobStore, UploadRejected
from fra_core.schemas import Analysis, Job, JobEvent, Upload

PDF_TYPE = "application/pdf"
_FINAL_EVENTS = frozenset({"completed", "needs_review", "failed"})


def store_of(request: Request) -> JobStore:
    store: JobStore = request.app.state.store
    return store


def settings_of(request: Request) -> JobSettings:
    settings: JobSettings = request.app.state.settings
    return settings


def _job(store: JobStore, job_id: str) -> Job:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return job


def sse(event: JobEvent) -> str:
    """One event in the server-sent event format: its seq as the id, its kind as the name."""
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"


async def _read_limited(request: Request, limit: int) -> bytes:
    """The body, refused as soon as it passes ``limit`` bytes, so a large upload is never
    held in memory whole."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"the file is over {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def add_job_routes(routes: FastAPI) -> None:
    """The upload, job and analysis routes, on the app itself."""

    @routes.post("/documents", status_code=201)
    async def upload(
        request: Request,
        content_type: Annotated[str, Header()],
        x_filename: Annotated[str, Header(min_length=1)],
    ) -> Upload:
        if content_type.split(";")[0].strip() != PDF_TYPE:
            raise HTTPException(status_code=415, detail=f"send the PDF as {PDF_TYPE}")
        settings = settings_of(request)
        data = await _read_limited(request, settings.max_upload_bytes)
        try:
            return store_of(request).add_upload(data, x_filename, settings)
        except UploadRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @routes.get("/documents/{sha256}")
    def document(request: Request, sha256: str) -> Upload:
        found = store_of(request).get_upload(sha256)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no document {sha256}")
        return found

    @routes.post("/documents/{sha256}/analyses", status_code=202)
    def analyse(request: Request, sha256: str) -> Job:
        try:
            return store_of(request).enqueue(sha256)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no document {sha256}") from exc

    @routes.get("/jobs/{job_id}")
    def job(request: Request, job_id: str) -> Job:
        return _job(store_of(request), job_id)

    @routes.get("/jobs/{job_id}/events")
    def events(
        request: Request,
        job_id: str,
        last_event_id: Annotated[int, Header(ge=0)] = 0,
    ) -> StreamingResponse:
        store = store_of(request)
        _job(store, job_id)
        poll = settings_of(request).poll_seconds

        def stream() -> Iterator[str]:
            after = last_event_id
            while True:
                new = store.events(job_id, after)
                for event in new:
                    yield sse(event)
                    after = event.seq
                    if event.kind in _FINAL_EVENTS:
                        return
                time.sleep(poll)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @routes.get("/analyses/{job_id}")
    def analysis(request: Request, job_id: str) -> Analysis:
        store = store_of(request)
        found = _job(store, job_id)
        result = store.load_analysis(job_id)
        if result is None:
            raise HTTPException(
                status_code=409, detail=f"job {job_id} is {found.status.value}: no analysis"
            )
        return result
