"""Behavioral checks for the native smoke runner using fake services and HTTP responses."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_job.sh"
SHA = "a" * 64
JOB_ID = "job-test"


@pytest.fixture(autouse=True)
def cleanup_fake_services(tmp_path: Path) -> Iterator[None]:
    yield
    # Run after assertions, so fixture cleanup cannot hide a script cleanup defect.
    for name in ("api.pid", "worker.pid"):
        for pid_file in tmp_path.rglob(name):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)


def fake_tools(tmp_path: Path, *, final: str = "completed", mode: str = "ok") -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError('fake service synchronization timed out')
        time.sleep(0.01)
def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False
args = sys.argv[1:]
count_file = pathlib.Path(os.environ['FAKE_COUNT'])
count = int(count_file.read_text()) if count_file.exists() else 0
count_file.write_text(str(count + 1))
url = next(arg for arg in args if arg.startswith('http://'))
mode = os.environ['FAKE_MODE']
if url.endswith('/health'):
    if count == 0: sys.exit(22)
    for name in ('FAKE_API_PID', 'FAKE_WORKER_PID'):
        wait_for(lambda: pathlib.Path(os.environ[name]).exists())
    if mode == 'worker_dies':
        wait_for(lambda: gone(int(pathlib.Path(os.environ['FAKE_WORKER_PID']).read_text())))
    sys.exit(0)
if mode in ('transport', 'timeout', 'stubborn_child') and url.endswith('/events'):
    if mode == 'timeout' and args[args.index('--max-time') + 1] != '1':
        sys.exit(90)
    sys.exit(28)
if url.endswith('/documents'):
    print(json.dumps({'sha256': 'bad' if mode == 'bad_sha' else 'a' * 64}))
elif url.endswith('/analyses'):
    print(json.dumps({'id': 'job-test'}))
elif url.endswith('/events'):
    print('data: ' + json.dumps({'kind': os.environ['FAKE_FINAL'], 'stage': 'structure'}))
elif url.endswith('/jobs/job-test'):
    print(json.dumps({'status': os.environ['FAKE_FINAL'], 'stage': 'structure', 'error': 'fixture failure'}))
elif url.endswith('/analyses/job-test'):
    if mode in ('worker_dies_late', 'api_dies_late'):
        name = 'FAKE_WORKER_PID' if mode == 'worker_dies_late' else 'FAKE_API_PID'
        pid = int(pathlib.Path(os.environ[name]).read_text())
        os.kill(pid, signal.SIGTERM)
        wait_for(lambda: gone(pid))
    body = {'job_id': 'job-test', 'upload_sha256': 'a' * 64, 'page_count': 1,
            'language': 'en', 'statements': [], 'metrics': []}
    if mode == 'bad_analysis': body = []
    print(json.dumps(body))
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!{sys.executable}\n"
        """import os, pathlib, signal, subprocess, sys, time
worker = sys.argv[2] == 'fra_worker.main'
if worker and os.environ['FAKE_MODE'] == 'stubborn_child':
    child = subprocess.Popen([sys.executable, '-c', '''
import os, pathlib, signal
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(os.environ['FAKE_CHILD_PID']).write_text(str(os.getpid()))
while True: signal.pause()
'''])
    deadline = time.monotonic() + 3
    while not pathlib.Path(os.environ['FAKE_CHILD_PID']).exists():
        if time.monotonic() >= deadline: raise RuntimeError('child did not start')
        time.sleep(0.01)
pathlib.Path(os.environ['FAKE_WORKER_PID' if worker else 'FAKE_API_PID']).write_text(str(os.getpid()))
if worker and os.environ['FAKE_MODE'] == 'worker_dies': sys.exit(9)
while True: signal.pause()
""",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return fake_bin, python.parent.parent


def run_smoke(
    tmp_path: Path,
    *,
    final: str = "completed",
    mode: str = "ok",
    log_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"fixture")
    fake_bin, venv = fake_tools(tmp_path, final=final, mode=mode)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "VENV": str(venv),
        "LOG_DIR": str(log_dir or tmp_path / "run"),
        "PORT": "18765",
        "TIMEOUT": "1",
        "FAKE_MODE": mode,
        "FAKE_FINAL": final,
        "FAKE_COUNT": str(tmp_path / "curl-count"),
        "FAKE_API_PID": str(tmp_path / "api.pid"),
        "FAKE_WORKER_PID": str(tmp_path / "worker.pid"),
        "FAKE_CHILD_PID": str(tmp_path / "child.pid"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(pdf)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_final_states_and_saved_analysis(tmp_path: Path) -> None:
    completed = run_smoke(tmp_path / "completed")
    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "completed/run/analysis.json").read_text())["job_id"] == JOB_ID
    for pid_file in (tmp_path / "completed/api.pid", tmp_path / "completed/worker.pid"):
        if pid_file.exists():
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    review = run_smoke(tmp_path / "review", final="needs_review")
    assert review.returncode == 2, review.stderr
    assert (tmp_path / "review/run/analysis.json").is_file()

    failed = run_smoke(tmp_path / "failed", final="failed")
    assert failed.returncode == 1
    assert "FAILED at stage" in failed.stderr


def test_rejects_bad_upload_and_malformed_analysis(tmp_path: Path) -> None:
    bad_sha = run_smoke(tmp_path / "sha", mode="bad_sha")
    assert bad_sha.returncode == 1
    assert "valid sha256" in bad_sha.stderr

    malformed = run_smoke(tmp_path / "analysis", mode="bad_analysis")
    assert malformed.returncode == 1
    assert "does not match" in malformed.stderr


def test_stream_failure_and_worker_startup_death(tmp_path: Path) -> None:
    stream = run_smoke(tmp_path / "stream", mode="transport")
    assert stream.returncode == 1
    assert "event stream failed" in stream.stderr

    timeout = run_smoke(tmp_path / "timeout", mode="timeout")
    assert timeout.returncode == 1
    assert "event stream failed" in timeout.stderr

    worker = run_smoke(tmp_path / "worker", mode="worker_dies")
    assert worker.returncode == 1
    assert "worker exited" in worker.stderr


def test_existing_state_is_refused_without_overwriting(tmp_path: Path) -> None:
    run = tmp_path / "existing"
    state = run / "state"
    state.mkdir(parents=True)
    marker = state / "keep"
    marker.write_text("prior run")
    result = run_smoke(tmp_path / "occupied", log_dir=run)
    assert result.returncode == 1
    assert "state already exists" in result.stderr
    assert marker.read_text() == "prior run"


@pytest.mark.parametrize("service", ["worker", "api"])
def test_service_death_before_final_verdict_fails(tmp_path: Path, service: str) -> None:
    result = run_smoke(tmp_path, mode=f"{service}_dies_late")
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{service} exited" in result.stderr.lower()
    assert not (tmp_path / "run/analysis.json").exists()


def test_timeout_kills_owned_descendant_ignoring_term(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_smoke(tmp_path, mode="stubborn_child")
    assert result.returncode == 1, result.stderr
    assert "event stream failed" in result.stderr
    assert time.monotonic() - started < 8
    for name in ("api.pid", "worker.pid", "child.pid"):
        pid = int((tmp_path / name).read_text())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, f"{name} process {pid} survived cleanup"
            time.sleep(0.01)


def test_preflight_rejects_missing_venv_and_invalid_port(tmp_path: Path) -> None:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"fixture")
    missing = subprocess.run(
        ["bash", str(SCRIPT), str(pdf)],
        cwd=tmp_path,
        env={**os.environ, "VENV": str(tmp_path / "missing")},
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert missing.returncode == 1 and "no executable" in missing.stderr

    invalid = subprocess.run(
        ["bash", str(SCRIPT), str(pdf)],
        cwd=tmp_path,
        env={**os.environ, "PORT": "70000", "VENV": str(tmp_path / "missing")},
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert invalid.returncode == 1 and "PORT must" in invalid.stderr
