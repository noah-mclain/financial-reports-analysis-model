"""Executable upload lifecycle matrix, through the real POST /documents route.

Invariant | Evidence in each cell
I1 slot   | one release after cleanup-end, capacity restored, over-release rejected
I2 cleanup| exactly one cleanup body, last operation (no body if start failed)
I3 order  | acquire < writes < finish < cleanup < release; max in-flight = one
I4 files  | no temp after cleanup; successful finish preserves PDF and sha256
I5 errors | original identity/status; cleanup raised alone or noted on original
I6 thread | join with timeout, non-daemon worker exited
I7 live   | next request on the same application succeeds

Product: raw/multipart x operation fault x cleanup fault; raw/multipart x
acquire/write/finish/closing barrier x 1/2/3 cancellations x cleanup fault.
Shutdown: asyncio.run cancellation and loop.close after cancellation at each barrier.
Start failure has no queued operation, writer, cleanup body, or thread to join.
Interrupted start may launch a thread; it must exit without cleanup or on_exit.
There is no on_exit-raising cell: BoundedSemaphore.release raises only on over-release, unreachable because on_exit runs exactly once after cleanup on the worker thread.
Acquire failure has no writer to close. Parse faults only apply to multipart.
Faults after the first failure are unreachable; cancellation while an operation is
running may prevent observing its error, but must never prevent cleanup.
A real unlink failure can leave its temp file: this is loud, not a deletion guarantee.
The injected unlink fault below raises AFTER deletion to check the error contract
and I4 together. The refused-unlink cells demonstrate why I4 cannot hold when
the filesystem denies removal; they assert the orphan explicitly, then remove it
after recording this limitation so the remaining invariants can be checked.
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread, get_ident
from threading import enumerate as enumerate_threads
from unittest.mock import Mock

import httpx
import pytest
from api_support import settings_at
from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from starlette.responses import Response

from fra_api.app import create_app
from fra_api.upload_worker import UploadWorker
from fra_core.jobs import JobSettings, JobStore, UploadWriter
from fra_core.profile import Profile
from fra_core.schemas import Upload

PDF = b"%PDF-1.7\n%matrix\n"
RAW = {"Content-Type": "application/pdf", "X-Filename": "matrix.pdf"}


@pytest.mark.parametrize("failure_point", ["construct", "start"])
@pytest.mark.parametrize("attempts", [1, 3, 9])
@pytest.mark.parametrize("multipart", [False, True])
def test_start_failure_has_no_queued_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
    multipart: bool,
    failure_point: str,
) -> None:
    """A failed start cannot leave an acquisition queued for a later start attempt."""
    matrix = Matrix(tmp_path, monkeypatch, multipart)
    failure = RuntimeError("injected thread start failure")

    def rejected(self: Thread) -> None:
        raise failure

    async def run() -> None:
        async with matrix.client() as client:
            with monkeypatch.context() as patch:
                if failure_point == "start":
                    patch.setattr(Thread, "start", rejected)
                else:
                    patch.setattr("fra_api.upload_worker.Thread", Mock(side_effect=failure))
                for _ in range(attempts):
                    with pytest.raises(RuntimeError) as caught:
                        await matrix.request(client)
                    assert caught.value is failure
                    matrix.invariants(start_failed=True)
                    matrix.events.clear()
                    matrix.threads.clear()
            await matrix.next_upload(client)

    asyncio.run(run())


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("multipart", [False, True])
def test_interrupted_start_abandons_launched_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
    multipart: bool,
) -> None:
    matrix = Matrix(tmp_path, monkeypatch, multipart)
    failure = error_type("injected interruption after thread launch")
    workers: list[UploadWorker] = []
    start = UploadWorker.start

    def recorded(worker: UploadWorker) -> None:
        workers.append(worker)
        start(worker)

    class InterruptedThread(Thread):
        def start(self) -> None:
            super().start()
            raise failure

    monkeypatch.setattr(UploadWorker, "start", recorded)

    async def run() -> None:
        async with matrix.client() as client:
            try:
                for _ in range(3 * matrix.settings.max_concurrent_uploads):
                    matrix.events.clear()
                    matrix.maximum = 0
                    with monkeypatch.context() as patch:
                        patch.setattr("fra_api.upload_worker.Thread", InterruptedThread)
                        with pytest.raises(error_type) as caught:
                            await matrix.request(client)
                    assert caught.value is failure
                    for thread in matrix.threads:
                        thread.join(3)
                        assert not thread.is_alive(), "interrupted start leaked upload thread"
                    assert not any(t.name == "pdf-upload" for t in enumerate_threads())
                    matrix.invariants(start_failed=True)
                    await matrix.next_upload(client)
                    matrix.threads.clear()
            finally:
                # Stop a leaked worker even when the regression assertion fails.
                for worker in workers:
                    if worker._thread.is_alive():
                        worker._queue.put(lambda: True)
                    worker._thread.join(3)

    asyncio.run(run())


class Matrix:
    def __init__(
        self,
        path: Path,
        patch: pytest.MonkeyPatch,
        multipart: bool,
        fault: str = "none",
        cleanup_fault: str = "none",
        barrier: str = "none",
    ) -> None:
        self.multipart = multipart
        self.fault = fault
        self.cleanup_fault = cleanup_fault
        self.barrier = barrier
        self.error = OSError(f"injected {fault}")
        self.cleanup_error = OSError(f"injected cleanup {cleanup_fault}")
        self.events: list[tuple[str, int]] = []
        self.active = 0
        self.maximum = 0
        self.blocked, self.resume = Event(), Event()
        self.threads: list[Thread] = []
        self.writes = 0
        self.finished = False
        self.raised: list[BaseException] = []
        self.http_errors: list[HTTPException] = []
        self.original: BaseException | None = None
        self.settings = settings_at(path).model_copy(update={"max_concurrent_uploads": 1})
        self.app = create_app(Profile.NATIVE, self.settings)

        async def observed(request: Request, error: Exception) -> Response:
            assert isinstance(error, HTTPException)
            self.http_errors.append(error)
            return await http_exception_handler(request, error)

        self.app.add_exception_handler(HTTPException, observed)
        matrix = self

        class Writer(UploadWriter):
            def write(self, data: bytes) -> None:
                matrix.writes += 1
                with matrix.operation(f"write-{matrix.writes}"):
                    super().write(data)

            def finish(self, filename: str) -> Upload:
                with matrix.operation("finish"):
                    result = super().finish(filename)
                    matrix.finished = True
                    return result

            def __exit__(self, *exc: object) -> None:
                # Inject real close/unlink exceptions into the unchanged core cleanup.
                with pytest.MonkeyPatch.context() as local:
                    if matrix.cleanup_fault == "close":
                        handle = self._file

                        def close() -> None:
                            handle.close()
                            raise matrix.cleanup_error

                        local.setattr(self, "_file", Mock(wraps=handle, close=close))
                    elif matrix.cleanup_fault == "unlink":
                        unlink = Path.unlink

                        def fail_unlink(path: Path, missing_ok: bool = False) -> None:
                            unlink(path, missing_ok=missing_ok)
                            if path == self._tmp:
                                raise matrix.cleanup_error

                        local.setattr(Path, "unlink", fail_unlink)
                    super().__exit__(*exc)

        class Store(JobStore):
            def upload_writer(self, settings: JobSettings) -> UploadWriter:
                with matrix.operation("acquire"):
                    return Writer(self, settings)

        class Worker(UploadWorker):
            def _run(self) -> None:
                if matrix.barrier == "queued-acquire":
                    matrix.blocked.set()
                    assert matrix.resume.wait(5), "test did not release worker queue"
                super()._run()

            def start(self) -> None:
                try:
                    super().start()
                finally:
                    if hasattr(self, "_thread"):
                        matrix.threads.append(self._thread)

            def close(self, cleanup: Callable[[], None]) -> Future[None]:
                matrix.original = sys.exception()

                def checked() -> None:
                    with matrix.operation("cleanup"):
                        cleanup()

                return super().close(checked)

        self.store = Store(self.settings.root)
        self.app.state.store = self.store
        slots = self.app.state.upload_slots
        release = slots.release

        def released() -> None:
            self.events.append(("release", get_ident()))
            release()

        patch.setattr(slots, "release", released)
        patch.setattr("fra_api.jobs.UploadWorker", Worker)

    @contextmanager
    def operation(self, name: str) -> Iterator[None]:
        self.events.append((name, get_ident()))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if name == self.barrier:
                self.blocked.set()
                assert self.resume.wait(5), f"test did not release {name}"
            if name == self.fault:
                raise self.error
            yield
        finally:
            self.active -= 1
            self.events.append((f"{name}-end", get_ident()))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def request(self, client: httpx.AsyncClient) -> httpx.Response:
        try:
            return await self._request(client)
        except BaseException as error:
            self.raised.append(error)
            raise

    async def _request(self, client: httpx.AsyncClient) -> httpx.Response:
        async def chunks() -> AsyncIterator[bytes]:
            body = b"not a PDF" if self.fault == "non-pdf" else PDF
            if self.fault == "limit":
                body += b"x" * self.settings.max_upload_bytes
            if self.multipart:
                yield (
                    b'--b\r\nContent-Disposition: form-data; name="file"; filename="m.pdf"\r\n'
                    b"Content-Type: application/pdf\r\n\r\n"
                )
            yield body[:7]
            yield body[7:10]
            yield body[10:]
            if self.multipart:
                yield b"\r\n--b--\r\n"

        headers = {"Content-Type": "multipart/form-data; boundary=b"} if self.multipart else RAW
        if self.fault == "parse":
            headers = {"Content-Type": "multipart/form-data; boundary=b"}

            async def malformed() -> AsyncIterator[bytes]:
                yield b"this is not a multipart boundary"

            return await client.post("/documents", content=malformed(), headers=headers)
        return await client.post("/documents", content=chunks(), headers=headers)

    def invariants(self, start_failed: bool = False) -> None:
        for thread in self.threads:
            if not start_failed:
                thread.join(3)
            assert not thread.is_alive(), "upload thread leaked"
            assert not thread.daemon
        names = [name for name, _ in self.events]
        assert names.count("cleanup") == names.count("cleanup-end") == (0 if start_failed else 1)
        assert names.count("release") == 1
        assert names[-3:] == (
            ["release"] if start_failed else ["cleanup", "cleanup-end", "release"]
        )
        assert self.app.state.upload_slots._value == self.settings.max_concurrent_uploads
        with pytest.raises(ValueError, match="released too many"):
            # Bypass the logging wrapper so this check is not a second release event.
            type(self.app.state.upload_slots).release(self.app.state.upload_slots)
        assert self.maximum == (0 if start_failed else 1) and self.active == 0
        ranks = [
            0 if name == "acquire" else 1 if name.startswith("write-") else 2
            for name in names
            if name == "acquire" or name.startswith("write-") or name == "finish"
        ]
        assert ranks == sorted(ranks)
        assert len({thread for _, thread in self.events}) == 1
        assert (self.events[0][1] == get_ident()) is start_failed
        assert list((self.settings.root / "uploads").glob("*.tmp")) == []
        final = self.store.upload_path(sha256(PDF).hexdigest())
        if self.finished:
            assert final.read_bytes() == PDF
            assert sha256(final.read_bytes()).hexdigest() == final.stem
        else:
            assert not final.exists()

    async def next_upload(self, client: httpx.AsyncClient) -> None:
        self.fault = self.cleanup_fault = self.barrier = "none"
        self.events.clear()
        response = await client.post("/documents", content=PDF, headers=RAW)
        assert response.status_code == 201
        self.invariants()


async def wait_for(event: Event) -> None:
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize(
    ("multipart", "fault"),
    [
        (multipart, fault)
        for multipart in (False, True)
        for fault in (
            "none",
            "acquire",
            "write-1",
            "write-2",
            "finish",
            "limit",
            "non-pdf",
            "parse",
        )
        if multipart or fault != "parse"
    ],
)
@pytest.mark.parametrize("cleanup_fault", ["none", "close", "unlink"])
def test_fault_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multipart: bool,
    fault: str,
    cleanup_fault: str,
) -> None:
    matrix = Matrix(tmp_path, monkeypatch, multipart, fault, cleanup_fault)

    async def run() -> None:
        async with matrix.client() as client:
            if fault in ("acquire", "write-1", "write-2", "finish"):
                with pytest.raises(OSError) as caught:
                    await matrix.request(client)
                assert caught.value is matrix.error
                if cleanup_fault != "none" and fault != "acquire":
                    assert repr(matrix.cleanup_error) in " ".join(caught.value.__notes__)
            elif fault == "none" and cleanup_fault != "none":
                with pytest.raises(OSError) as caught:
                    await matrix.request(client)
                assert caught.value is matrix.cleanup_error
            else:
                response = await matrix.request(client)
                assert (
                    response.status_code
                    == {"none": 201, "limit": 413, "non-pdf": 400, "parse": 400}[fault]
                )
                if fault != "none":
                    cause = matrix.http_errors[-1].__cause__
                    assert cause is matrix.original and cause is not None
                    if cleanup_fault != "none":
                        assert repr(matrix.cleanup_error) in " ".join(cause.__notes__)
            matrix.invariants()
            await matrix.next_upload(client)

    asyncio.run(run())


@pytest.mark.parametrize("multipart", [False, True], ids=["raw", "multipart"])
@pytest.mark.parametrize("operation", ["acquire", "write-1", "finish", "cleanup"])
@pytest.mark.parametrize("cancellations", [1, 2, 3])
@pytest.mark.parametrize("cleanup_fault", ["none", "close", "unlink"])
def test_cancellation_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    multipart: bool,
    operation: str,
    cancellations: int,
    cleanup_fault: str,
) -> None:
    matrix = Matrix(
        tmp_path, monkeypatch, multipart, cleanup_fault=cleanup_fault, barrier=operation
    )

    async def run() -> None:
        async with matrix.client() as client:
            task = asyncio.create_task(matrix.request(client))
            try:
                await wait_for(matrix.blocked)
                for number in range(cancellations):
                    task.cancel(f"cancel-{number}")
                    await asyncio.sleep(0.01)
                assert matrix.app.state.upload_slots._value == 0
                assert "release" not in [name for name, _ in matrix.events]
                response = await client.post("/documents", content=PDF, headers=RAW)
                assert response.status_code == 503 and response.headers["Retry-After"] == "1"
            finally:
                matrix.resume.set()
                # Await directly: gather(return_exceptions=True) replaces cancellation identity.
                with pytest.raises(asyncio.CancelledError, match="cancel-0") as caught:
                    await task
                original = caught.value
                assert original is matrix.raised[0]
            matrix.invariants()
            if cleanup_fault != "none":
                assert repr(matrix.cleanup_error) in " ".join(original.__notes__)
                if cancellations > 1 or operation == "cleanup":
                    assert any(
                        "cleanup failed after request cancellation" in r.message
                        for r in caplog.records
                    )
            await matrix.next_upload(client)

    asyncio.run(run())


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("shutdown", ["asyncio-run", "loop-close"])
@pytest.mark.parametrize("operation", ["acquire", "write-1", "finish", "cleanup"])
@pytest.mark.parametrize("cleanup_fault", ["none", "close", "unlink"])
def test_shutdown_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    shutdown: str,
    multipart: bool,
    operation: str,
    cleanup_fault: str,
) -> None:
    matrix = Matrix(
        tmp_path, monkeypatch, multipart, cleanup_fault=cleanup_fault, barrier=operation
    )
    tasks: list[asyncio.Task[httpx.Response]] = []
    monitors: list[asyncio.Task[None]] = []

    async def start() -> None:
        client = matrix.client()
        task = asyncio.create_task(matrix.request(client))
        tasks.append(task)
        await wait_for(matrix.blocked)
        if shutdown == "loop-close":
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            with pytest.raises(asyncio.CancelledError, match="first cancellation"):
                await task
        # asyncio.run cancels both tasks; release barriers during shutdown, not on a timer.
        if shutdown == "asyncio-run":

            async def monitor() -> None:
                try:
                    await asyncio.Event().wait()
                finally:
                    matrix.resume.set()

            monitors.append(asyncio.create_task(monitor()))
            await asyncio.sleep(0)

    loop = asyncio.new_event_loop() if shutdown == "loop-close" else None
    try:
        if loop is None:
            asyncio.run(start())
        else:
            loop.run_until_complete(start())
            loop.close()
        assert tasks[0].cancelled()
    finally:
        matrix.resume.set()
        for thread in matrix.threads:
            thread.join(3)
        if loop is not None and not loop.is_closed():
            loop.close()
    matrix.invariants()
    if cleanup_fault != "none":
        assert repr(matrix.cleanup_error) in " ".join(matrix.raised[0].__notes__)
        if shutdown == "loop-close" or operation == "cleanup":
            assert any(
                "cleanup failed after request cancellation" in r.message for r in caplog.records
            )

    async def next_request() -> None:
        async with matrix.client() as client:
            await matrix.next_upload(client)

    asyncio.run(next_request())


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("cancellations", [1, 2, 3])
def test_cancelled_queued_acquisition_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multipart: bool, cancellations: int
) -> None:
    matrix = Matrix(tmp_path, monkeypatch, multipart, barrier="queued-acquire")

    async def run() -> None:
        async with matrix.client() as client:
            task = asyncio.create_task(matrix.request(client))
            try:
                await wait_for(matrix.blocked)
                for number in range(cancellations):
                    task.cancel(f"cancel-{number}")
                    await asyncio.sleep(0.01)
                assert matrix.app.state.upload_slots._value == 0
                assert not matrix.events
            finally:
                matrix.resume.set()
                with pytest.raises(asyncio.CancelledError, match="cancel-0") as caught:
                    await task
            assert caught.value is matrix.raised[0]
            matrix.invariants()
            assert [name for name, _ in matrix.events] == ["cleanup", "cleanup-end", "release"]
            await matrix.next_upload(client)

    asyncio.run(run())


@pytest.mark.parametrize(
    "error_type", [OSError, SystemExit, KeyboardInterrupt, asyncio.CancelledError]
)
def test_worker_survives_base_exception_and_skips_cancelled_work(
    error_type: type[BaseException],
) -> None:
    events: list[str] = []
    entered, release = Event(), Event()
    error = error_type("injected operation failure")
    worker = UploadWorker(on_exit=lambda: events.append("release"))

    def blocked() -> None:
        entered.set()
        assert release.wait(5), "test did not release operation"
        raise error

    worker.start()
    try:
        first = worker.submit(blocked)
        assert entered.wait(3)
        skipped = worker.submit(lambda: events.append("must not run"))
        assert skipped.cancel()
        following = worker.submit(lambda: events.append("following"))
    finally:
        closing = worker.close(lambda: events.append("cleanup"))
        assert not closing.cancel()
        release.set()
        worker._thread.join(3)
    assert not worker._thread.is_alive()
    with pytest.raises(error_type) as caught:
        first.result()
    assert caught.value is error
    assert skipped.cancelled()
    following.result()
    closing.result()
    assert events == ["following", "cleanup", "release"]


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("fault", ["acquire", "write-1", "write-2", "finish"])
@pytest.mark.parametrize("cleanup_fault", ["none", "close", "unlink"])
@pytest.mark.parametrize("cancellations", [1, 2, 3])
def test_original_failure_survives_cancellation_of_closing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    multipart: bool,
    fault: str,
    cleanup_fault: str,
    cancellations: int,
) -> None:
    matrix = Matrix(tmp_path, monkeypatch, multipart, fault, cleanup_fault, barrier="cleanup")

    async def run() -> None:
        async with matrix.client() as client:
            task = asyncio.create_task(matrix.request(client))
            try:
                await wait_for(matrix.blocked)
                for number in range(cancellations):
                    task.cancel(f"cancel-{number}")
                    await asyncio.sleep(0.01)
                assert matrix.app.state.upload_slots._value == 0
            finally:
                matrix.resume.set()
                with pytest.raises(OSError) as caught:
                    await task
            assert caught.value is matrix.error
            matrix.invariants()
            if cleanup_fault != "none" and fault != "acquire":
                assert repr(matrix.cleanup_error) in " ".join(matrix.error.__notes__)
                assert any(
                    "cleanup failed after request cancellation" in r.message for r in caplog.records
                )
            await matrix.next_upload(client)

    asyncio.run(run())


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("original_fault", ["none", "write-1"])
def test_refused_unlink_is_loud_but_cannot_guarantee_no_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multipart: bool,
    original_fault: str,
) -> None:
    matrix = Matrix(tmp_path, monkeypatch, multipart, fault=original_fault)
    # A refused unlink after successful finish still raises, but has no temp left to remove.
    unlink = Path.unlink
    denied = PermissionError("filesystem denied temp removal")

    def refuse(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".tmp":
            raise denied
        unlink(path, missing_ok=missing_ok)

    async def run() -> None:
        async with matrix.client() as client:
            with monkeypatch.context() as patch:
                patch.setattr(Path, "unlink", refuse)
                with pytest.raises(OSError) as caught:
                    await matrix.request(client)
                if original_fault == "none":
                    assert caught.value is denied
                else:
                    assert caught.value is matrix.error
                    assert repr(denied) in " ".join(matrix.error.__notes__)
            for thread in matrix.threads:
                thread.join(3)
                assert not thread.is_alive()
            temps = list((matrix.settings.root / "uploads").glob("*.tmp"))
            assert len(temps) == (0 if matrix.finished else 1)
            # I4 is impossible for a real refusal; record the orphan before test teardown.
            for temp in temps:
                temp.unlink()
            matrix.invariants()
            await matrix.next_upload(client)

    asyncio.run(run())
