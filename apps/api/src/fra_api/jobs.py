"""Upload, queue and follow analysis jobs (docs/blueprint/02-architecture.md, 2.8).

``POST /documents`` takes a multipart ``file`` part or a raw ``application/pdf`` body with
its file name in ``X-Filename``. Both stream into the same bounded, hashing upload writer.
The page limit needs the PDF read: the worker checks it and fails the job.
``GET /jobs/{id}/events`` is a server-sent event stream that ends after the job's last event;
a client that reconnects sends ``Last-Event-ID`` and receives only what it missed, and the
stream of a job already finished closes at once, so a browser's automatic reconnect never
leaves a request waiting.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from concurrent.futures import Future
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from fra_api.upload_worker import UploadWorker
from fra_api.uploads import MULTIPART_TYPE, PDF_TYPE, MultipartUpload, UploadTypeRejected
from fra_core.jobs import JobSettings, JobStore, UploadRejected, UploadTooLarge, UploadWriter
from fra_core.schemas import FINISHED, Analysis, Job, JobEvent, Upload

logger = logging.getLogger(__name__)

_FINAL_EVENTS = frozenset({"completed", "needs_review", "failed"})
# Upload duration is unknown; invite a short retry without holding a server-side waiter.
_UPLOAD_RETRY_SECONDS = 1


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


def add_job_routes(routes: FastAPI) -> None:
    """The upload, job and analysis routes, on the app itself."""

    @routes.post("/documents", status_code=201)
    async def upload(
        request: Request,
        content_type: Annotated[str, Header()] = "",
        x_filename: Annotated[str | None, Header(min_length=1)] = None,
    ) -> Upload:
        media_type = content_type.split(";")[0].strip().lower()
        if media_type not in (PDF_TYPE, MULTIPART_TYPE):
            raise HTTPException(
                status_code=415, detail=f"content type must be {PDF_TYPE} or {MULTIPART_TYPE}"
            )
        if media_type == PDF_TYPE and x_filename is None:
            raise HTTPException(status_code=422, detail="raw PDF requires X-Filename")
        store, settings = store_of(request), settings_of(request)
        slots: BoundedSemaphore = request.app.state.upload_slots
        writer: UploadWriter | None = None

        def acquire() -> UploadWriter:
            nonlocal writer
            writer = store.upload_writer(settings)
            return writer

        def cleanup() -> None:
            if writer is not None:
                writer.__exit__(None, None, None)

        if not slots.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail="concurrent upload limit reached; retry later",
                headers={"Retry-After": str(_UPLOAD_RETRY_SECONDS)},
            )
        worker = UploadWorker(on_exit=slots.release)
        try:
            worker.start()
        except BaseException:
            slots.release()  # No work was queued and no worker owns the slot.
            raise
        try:
            try:
                writer = await asyncio.wrap_future(worker.submit(acquire))
                multipart = (
                    MultipartUpload(writer, content_type, settings)
                    if media_type == MULTIPART_TYPE
                    else None
                )
                async for chunk in request.stream():
                    await asyncio.wrap_future(worker.submit((multipart or writer).write, chunk))
                filename = multipart.filename() if multipart is not None else x_filename
                assert filename is not None  # raw name checked before reading the body
                return await asyncio.wrap_future(worker.submit(writer.finish, filename))
            finally:
                closing = worker.close(cleanup)
                failure = sys.exception()
                pending = asyncio.wrap_future(closing)
                pending.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError as cancelled:
                    original = failure if failure is not None else cancelled

                    def report(done: Future[None]) -> None:
                        try:
                            done.result()
                        except BaseException as cleanup_error:
                            original.add_note(f"upload cleanup failed: {cleanup_error!r}")
                            logger.exception("upload cleanup failed after request cancellation")

                    closing.add_done_callback(report)
                    raise original from None
                except BaseException as cleanup_error:
                    if failure is None:
                        raise
                    failure.add_note(f"upload cleanup failed: {cleanup_error!r}")
                if failure is not None:
                    raise failure
        except UploadTypeRejected as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
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
    async def events(
        request: Request,
        job_id: str,
        last_event_id: Annotated[int, Header(ge=0)] = 0,
    ) -> StreamingResponse:
        store = store_of(request)
        await run_in_threadpool(_job, store, job_id)
        poll = settings_of(request).poll_seconds

        async def stream() -> AsyncIterator[str]:
            # Asynchronous, so a client that goes away is noticed between polls and its stream
            # ends, rather than a thread polling for it until the job finishes.
            after = last_event_id
            while not await request.is_disconnected():
                job = await run_in_threadpool(_job, store, job_id)
                new = await run_in_threadpool(store.events, job_id, after)
                for event in new:
                    yield sse(event)
                    after = event.seq
                    if event.kind in _FINAL_EVENTS:
                        return
                if job.status in FINISHED:  # read before the events, so none is missed
                    return
                await asyncio.sleep(poll)

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
