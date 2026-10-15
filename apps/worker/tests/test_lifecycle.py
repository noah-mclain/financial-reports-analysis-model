"""Native shutdown and crash exclusion with a stdlib stand-in for conversion."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from fra_core.jobs import JobStore, WorkerAlreadyRunning

# Real main -> real_pipeline -> convert_in_child, replacing only stage dependencies. The
# conversion child keeps its ready FIFO open until exit; EOF is proof of process cleanup.
_DRIVER = r'''
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from fra_core.jobs import JobSettings, JobStore
from fra_core.profile import Profile
from fra_core.schemas import Document
from fra_ingest.config import IngestConfig
from fra_ingest.results import LocateResult, IndustrySignal
import fra_worker.main as worker
import fra_worker.pipeline as stages

root = Path(sys.argv[1])
store = JobStore(root / "jobs")
settings = JobSettings(root=store.root, max_upload_bytes=1000, max_pages=10, poll_seconds=1)
upload = store.add_upload(b"%PDF-1.7 synthetic", "test.pdf", settings)
if sys.argv[3] == "run":
    store.enqueue(upload.sha256)
config = IngestConfig(artifact_root=root / "artifacts", child_timeout_s=60)
worker.read_profile = lambda env: Profile.NATIVE
worker.load_job_settings = lambda *args: settings
worker.load_config = lambda: config
worker.load_policy = lambda *args: None
stages.make_engine = lambda config: None
stages.page_count = lambda pdf: 1
stages.current_convert = lambda pdf, config, ocr, located, convert: convert(pdf, config)
if sys.argv[2] == "structure":
    stages.current_convert = lambda *args: SimpleNamespace(ranges=[])
    stages.structure_pdf = lambda pdf, config, ocr, convert: convert(pdf, config)
stages.load_or_locate = lambda *args: LocateResult(
    version="test", document=Document(sha256=upload.sha256, filename="test.pdf",
    page_count=1, language="en"), pages=[], ranges=[], convert_ranges=[],
    industry=IndustrySignal(kind="corporate"))
original = stages.convert_in_child
child_code = """
import os
import sys
from pathlib import Path
root = Path(sys.argv[1])
ready = os.open(root / 'ready', os.O_WRONLY)
os.write(ready, str(os.getpid()).encode())
with (root / 'release').open('rb', buffering=0) as release:
    release.read(1)
"""
def convert(pdf, config, **kwargs):
    return original(pdf, config, command=[sys.executable, "-c", child_code, str(root)], **kwargs)
stages.convert_in_child = convert
sys.exit(worker.main(["--once"]))
'''

_REPEATED_STOP = r"""
import inspect
import signal
import fra_ingest.child as child_runner

phase, second_signal = sys.argv[4], int(sys.argv[5])
original_popen = child_runner.subprocess.Popen
def repeat_stop():
    (root / "second-signal").write_text(phase)
    signal.raise_signal(second_signal)

class ObservedChild(original_popen):
    def communicate(self, *args, **kwargs):
        with (root / "communicating").open("wb", buffering=0) as ready:
            ready.write(b"x")
        return super().communicate(*args, **kwargs)

    def kill(self):
        if phase == "during_cleanup":
            repeat_stop()
        return super().kill()

    def wait(self, *args, **kwargs):
        code = super().wait(*args, **kwargs)
        (root / "reaped").write_text(str(code))
        return code

child_runner.subprocess.Popen = ObservedChild
if phase == "before_deferral":
    # Stop exactly in the finally block before the cleanup handler is installed.
    # The first signal arrives normally in communicate(); tracing only locates the gap.
    source, first_line = inspect.getsourcelines(child_runner._run_child)
    cleanup_line = first_line + next(
        i for i, line in enumerate(source) if line.strip() == "if child is not None:"
    )
    def trace(frame, event, arg):
        if frame.f_code is child_runner._run_child.__code__:
            if event == "line" and frame.f_lineno == cleanup_line:
                sys.settrace(None)
                repeat_stop()
            return trace
    sys.settrace(trace)
"""


def _read(fd: int) -> bytes:
    assert select.select([fd], [], [], 15)[0], "conversion child did not report or exit"
    return os.read(fd, 1024)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL])
@pytest.mark.parametrize("conversion_stage", ["convert", "structure"])
def test_native_worker_shutdown_and_crash_keep_child_exclusion(
    tmp_path: Path, stop_signal: signal.Signals, conversion_stage: str
) -> None:
    _assert_worker_shutdown(tmp_path, stop_signal, conversion_stage)


@pytest.mark.parametrize("phase", ["before_deferral", "during_cleanup"])
@pytest.mark.parametrize("first_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("second_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("conversion_stage", ["convert", "structure"])
def test_repeated_stop_finishes_cleanup_with_first_exit_code(
    tmp_path: Path,
    phase: str,
    first_signal: signal.Signals,
    second_signal: signal.Signals,
    conversion_stage: str,
) -> None:
    _assert_worker_shutdown(tmp_path, first_signal, conversion_stage, (phase, second_signal))


def _assert_worker_shutdown(
    tmp_path: Path,
    stop_signal: signal.Signals,
    conversion_stage: str,
    repeat: tuple[str, signal.Signals] | None = None,
) -> None:
    for name in ("ready", "release"):
        os.mkfifo(tmp_path / name)
    ready = os.open(tmp_path / "ready", os.O_RDONLY | os.O_NONBLOCK)
    release = os.open(tmp_path / "release", os.O_RDWR | os.O_NONBLOCK)
    communicating: int | None = None
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    driver = _DRIVER
    extra_args: list[str] = []
    if repeat is not None:
        os.mkfifo(tmp_path / "communicating")
        communicating = os.open(tmp_path / "communicating", os.O_RDONLY | os.O_NONBLOCK)
        driver = driver.replace(
            'sys.exit(worker.main(["--once"]))',
            _REPEATED_STOP + '\nsys.exit(worker.main(["--once"]))',
        )
        extra_args = [repeat[0], str(repeat[1])]
    parent = subprocess.Popen(
        [sys.executable, "-c", driver, str(tmp_path), conversion_stage, "run", *extra_args],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: int | None = None
    exited = False
    try:
        child_pid = int(_read(ready).strip())
        if communicating is not None:
            assert _read(communicating) == b"x"
        store = JobStore(tmp_path / "jobs")
        with pytest.raises(WorkerAlreadyRunning), store.worker_lock():
            pass
        parent.send_signal(stop_signal)
        parent.wait(timeout=15)
        if stop_signal == signal.SIGKILL:
            # A crash cannot run cleanup. The *child* must still exclude a replacement.
            with pytest.raises(WorkerAlreadyRunning), store.worker_lock():
                pass
            refused = subprocess.run(
                [sys.executable, "-c", _DRIVER, str(tmp_path), conversion_stage, "restart"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert refused.returncode != 0 and "WorkerAlreadyRunning" in refused.stderr
            os.write(release, b"x")
        else:
            assert parent.returncode == 128 + stop_signal
        if repeat is not None:
            assert (tmp_path / "second-signal").read_text() == repeat[0]
            assert (tmp_path / "reaped").is_file(), "repeated stop bypassed child reaping"
            assert int((tmp_path / "reaped").read_text()) == -signal.SIGKILL
        assert _read(ready) == b"", "a conversion child survived orderly shutdown"
        exited = True
        with store.worker_lock():
            pass
        replacement = subprocess.run(
            [sys.executable, "-c", _DRIVER, str(tmp_path), conversion_stage, "restart"],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert replacement.returncode == 0, replacement.stderr
        assert "failed, left running by a stopped worker" in replacement.stderr
        assert "worker ready" in replacement.stderr
    finally:
        if parent.poll() is None:
            parent.kill()
        _, stderr = parent.communicate(timeout=15)
        if not exited:
            print(stderr.decode())
        if child_pid is not None and not exited:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
            assert _read(ready) == b""
        os.close(ready)
        os.close(release)
        if communicating is not None:
            os.close(communicating)
