"""One upload's ordered operations and final cleanup, independent of the event loop."""

from collections.abc import Callable
from concurrent.futures import Future
from queue import SimpleQueue
from threading import Thread


class UploadWorker:
    """Single-use owner: start before submit, then close exactly once as the last message."""

    def __init__(self, on_exit: Callable[[], None]) -> None:
        self._queue: SimpleQueue[Callable[[], bool]] = SimpleQueue()
        self._on_exit = on_exit

    def start(self) -> None:
        # Non-daemon so queued cleanup drains at exit. Every route path ends in close().
        self._thread = Thread(target=self._run, name="pdf-upload", daemon=False)
        try:
            self._thread.start()
        except BaseException:
            # start() precedes every submit, so only this abandon message can be queued.
            self._queue.put(lambda: True)
            raise

    def submit[T, **P](self, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        future: Future[T] = Future()

        def work() -> bool:
            if future.set_running_or_notify_cancel():
                self._complete(future, lambda: fn(*args, **kwargs))
            return False

        self._queue.put(work)
        return future

    def close(self, cleanup: Callable[[], None]) -> Future[None]:
        future: Future[None] = Future()
        # Closing is never cancellable: it owns cleanup and the slot, even without a loop.
        future.set_running_or_notify_cancel()

        def finish() -> None:
            try:
                cleanup()
            finally:
                self._on_exit()

        def last() -> bool:
            self._complete(future, finish)
            return True

        self._queue.put(last)
        return future

    @staticmethod
    def _complete[T](future: Future[T], fn: Callable[[], T]) -> None:
        try:
            result = fn()
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)

    def _run(self) -> None:
        while not self._queue.get()():
            pass
