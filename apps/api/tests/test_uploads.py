"""Raw and multipart uploads share streamed validation and content-addressed storage."""

import asyncio
import threading
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from threading import (
    Barrier,
    Event,
    Lock,
    Thread,
    current_thread,
    get_ident,
)
from unittest.mock import Mock

import anyio
import httpx
import pytest
from api_support import settings_at
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from fra_api.app import create_app
from fra_api.uploads import MultipartUpload
from fra_core.jobs import JobSettings, JobStore, UploadWriter
from fra_core.profile import Profile
from fra_core.schemas import Upload

PDF = b"%PDF-1.7\n%upload test\n"
MULTIPART = {"Content-Type": 'multipart/form-data; boundary="boundary"'}


def part(data: bytes, name: str = "file", content_type: str = "application/pdf") -> bytes:
    return (
        (
            f'--boundary\r\nContent-Disposition: form-data; name="{name}"; filename="r.pdf"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )


END = b"--boundary--\r\n"
RAW_HEADERS = {"Content-Type": "application/pdf", "X-Filename": "r.pdf"}


@pytest.mark.parametrize(("limit", "requests"), [(8, 9), (3, 9)])
def test_upload_limit_rejects_excess_and_bounds_live_worker_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int, requests: int
) -> None:
    settings = settings_at(tmp_path)
    if limit != 8:
        settings = settings.model_copy(update={"max_concurrent_uploads": limit})
    workers: set[Thread] = set()
    lock = Lock()
    acquire = JobStore.upload_writer

    def record_worker(self: JobStore, config: JobSettings) -> UploadWriter:
        with lock:
            workers.add(current_thread())
        return acquire(self, config)

    monkeypatch.setattr(JobStore, "upload_writer", record_worker)

    async def run() -> None:
        release = asyncio.Event()
        ready = [asyncio.Event() for _ in range(requests)]

        async def body(number: int) -> AsyncIterator[bytes]:
            yield PDF
            ready[number].set()
            await release.wait()

        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            tasks = [
                asyncio.create_task(client.post("/documents", content=body(i), headers=RAW_HEADERS))
                for i in range(requests)
            ]
            for task, event in zip(tasks, ready, strict=True):

                def completed(
                    _: asyncio.Task[httpx.Response], event: asyncio.Event = event
                ) -> None:
                    event.set()

                task.add_done_callback(completed)
            try:
                async with asyncio.timeout(3):
                    await asyncio.gather(*(event.wait() for event in ready))
                if requests == 3 * limit:
                    with lock:
                        assert len(workers.intersection(threading.enumerate())) == limit
                rejected = [task.result() for task in tasks if task.done()]
                assert len(rejected) == requests - limit
                assert all(response.status_code == 503 for response in rejected)
                assert all(int(response.headers["Retry-After"]) > 0 for response in rejected)
                assert all("upload" in response.json()["detail"] for response in rejected)
                other = create_app(Profile.NATIVE, settings_at(tmp_path / "other"))
                async with (
                    other.router.lifespan_context(other),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=other), base_url="http://other"
                    ) as other_client,
                ):
                    response = await other_client.post(
                        "/documents", content=PDF, headers=RAW_HEADERS
                    )
                    assert response.status_code == 201
            finally:
                release.set()
                responses = await asyncio.gather(*tasks)
            accepted = [response for response in responses if response.status_code == 201]
            assert len(accepted) == limit
            assert all(
                response.json()["sha256"] == sha256(PDF).hexdigest() for response in accepted
            )
            assert (
                await client.post("/documents", content=PDF, headers=RAW_HEADERS)
            ).status_code == 201

    anyio.run(run)


@pytest.mark.parametrize(
    "failure", ["success", "disconnect", "cancellation", "abandoned", "cleanup"]
)
def test_upload_slot_is_held_until_worker_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    settings = settings_at(tmp_path).model_copy(update={"max_concurrent_uploads": 1})
    blocked, release = Event(), Event()
    cleanup = UploadWriter.__exit__

    def held_cleanup(self: UploadWriter, *exc: object) -> None:
        first = not blocked.is_set()
        if first:
            blocked.set()
            assert release.wait(5), "test did not release cleanup"
        cleanup(self, *exc)
        if first and failure == "cleanup":
            raise OSError("cleanup failed")

    monkeypatch.setattr(UploadWriter, "__exit__", held_cleanup)

    async def run() -> None:
        entered = asyncio.Event()

        async def body() -> AsyncIterator[bytes]:
            yield PDF
            entered.set()
            if failure in ("cancellation", "abandoned"):
                await asyncio.Event().wait()
            elif failure == "disconnect":
                raise ClientDisconnect()

        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            task = asyncio.create_task(
                client.post("/documents", content=body(), headers=RAW_HEADERS)
            )
            try:
                async with asyncio.timeout(3):
                    await entered.wait()
                if failure in ("cancellation", "abandoned"):
                    task.cancel("client cancelled")
                await wait_for(blocked)
                if failure == "abandoned":
                    task.cancel("abandon cleanup waiter")
                    with pytest.raises(asyncio.CancelledError, match="client cancelled"):
                        await task
                response = await client.post("/documents", content=PDF, headers=RAW_HEADERS)
                assert response.status_code == 503, response.text
                assert "Retry-After" in response.headers
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            if failure in ("cancellation", "abandoned"):
                assert task.cancelled()
            elif failure == "disconnect":
                assert isinstance(task.exception(), ClientDisconnect)
            elif failure == "cleanup":
                assert isinstance(task.exception(), OSError)
            else:
                assert task.result().status_code == 201
            # Abandoned cleanup may still be finishing after the request task exits.
            async with asyncio.timeout(3):
                while (
                    response := await client.post("/documents", content=PDF, headers=RAW_HEADERS)
                ).status_code == 503:
                    await asyncio.sleep(0.001)
            assert response.status_code == 201

    anyio.run(run)


def test_upload_parser_setup_failure_returns_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_at(tmp_path).model_copy(update={"max_concurrent_uploads": 1})
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(
                "fra_api.jobs.MultipartUpload", Mock(side_effect=OSError("parser failed"))
            )
            with pytest.raises(OSError, match="parser failed"):
                client.post("/documents", content=part(PDF) + END, headers=MULTIPART)
        assert client.post("/documents", content=PDF, headers=RAW_HEADERS).status_code == 201


@pytest.mark.parametrize("status", [400, 413])
def test_header_rejections_take_no_slot_and_invalid_uploads_release_it(
    tmp_path: Path, status: int
) -> None:
    settings = settings_at(tmp_path).model_copy(update={"max_concurrent_uploads": 1})

    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def body() -> AsyncIterator[bytes]:
            yield PDF
            entered.set()
            await release.wait()

        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            for headers, expected in [
                ({"Content-Type": "image/png"}, 415),
                ({"Content-Type": "application/pdf"}, 422),
            ]:
                assert (
                    await client.post("/documents", content=PDF, headers=headers)
                ).status_code == expected
            task = asyncio.create_task(
                client.post("/documents", content=body(), headers=RAW_HEADERS)
            )
            try:
                async with asyncio.timeout(3):
                    await entered.wait()
                response = await client.post(
                    "/documents", content=PDF, headers={"Content-Type": "image/png"}
                )
                assert response.status_code == 415
                response = await client.post(
                    "/documents", content=PDF, headers={"Content-Type": "application/pdf"}
                )
                assert response.status_code == 422
                response = await client.post("/documents", content=PDF, headers=RAW_HEADERS)
                assert response.status_code == 503
            finally:
                release.set()
                await task
            invalid = b"GIF89a" if status == 400 else PDF + b"x" * settings.max_upload_bytes
            response = await client.post("/documents", content=invalid, headers=RAW_HEADERS)
            assert response.status_code == status
            assert (
                await client.post("/documents", content=PDF, headers=RAW_HEADERS)
            ).status_code == 201

    anyio.run(run)


def test_multipart_and_raw_uploads_deduplicate_by_pdf_bytes(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        multipart = client.post("/documents", files={"file": ("r.pdf", PDF, "application/pdf")})
        assert multipart.status_code == 201, multipart.text
        assert multipart.json()["sha256"] == sha256(PDF).hexdigest()
        assert multipart.json()["size_bytes"] == len(PDF)
        assert multipart.json()["filename"] == "r.pdf"
        raw = client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "other.pdf"},
        )
        assert raw.status_code == 201 and raw.json() == multipart.json()
        assert (settings.root / "uploads" / f"{sha256(PDF).hexdigest()}.pdf").read_bytes() == PDF


@pytest.mark.parametrize(
    ("body", "headers", "status", "detail"),
    [
        (PDF, {"Content-Type": "image/png"}, 415, "content type"),
        (END, MULTIPART, 400, "missing file part 'file'"),
        (part(PDF, name="other") + END, MULTIPART, 400, "missing file part 'file'"),
        (part(PDF) + part(PDF, name="other") + END, MULTIPART, 400, "more than one file part"),
        (part(PDF) + part(PDF) + END, MULTIPART, 400, "more than one file part"),
        (part(b"") + END, MULTIPART, 400, "empty"),
        (part(PDF + b"x" * 10_000) + END, MULTIPART, 413, "limit"),
        (part(b"GIF89a") + END, MULTIPART, 400, "not a PDF"),
        (part(PDF, content_type="image/png") + END, MULTIPART, 415, "file content type"),
        (part(PDF) + END, {"Content-Type": "multipart/form-data"}, 400, "boundary"),
        (part(PDF), MULTIPART, 400, "incomplete multipart"),
        (b"broken", MULTIPART, 400, "malformed multipart"),
        (
            part(PDF) + END,
            {"Content-Type": "multipart/form-data; boundary=" + "x" * 257},
            400,
            "multipart boundary",
        ),
    ],
    ids=[
        "wrong-request-type",
        "missing-file",
        "wrong-field",
        "extra-file",
        "duplicate-file",
        "empty-file",
        "oversize-file",
        "non-pdf",
        "wrong-file-type",
        "missing-boundary",
        "truncated",
        "malformed",
        "oversized-boundary",
    ],
)
def test_invalid_uploads_are_loud_and_leave_no_files(
    tmp_path: Path, body: bytes, headers: dict[str, str], status: int, detail: str
) -> None:
    settings = settings_at(tmp_path)
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        response = client.post("/documents", content=body, headers=headers)
        assert response.status_code == status, response.text
        assert detail in response.json()["detail"]
        assert list((settings.root / "uploads").iterdir()) == []


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("bad_pdf", [False, True])
def test_invalid_stream_stops_before_the_rest_is_read(
    tmp_path: Path, multipart: bool, bad_pdf: bool
) -> None:
    settings = settings_at(tmp_path)
    consumed: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        body = b"GIF89a" if bad_pdf else PDF + b"x" * settings.max_upload_bytes
        chunks = (
            [part(b"")[:-2], body, b"unread tail\r\n" + END]
            if multipart
            else [body, b"unread tail"]
        )
        for number, chunk in enumerate(chunks):
            consumed.append(number)
            yield chunk

    async def run() -> None:
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post(
                "/documents",
                content=chunks(),
                headers=MULTIPART
                if multipart
                else {"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
            )
            assert response.status_code == (400 if bad_pdf else 413), response.text
        assert consumed == ([0, 1] if multipart else [0])
        assert list((settings.root / "uploads").iterdir()) == []

    anyio.run(run)


def test_multipart_accepts_the_exact_part_and_envelope_limits(tmp_path: Path) -> None:
    settings = settings_at(tmp_path).model_copy(
        update={"max_multipart_parts": 3, "max_multipart_overhead_bytes": 1024}
    )
    field = b'--boundary\r\nContent-Disposition: form-data; name="note"\r\n\r\nx\r\n'
    pdf = PDF + b"x" * (settings.max_upload_bytes - len(PDF))
    body = field * 2 + part(pdf) + END
    body += b"x" * (settings.max_upload_bytes + settings.max_multipart_overhead_bytes - len(body))
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        response = client.post("/documents", content=body, headers=MULTIPART)
        assert response.status_code == 201, response.text
        assert response.json()["sha256"] == sha256(pdf).hexdigest()
        assert response.json()["size_bytes"] == settings.max_upload_bytes


@pytest.mark.parametrize("case", ["parts", "fields", "header", "metadata", "epilogue", "envelope"])
def test_multipart_limits_stop_before_the_tail_is_read(tmp_path: Path, case: str) -> None:
    settings = settings_at(tmp_path).model_copy(
        update={"max_multipart_parts": 3, "max_multipart_overhead_bytes": 1024}
    )
    field = b'--boundary\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
    if case in ("parts", "fields"):
        prefix = part(PDF) if case == "parts" else field + b"\r\n"
        chunks = [prefix + (field + b"\r\n") * 2, b"--boundary\r\n"]
        detail = "parts"
    elif case == "header":
        chunks = [b"--boundary\r\nX-Padding: " + b"x" * 1025]
        detail = "metadata"
    elif case == "metadata":
        # Aggregate small fields, each below the allowance, must also be bounded.
        chunks = [field + b"x" * 500 + b"\r\n", field + b"x" * 500]
        detail = "metadata"
    else:
        chunks = [part(PDF) + END] if case == "epilogue" else [part(PDF)[:-2]]
        # Even ignored epilogue bytes and framing count against the envelope cap.
        chunks.append(b"x" * (settings.max_upload_bytes + 1024 - len(chunks[0]) + 1))
        detail = "envelope"
    consumed: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for number, chunk in enumerate([*chunks, b"unread tail"]):
            consumed.append(number)
            yield chunk

    async def run() -> None:
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post("/documents", content=body(), headers=MULTIPART)
            assert response.status_code == (400 if case in ("parts", "fields") else 413)
            assert detail in response.json()["detail"]
        assert consumed == list(range(len(chunks)))
        assert list((settings.root / "uploads").iterdir()) == []

    anyio.run(run)


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize(
    "failure", ["rejection", "truncation", "disconnect", "cancellation", "task-cancellation"]
)
def test_upload_cleanup_runs_off_loop_even_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multipart: bool, failure: str
) -> None:
    settings = settings_at(tmp_path)
    cleanup = UploadWriter.__exit__
    threads: list[int] = []
    cleaned = Event()

    def checked_cleanup(self: UploadWriter, *exc: object) -> None:
        threads.append(get_ident())
        cleanup(self, *exc)
        cleaned.set()

    monkeypatch.setattr(UploadWriter, "__exit__", checked_cleanup)

    async def run() -> None:
        event_thread = get_ident()

        async def body() -> AsyncIterator[bytes]:
            yield part(PDF) if multipart else PDF
            # Buffered PDF bytes and a temporary file exist before the failure.
            assert len(list((settings.root / "uploads").glob("*.tmp"))) == 1
            if failure == "disconnect":
                raise ClientDisconnect()
            if failure == "cancellation":
                scope.cancel()
                await anyio.sleep(0)
            elif failure == "task-cancellation":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await anyio.sleep(0)
            elif failure == "rejection":
                yield b"x" * (settings.max_upload_bytes + 1)
            elif not multipart:
                raise ClientDisconnect()

        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            with anyio.CancelScope() as scope:
                try:
                    response = await client.post(
                        "/documents",
                        content=body(),
                        headers=MULTIPART
                        if multipart
                        else {"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
                    )
                except ClientDisconnect:
                    assert failure == "disconnect" or (failure == "truncation" and not multipart)
                except asyncio.CancelledError:
                    assert failure in ("cancellation", "task-cancellation")
                else:
                    assert failure in ("rejection", "truncation")
                    assert response.status_code == (413 if failure == "rejection" else 400)
            assert scope.cancel_called == (failure == "cancellation")
        await wait_for(cleaned)
        assert threads and event_thread not in threads
        assert list((settings.root / "uploads").iterdir()) == []

    anyio.run(run)


@pytest.mark.parametrize("oversized", [False, True])
def test_upload_close_failure_is_loud_without_masking_size_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oversized: bool
) -> None:
    settings = settings_at(tmp_path)
    write = UploadWriter.write

    def failing_write(self: UploadWriter, data: bytes) -> None:
        self._file.close()
        monkeypatch.setattr(self, "_file", Mock(close=Mock(side_effect=OSError("close failed"))))
        write(self, data)

    monkeypatch.setattr(UploadWriter, "write", failing_write)
    # An invalid PDF gives cleanup an original failure; an oversize PDF must remain 413.
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        response = client.post(
            "/documents",
            content=PDF + b"x" * settings.max_upload_bytes if oversized else b"GIF89a",
            headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
        )
        assert response.status_code == (413 if oversized else 400)
    assert list((settings.root / "uploads").iterdir()) == []


def test_cleanup_failure_after_a_successful_request_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_at(tmp_path)
    cleanup = UploadWriter.__exit__

    def failing_cleanup(self: UploadWriter, *exc: object) -> None:
        cleanup(self, *exc)
        raise OSError("cleanup failed")

    monkeypatch.setattr(UploadWriter, "__exit__", failing_cleanup)
    with (
        TestClient(create_app(Profile.NATIVE, settings)) as client,
        pytest.raises(OSError, match="cleanup failed"),
    ):
        client.post(
            "/documents",
            content=PDF,
            headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
        )
    assert list((settings.root / "uploads").glob("*.tmp")) == []


async def wait_for(event: Event) -> None:
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("operation", ["write", "finish", "cleanup"])
@pytest.mark.parametrize("cancellations", [1, 2, 3])
@pytest.mark.parametrize("multipart", [False, True])
def test_cancellation_orders_cleanup_after_inflight_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    cancellations: int,
    multipart: bool,
) -> None:
    settings = settings_at(tmp_path)
    blocked, release, cleaned = Event(), Event(), Event()
    events: list[tuple[str, int]] = []
    failures: list[BaseException] = []
    acquire, write, finish, cleanup = (
        JobStore.upload_writer,
        UploadWriter.write,
        UploadWriter.finish,
        UploadWriter.__exit__,
    )

    def record(name: str) -> None:
        events.append((name, get_ident()))
        if name == operation:
            blocked.set()
            assert release.wait(5), "test did not release operation"

    def checked_acquire(self: JobStore, config: JobSettings) -> UploadWriter:
        record("acquire")
        return acquire(self, config)

    def checked_write(self: UploadWriter, data: bytes) -> None:
        record("write")
        try:
            write(self, data)
        except BaseException as exc:
            failures.append(exc)
            raise
        events.append(("wrote", get_ident()))

    def checked_finish(self: UploadWriter, filename: str) -> Upload:
        record("finish")
        try:
            result = finish(self, filename)
        except BaseException as exc:
            failures.append(exc)
            raise
        events.append(("finished", get_ident()))
        return result

    def checked_cleanup(self: UploadWriter, *exc: object) -> None:
        record("cleanup")
        try:
            cleanup(self, *exc)
        finally:
            events.append(("cleaned", get_ident()))
            cleaned.set()

    monkeypatch.setattr(JobStore, "upload_writer", checked_acquire)
    monkeypatch.setattr(UploadWriter, "write", checked_write)
    monkeypatch.setattr(UploadWriter, "finish", checked_finish)
    monkeypatch.setattr(UploadWriter, "__exit__", checked_cleanup)

    async def run() -> None:
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            task = asyncio.create_task(
                client.post(
                    "/documents",
                    content=part(PDF) + END if multipart else PDF,
                    headers=MULTIPART
                    if multipart
                    else {"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
                )
            )
            try:
                await wait_for(blocked)
                limiter = anyio.to_thread.current_default_thread_limiter()
                previous_tokens = limiter.total_tokens
                limiter.total_tokens = 1
                await limiter.acquire()
                try:
                    for number in range(cancellations):
                        task.cancel(f"cancellation {number + 1}")
                        await asyncio.sleep(0.01)
                    if operation != "cleanup":
                        assert "cleanup" not in [name for name, _ in events]
                    release.set()
                    with pytest.raises(asyncio.CancelledError, match="cancellation 1"):
                        await task
                    await wait_for(cleaned)
                finally:
                    limiter.release()
                    limiter.total_tokens = previous_tokens
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert not failures
        assert len({thread for _, thread in events}) == 1
        assert events[0][1] != get_ident()
        names = [name for name, _ in events]
        assert names[-2:] == ["cleanup", "cleaned"]
        assert names.count("cleanup") == 1
        assert list((settings.root / "uploads").glob("*.tmp")) == []

    anyio.run(run)


@pytest.mark.parametrize("cancellation", ["shutdown", "waiters", "race"])
def test_unclaimed_upload_writer_is_finalised_when_all_waiters_are_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    settings = settings_at(tmp_path)
    created, release, shutting_down, cleaned = (Event() for _ in range(4))
    race = Barrier(2)
    writers: list[UploadWriter] = []
    exits: list[int] = []
    acquire, cleanup = JobStore.upload_writer, UploadWriter.__exit__

    def held_acquire(self: JobStore, config: JobSettings) -> UploadWriter:
        writer = acquire(self, config)
        writers.append(writer)
        created.set()
        assert release.wait(5), "test did not release acquisition"
        if cancellation == "race":
            race.wait(5)
        return writer

    def checked_cleanup(self: UploadWriter, *exc: object) -> None:
        exits.append(get_ident())
        cleanup(self, *exc)
        cleaned.set()

    monkeypatch.setattr(JobStore, "upload_writer", held_acquire)
    monkeypatch.setattr(UploadWriter, "__exit__", checked_cleanup)

    async def request() -> None:
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            await client.post(
                "/documents",
                content=PDF,
                headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
            )

    async def start() -> None:
        task = asyncio.create_task(request())
        async with asyncio.timeout(3):
            while not created.is_set():
                await asyncio.sleep(0.001)
        task.cancel("cancel before claiming the writer")
        # Let the request enter its finally and start waiting for cleanup.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def shutdown_monitor() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            shutting_down.set()

    def release_during_shutdown() -> None:
        assert shutting_down.wait(5), "runner did not start shutdown"
        release.set()

    async def cancel_waiters() -> None:
        tasks = asyncio.all_tasks() - {asyncio.current_task()}
        if cancellation == "race":
            release.set()
            # The acquiring thread and waiter cancellation are released together.
            race.wait(5)
        for task in tasks:
            task.cancel("cancel cleanup waiter too")
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    event_thread = get_ident()
    releaser = Thread(target=release_during_shutdown) if cancellation == "shutdown" else None
    monitor: asyncio.Task[None] | None = None
    try:
        with asyncio.Runner() as runner:
            runner.run(start())
            if releaser is not None:

                async def arm_monitor() -> asyncio.Task[None]:
                    monitor = asyncio.create_task(shutdown_monitor())
                    await asyncio.sleep(0)
                    return monitor

                monitor = runner.run(arm_monitor())
                releaser.start()
            else:
                runner.run(cancel_waiters())
        assert cleaned.wait(3), "cleanup did not finish after runner shutdown"
        assert monitor is None or monitor.cancelled()
        assert len(writers) == 1
        assert exits and event_thread not in exits
        assert len(exits) == 1
        assert writers[0]._file.closed
        assert list((settings.root / "uploads").iterdir()) == []
    finally:
        release.set()
        shutting_down.set()
        if releaser is not None and releaser.ident is not None:
            releaser.join(5)


@pytest.mark.parametrize("shutdown", ["asyncio-run", "loop-close"])
def test_route_cleans_acquisition_after_the_event_loop_has_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown: str
) -> None:
    settings = settings_at(tmp_path)
    created, release, cleaned = Event(), Event(), Event()
    writers: list[UploadWriter] = []
    threads: list[int] = []
    unlinks: list[Path] = []
    acquire, cleanup, unlink = JobStore.upload_writer, UploadWriter.__exit__, Path.unlink

    def held_acquire(self: JobStore, config: JobSettings) -> UploadWriter:
        writer = acquire(self, config)
        writers.append(writer)
        threads.append(get_ident())
        created.set()
        assert release.wait(5), "test did not release acquisition"
        return writer

    def checked_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".tmp":
            unlinks.append(self)
        unlink(self, missing_ok=missing_ok)

    def checked_cleanup(self: UploadWriter, *exc: object) -> None:
        threads.append(get_ident())
        cleanup(self, *exc)
        cleaned.set()

    monkeypatch.setattr(JobStore, "upload_writer", held_acquire)
    monkeypatch.setattr(UploadWriter, "__exit__", checked_cleanup)
    monkeypatch.setattr(Path, "unlink", checked_unlink)
    loops: list[asyncio.AbstractEventLoop] = []

    async def request() -> None:
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            await client.post(
                "/documents",
                content=PDF,
                headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
            )

    async def start() -> None:
        loops.append(asyncio.get_running_loop())
        task = asyncio.create_task(request())
        await wait_for(created)
        task.cancel("first cancellation")
        await asyncio.sleep(0)
        if shutdown == "loop-close":
            task.cancel("second cancellation")
            with pytest.raises(asyncio.CancelledError, match="first cancellation"):
                await task

    loop = asyncio.new_event_loop() if shutdown == "loop-close" else None
    try:
        if loop is None:
            asyncio.run(start())
        else:
            loop.run_until_complete(start())
            loop.close()
        assert loops[0].is_closed()
        callback = Mock(side_effect=AssertionError("cleanup used the closed loop"))
        monkeypatch.setattr(loops[0], "call_soon_threadsafe", callback)
        release.set()
        assert cleaned.wait(3), "cleanup did not drain after loop closure"
        assert len(writers) == 1 and writers[0]._file.closed
        assert unlinks == [writers[0]._tmp]
        assert len(threads) == 2 and len(set(threads)) == 1
        assert threads[0] != get_ident()
        callback.assert_not_called()
        assert list((settings.root / "uploads").iterdir()) == []
    finally:
        release.set()
        if loop is not None and not loop.is_closed():
            loop.close()


def test_multipart_stream_handles_split_headers_magic_and_boundaries_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_at(tmp_path)
    acquire, write, finish, cleanup, parse = (
        JobStore.upload_writer,
        UploadWriter.write,
        UploadWriter.finish,
        UploadWriter.__exit__,
        MultipartUpload.write,
    )
    events: list[str] = []
    threads: list[int] = []

    def checked_acquire(self: JobStore, config: JobSettings) -> UploadWriter:
        threads.append(get_ident())
        events.append("acquire")
        return acquire(self, config)

    def checked_parse(self: MultipartUpload, data: bytes) -> None:
        threads.append(get_ident())
        events.append("parse")
        parse(self, data)

    def checked_cleanup(self: UploadWriter, *exc: object) -> None:
        threads.append(get_ident())
        events.append("cleanup")
        cleanup(self, *exc)

    def checked_write(self: UploadWriter, data: bytes) -> None:
        threads.append(get_ident())
        events.append("write")
        write(self, data)

    def checked_finish(self: UploadWriter, filename: str) -> Upload:
        threads.append(get_ident())
        events.append("finish")
        return finish(self, filename)

    monkeypatch.setattr(JobStore, "upload_writer", checked_acquire)
    monkeypatch.setattr(MultipartUpload, "write", checked_parse)
    monkeypatch.setattr(UploadWriter, "__exit__", checked_cleanup)
    monkeypatch.setattr(UploadWriter, "write", checked_write)
    monkeypatch.setattr(UploadWriter, "finish", checked_finish)

    async def chunks() -> AsyncIterator[bytes]:
        for byte in part(PDF) + END:
            yield bytes([byte])

    async def run() -> None:
        event_thread = get_ident()
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            response = await client.post("/documents", content=chunks(), headers=MULTIPART)
            assert response.status_code == 201, response.text
            assert response.json()["sha256"] == sha256(PDF).hexdigest()
        assert threads and event_thread not in threads
        assert len(set(threads)) == 1
        assert events[0] == "acquire" and events[-2:] == ["finish", "cleanup"]

    anyio.run(run)


@pytest.mark.parametrize("status", [400, 413, 415])
def test_close_error_adds_a_note_without_masking_multipart_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    settings = settings_at(tmp_path)
    acquire, parse = JobStore.upload_writer, MultipartUpload.write
    failures: list[Exception] = []

    def failing_close(self: JobStore, config: JobSettings) -> UploadWriter:
        writer = acquire(self, config)
        writer._file.close()
        monkeypatch.setattr(writer, "_file", Mock(close=Mock(side_effect=OSError("close failed"))))
        return writer

    def checked_parse(self: MultipartUpload, data: bytes) -> None:
        try:
            parse(self, data)
        except Exception as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(JobStore, "upload_writer", failing_close)
    monkeypatch.setattr(MultipartUpload, "write", checked_parse)
    body = (
        part(
            b"GIF89a" if status == 400 else PDF + b"x" * settings.max_upload_bytes,
            content_type="image/png" if status == 415 else "application/pdf",
        )
        + END
    )
    with TestClient(create_app(Profile.NATIVE, settings)) as client:
        response = client.post("/documents", content=body, headers=MULTIPART)
        assert response.status_code == status, response.text
    assert len(failures) == 1
    assert failures[0].__notes__ == ["upload cleanup failed: OSError('close failed')"]
    assert list((settings.root / "uploads").iterdir()) == []


def test_abandoned_cleanup_error_is_logged_after_loop_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_at(tmp_path)
    created, release, logged = Event(), Event(), Event()
    acquire = JobStore.upload_writer
    writers: list[UploadWriter] = []

    def held_acquire(self: JobStore, config: JobSettings) -> UploadWriter:
        writer = acquire(self, config)
        writers.append(writer)
        writer._file.close()
        monkeypatch.setattr(writer, "_file", Mock(close=Mock(side_effect=OSError("close failed"))))
        created.set()
        assert release.wait(5), "test did not release acquisition"
        return writer

    from fra_api.jobs import logger

    log_exception = logger.exception

    def checked_log(message: str) -> None:
        log_exception(message)
        logged.set()

    monkeypatch.setattr(JobStore, "upload_writer", held_acquire)
    monkeypatch.setattr(logger, "exception", checked_log)
    cancellations: list[asyncio.CancelledError] = []

    async def request() -> None:
        app = create_app(Profile.NATIVE, settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            await client.post(
                "/documents",
                content=PDF,
                headers={"Content-Type": "application/pdf", "X-Filename": "r.pdf"},
            )

    async def start() -> None:
        task = asyncio.create_task(request())
        await wait_for(created)
        task.cancel("original cancellation")
        await asyncio.sleep(0)
        task.cancel("abandon cleanup wait")
        with pytest.raises(asyncio.CancelledError, match="original cancellation") as caught:
            await task
        cancellations.append(caught.value)

    loop = asyncio.new_event_loop()
    errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _, context: errors.append(context))
    try:
        loop.run_until_complete(start())
        loop.close()
        release.set()
        assert logged.wait(3), "cleanup failure was not logged without a running loop"
        assert not writers[0]._tmp.exists()
        assert cancellations[0].__notes__ == ["upload cleanup failed: OSError('close failed')"]
        assert len(caplog.records) == 1
        assert caplog.records[0].exc_info is not None
        assert "close failed" in caplog.text
        assert not errors
    finally:
        release.set()
        if not loop.is_closed():
            loop.close()
