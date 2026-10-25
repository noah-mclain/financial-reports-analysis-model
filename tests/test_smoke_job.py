"""Behavioral checks for the native smoke runner using fake services and HTTP responses."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_job.sh"
SHA = "a" * 64
JOB_ID = "job-test"
# The script's single shared TERM grace period, plus scheduling and reaping margin.
CLEANUP_SETTING = re.search(r"^CLEANUP_GRACE_SECONDS=(\d+)$", SCRIPT.read_text(), re.M)
assert CLEANUP_SETTING is not None, "smoke script must document its cleanup grace period"
CLEANUP_GRACE = int(CLEANUP_SETTING[1])
CLEANUP_TIMEOUT = CLEANUP_GRACE + 3


def default_sigint() -> None:
    # A background-started test run hands down an ignored SIGINT, which bash cannot trap.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@pytest.fixture(autouse=True)
def cleanup_fake_services(tmp_path: Path) -> Iterator[None]:
    yield
    # Assert before test-owned cleanup, so a leaked service cannot pass unnoticed.
    try:
        for root_file in tmp_path.rglob("socket-root"):
            assert_owned_processes_gone(root_file.parent)
    finally:
        for name in ("api.pid", "worker.pid", "stream.pid", "launch.pid"):
            for pid_file in tmp_path.rglob(name):
                with contextlib.suppress(ProcessLookupError):
                    group_file = pid_file.with_suffix(".pgid")
                    pgid = int((group_file if group_file.exists() else pid_file).read_text())
                    os.killpg(pgid, signal.SIGKILL)
        for root_file in tmp_path.rglob("socket-root"):
            shutil.rmtree(Path(root_file.read_text()))


def fake_tools(tmp_path: Path, *, final: str = "completed", mode: str = "ok") -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        """
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
url = next(arg for arg in args if arg.startswith('http://'))
mode = os.environ['FAKE_MODE']
socket = pathlib.Path(args[args.index('--unix-socket') + 1]) if '--unix-socket' in args else None
with open(os.environ['FAKE_REQUESTS'], 'a') as log:
    log.write(json.dumps({'args': args, 'mode': socket.parent.stat().st_mode & 0o777 if socket else None}) + '\\n')
if socket is None and mode == 'foreign_tcp':
    if url.endswith('/documents'): pathlib.Path(os.environ['FAKE_FOREIGN_UPLOAD']).touch()
    print(json.dumps({'sha256': 'a' * 64, 'id': 'foreign-job'}))
    sys.exit(0)
if url.endswith('/health'):
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
    pathlib.Path(os.environ['FAKE_UPLOAD']).touch()
    print(json.dumps({'sha256': 'bad' if mode == 'bad_sha' else 'a' * 64}))
elif url.endswith('/analyses'):
    print(json.dumps({'id': 'job-test'}))
elif url.endswith('/events'):
    body = {'job_id': 'job-test', 'kind': os.environ['FAKE_FINAL'], 'stage': 'structure'}
    if mode == 'event_missing': del body['job_id']
    if mode == 'event_wrong': body['job_id'] = 'other-job'
    if mode == 'event_kind': body['kind'] = 'running'
    if mode.startswith('event_'):
        print('data: ' + json.dumps({**body, 'kind': body['kind'] if mode == 'event_kind' else 'started'}))
    print('data: ' + json.dumps(body), flush=True)
    if mode in ('blocking_stream', 'cleanup_signals'):
        if mode == 'cleanup_signals': signal.signal(signal.SIGTERM, lambda *args: None)
        stream_pid = pathlib.Path(os.environ['FAKE_STREAM_PID'])
        stream_pid.with_suffix('.pgid').write_text(str(os.getpgrp()))
        stream_pid.write_text(str(os.getpid()))
        while True: signal.pause()
elif url.endswith('/jobs/job-test'):
    body = {'id': 'job-test', 'upload_sha256': 'a' * 64,
            'status': os.environ['FAKE_FINAL'], 'stage': 'structure', 'error': 'fixture failure'}
    if mode == 'job_missing': del body['id']
    if mode == 'job_wrong': body['id'] = 'other-job'
    if mode == 'sha_missing': del body['upload_sha256']
    if mode == 'sha_wrong': body['upload_sha256'] = 'b' * 64
    if mode == 'status_wrong': body['status'] = 'failed'
    print(json.dumps(body))
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
        """import json, os, pathlib, signal, subprocess, sys, time, tomllib
worker = sys.argv[2] == 'fra_worker.main'
if not worker:
    pathlib.Path(os.environ['FAKE_API_ARGS']).write_text(json.dumps(sys.argv[1:]))
if os.environ['FAKE_MODE'] == 'cleanup_signals':
    def ignore_term(signum, frame):
        if worker: pathlib.Path(os.environ['FAKE_CLEANING']).touch()
    signal.signal(signal.SIGTERM, ignore_term)
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
pid_file = pathlib.Path(os.environ['FAKE_WORKER_PID' if worker else 'FAKE_API_PID'])
if os.environ['FAKE_MODE'] == 'config_isolation':
    root = pathlib.Path(os.environ['FRA_ROOT'])
    source = pathlib.Path(os.environ.get('FRA_INGEST_CONFIG', root / 'configs/ingest.toml'))
    artifact_root = pathlib.Path(tomllib.loads(source.read_text())['artifacts']['root'])
    if not artifact_root.is_absolute(): artifact_root = root / artifact_root
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / ('worker' if worker else 'api')).touch()
    pid_file.with_suffix('.env.json').write_text(json.dumps({
        'root': str(root), 'config': os.environ.get('FRA_INGEST_CONFIG'),
        'effective': os.environ.get('FRA_INGEST_EFFECTIVE_CONFIG')}))
pid_file.write_text(str(os.getpid()))
if worker and os.environ['FAKE_MODE'] == 'worker_dies': sys.exit(9)
while True: signal.pause()
""",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return fake_bin, python.parent.parent


def prepare_smoke(
    tmp_path: Path,
    *,
    final: str = "completed",
    mode: str = "ok",
    log_dir: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"fixture")
    fake_bin, venv = fake_tools(tmp_path, final=final, mode=mode)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "VENV": str(venv),
        "LOG_DIR": str(log_dir or tmp_path / "run"),
        "TMPDIR": tempfile.mkdtemp(prefix="smoke-test-", dir="/tmp"),
        "TIMEOUT": "1",
        "FAKE_MODE": mode,
        "FAKE_FINAL": final,
        "FAKE_API_PID": str(tmp_path / "api.pid"),
        "FAKE_WORKER_PID": str(tmp_path / "worker.pid"),
        "FAKE_CHILD_PID": str(tmp_path / "child.pid"),
        "FAKE_STREAM_PID": str(tmp_path / "stream.pid"),
        "FAKE_UPLOAD": str(tmp_path / "uploaded"),
        "FAKE_FOREIGN_UPLOAD": str(tmp_path / "foreign-uploaded"),
        "FAKE_REQUESTS": str(tmp_path / "requests.jsonl"),
        "FAKE_API_ARGS": str(tmp_path / "api-args.json"),
        "FAKE_CLEANING": str(tmp_path / "cleaning"),
    }
    (tmp_path / "socket-root").write_text(env["TMPDIR"])
    return ["bash", str(SCRIPT), str(pdf)], env


def run_smoke(
    tmp_path: Path,
    *,
    final: str = "completed",
    mode: str = "ok",
    log_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command, env = prepare_smoke(tmp_path, final=final, mode=mode, log_dir=log_dir)
    return subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=CLEANUP_TIMEOUT + 4,
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
    assert time.monotonic() - started < CLEANUP_TIMEOUT + 3
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


def test_preflight_rejects_missing_venv_and_invalid_timeout(tmp_path: Path) -> None:
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
        env={**os.environ, "TIMEOUT": "0", "VENV": str(tmp_path / "missing")},
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert invalid.returncode == 1 and "TIMEOUT must" in invalid.stderr


def test_inherited_artifact_configuration_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "external-artifacts"
    sentinel.mkdir()
    config = tmp_path / "external.toml"
    config.write_text(f'[artifacts]\nroot = "{sentinel}"\n')
    monkeypatch.setenv("FRA_INGEST_CONFIG", str(config))
    monkeypatch.setenv("FRA_INGEST_EFFECTIVE_CONFIG", json.dumps({"artifact_root": str(sentinel)}))
    result = run_smoke(tmp_path / "isolated", mode="config_isolation")
    assert result.returncode == 0, result.stderr
    for service in ("api", "worker"):
        observed = json.loads((tmp_path / f"isolated/{service}.env.json").read_text())
        assert observed["config"] is None
        assert observed["effective"] is None
        state = tmp_path / "isolated/run/state"
        assert observed["root"] == str(state)
        assert (state / "configs/ingest.toml").is_file()
        assert (state / "var/artifacts" / service).is_file()
    assert not list(sentinel.iterdir())


def test_all_requests_use_private_socket_even_with_foreign_tcp_api(tmp_path: Path) -> None:
    result = run_smoke(tmp_path, mode="foreign_tcp", log_dir=tmp_path / ("deep" * 30))
    assert result.returncode == 0, result.stdout + result.stderr
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert len(requests) == 6
    sockets = set()
    for request in requests:
        args = request["args"]
        assert "--unix-socket" in args
        socket = Path(args[args.index("--unix-socket") + 1])
        sockets.add(socket)
        assert socket.parent.parent == Path((tmp_path / "socket-root").read_text())
        assert request["mode"] == 0o700
        assert [arg for arg in args if arg.startswith("http://")] == [
            next(arg for arg in args if arg.startswith("http://localhost/"))
        ]
    assert len(sockets) == 1
    api_args = json.loads((tmp_path / "api-args.json").read_text())
    assert "--port" not in api_args and "--host" not in api_args
    assert api_args[api_args.index("--uds") + 1] == str(next(iter(sockets)))
    assert (tmp_path / "uploaded").exists()
    assert not (tmp_path / "foreign-uploaded").exists()
    assert not next(iter(sockets)).parent.exists()


@pytest.mark.parametrize("failure", ["long_path", "create", "permissions"])
def test_socket_directory_setup_fails_closed(tmp_path: Path, failure: str) -> None:
    command, env = prepare_smoke(tmp_path)
    if failure == "long_path":
        directory = tmp_path / ("long" * 30)
        directory.mkdir()
        env["TMPDIR"] = str(directory)
    else:
        tool = tmp_path / "bin" / ("mktemp" if failure == "create" else "chmod")
        tool.write_text("#!/bin/sh\nexit 1\n")
        tool.chmod(0o755)
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=CLEANUP_TIMEOUT
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "socket path" if failure == "long_path" else "private socket directory"
    ) in result.stderr
    assert not (tmp_path / "api.pid").exists()
    assert not (tmp_path / "worker.pid").exists()
    assert not (tmp_path / "uploaded").exists()
    assert not list(Path(env["TMPDIR"]).iterdir())


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} was not created"
        time.sleep(0.01)


def assert_owned_processes_gone(tmp_path: Path) -> None:
    pids = {int(path.read_text()) for path in tmp_path.glob("*.pid")}
    groups = {int(path.read_text()) for path in tmp_path.glob("*.pgid")}
    groups.update(
        int(path.read_text())
        for name in ("api.pid", "worker.pid", "launch.pid")
        if (path := tmp_path / name).exists()
    )
    deadline = time.monotonic() + 2
    while pids or groups:
        for pid in pids.copy():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pids.remove(pid)
        for pgid in groups.copy():
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                groups.remove(pgid)
        assert time.monotonic() < deadline, f"surviving processes {pids}, groups {groups}"
        if pids or groups:
            time.sleep(0.01)
    assert not list(Path((tmp_path / "socket-root").read_text()).iterdir())


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize(
    "boundary",
    [
        "set -m",  # Before the first child.
        "API_PID=$!",  # Forked, PID not yet recorded.
        '"$VENV/bin/python" -m fra_worker.main*',  # API recorded, before worker.
        "WORKER_PID=$!",  # Worker forked, PID not yet recorded.
        "ready=0",  # Worker recorded, before stream.
        "STREAM_PID=$!",  # Stream forked, PID not yet recorded.
        'wait "$STREAM_PID"',  # Stream recorded, before wait.
    ],
)
def test_cancellation_at_launch_boundaries(
    tmp_path: Path, boundary: str, signum: signal.Signals
) -> None:
    command, env = prepare_smoke(tmp_path, mode="blocking_stream")
    hook = tmp_path / "signals.bash"
    hook.write_text(
        """trap 'if [[ "$BASH_SUBSHELL" == 0 && "$BASH_COMMAND" == $INJECT_BOUNDARY && ! -f "$INJECT_MARKER" ]]; then
  if [[ -n "$INJECT_READY" ]]; then
    deadline=$((SECONDS + 3))
    while [[ ! -f "$INJECT_READY" ]]; do
      (( SECONDS < deadline )) || exit 98
      sleep 0.01
    done
    printf "%s\\n" "$!" > "$INJECT_PID"
  fi
  touch "$INJECT_MARKER"
  kill -s "$INJECT_SIGNAL" "$$"
fi' DEBUG
"""
    )
    env.update(
        BASH_ENV=str(hook),
        INJECT_BOUNDARY=boundary,
        INJECT_MARKER=str(tmp_path / "injected"),
        INJECT_SIGNAL=str(int(signum)),
        INJECT_PID=str(tmp_path / "launch.pid"),
        INJECT_READY=str(tmp_path / f"{boundary.split('_')[0].lower()}.pid")
        if boundary.endswith("_PID=$!")
        else "",
    )
    output = tmp_path / "output"
    with (
        output.open("w") as log,
        subprocess.Popen(
            command,
            env=env,
            stdout=log,
            stderr=log,
            text=True,
            start_new_session=True,
            preexec_fn=default_sigint,
        ) as process,
    ):
        try:
            process.wait(timeout=CLEANUP_TIMEOUT + 4)
            assert process.returncode == 128 + signum, output.read_text()
            marker = tmp_path / "injected"
            assert marker.exists(), "launch boundary was not reached"
            assert_owned_processes_gone(tmp_path)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


def test_repeated_signals_cannot_abort_cleanup(tmp_path: Path) -> None:
    command, env = prepare_smoke(tmp_path, mode="cleanup_signals")
    with subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=default_sigint,
    ) as process:
        try:
            wait_for_file(tmp_path / "stream.pid")
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            wait_for_file(tmp_path / "cleaning")
            process.send_signal(signal.SIGTERM)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=CLEANUP_TIMEOUT)
            assert process.returncode == 143, stdout + stderr
            assert time.monotonic() - started < CLEANUP_TIMEOUT
            assert_owned_processes_gone(tmp_path)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_cancellation_interrupts_blocked_stream(tmp_path: Path, signum: signal.Signals) -> None:
    command, env = prepare_smoke(tmp_path, mode="blocking_stream")
    env["TIMEOUT"] = "90"
    with subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=default_sigint,
    ) as process:
        try:
            wait_for_file(tmp_path / "stream.pid")
            wait_for_file(tmp_path / "run/final-event")
            groups = {
                name: int((tmp_path / name).read_text())
                for name in ("api.pid", "worker.pid", "stream.pgid")
            }
            started = time.monotonic()
            process.send_signal(signum)
            try:
                stdout, stderr = process.communicate(timeout=CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pytest.fail("cancellation waited for the blocked event stream")
            assert process.returncode == 128 + signum, stdout + stderr
            assert time.monotonic() - started < CLEANUP_TIMEOUT
            assert "event completed" in stdout
            assert "data:" in (tmp_path / "run/events.sse").read_text()
            assert (tmp_path / "run/events.err").is_file()
            for name, pgid in groups.items():
                deadline = time.monotonic() + 2
                while True:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    assert time.monotonic() < deadline, f"{name} group survived cancellation"
                    time.sleep(0.01)
        finally:
            if process.poll() is None:
                # The original foreground pipeline needs test-owned cleanup on failure.
                stream = tmp_path / "stream.pid"
                if stream.exists():
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(int(stream.with_suffix(".pgid").read_text()), signal.SIGKILL)
                process.kill()
                process.communicate(timeout=3)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("event_missing", "event job_id"),
        ("event_wrong", "event job_id"),
        ("event_kind", "event kind"),
        ("job_missing", "final job id"),
        ("job_wrong", "final job id"),
        ("sha_missing", "final job upload_sha256"),
        ("sha_wrong", "final job upload_sha256"),
        ("status_wrong", "job endpoint says"),
    ],
)
def test_rejects_wrong_or_missing_identity(tmp_path: Path, mode: str, message: str) -> None:
    result = run_smoke(tmp_path, mode=mode)
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    assert not (tmp_path / "run/analysis.json").exists()


def test_make_smoke_passes_shell_metacharacters_in_the_path_literally(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / ".env").write_text("FRA_API_PORT=8000\nFRA_LLM_PORT=8001\n")
    received = tmp_path / "received"
    stub = tmp_path / "scripts" / "smoke_job.sh"
    stub.write_text(f'#!/usr/bin/env bash\nprintf %s "$1" > {shlex.quote(str(received))}\n')
    marker = tmp_path / "marker"
    pdf = f"a `touch {shlex.quote(str(marker))}` 'q' \"d\"; b.pdf"

    subprocess.run(["make", "smoke", f"PDF={pdf}"], cwd=tmp_path, check=True)

    assert received.read_text() == pdf
    assert not marker.exists()
