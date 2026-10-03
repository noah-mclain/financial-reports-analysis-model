"""The health check, the way compose and `make docker-health` run it. Servers here are real
sockets the test owns."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import uvicorn
from fastapi import FastAPI

from fra_api.app import create_app
from fra_api.healthcheck import HealthCheckError, check, main
from fra_core.profile import Profile

Serve = Callable[[FastAPI], int]
ServeRaw = Callable[[bytes], int]

START_DEADLINE_SECONDS = 10


def unused_port() -> int:
    """A port that was free a moment ago and has nothing listening on it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def serve_app() -> Iterator[Callable[[FastAPI], int]]:
    """Serve an app over HTTP on a socket the test binds; returns its port."""
    stops: list[Callable[[], None]] = []

    def serve(app: FastAPI) -> int:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)

        def stop() -> None:
            server.should_exit = True
            if thread.is_alive():
                thread.join(5)
            sock.close()

        stops.append(stop)  # registered before the wait, so a failed start is cleaned up too
        thread.start()
        deadline = time.monotonic() + START_DEADLINE_SECONDS
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the test server did not start")
            time.sleep(0.01)
        return int(sock.getsockname()[1])

    yield serve
    for stop in stops:
        stop()


@pytest.fixture
def serve_raw() -> Iterator[Callable[[bytes], int]]:
    """Serve a fixed raw HTTP reply (or an immediate hang-up for ``b""``); returns the port."""
    servers: list[HTTPServer] = []

    def serve(reply: bytes) -> int:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if reply:
                    self.wfile.write(reply)
                self.close_connection = True

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        servers.append(server)
        return int(server.server_address[1])

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def reply(body: bytes, content_type: str = "application/json") -> bytes:
    head = f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode() + body


def test_check_returns_the_body_when_the_profile_matches(serve_app: Serve) -> None:
    port = serve_app(create_app(Profile.DOCKER))

    assert check(port, expect_profile=Profile.DOCKER)["profile"] == "docker"


def test_check_fails_when_the_profile_differs(serve_app: Serve) -> None:
    port = serve_app(create_app(Profile.DOCKER))

    with pytest.raises(HealthCheckError, match="native"):
        check(port, expect_profile=Profile.NATIVE)


def test_check_fails_when_nothing_is_listening() -> None:
    with pytest.raises(HealthCheckError, match="unreachable"):
        check(unused_port(), expect_profile=Profile.DOCKER)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (reply(b'{"status": "degraded", "profile": "docker"}'), "degraded"),
        (reply(b"<html>not json</html>", "text/html"), "unreachable or did not return"),
        (reply(b'["status", "ok"]'), "not a JSON object"),
        (reply(b'{"status": "\xff"}'), "unreachable or did not return"),
        (b"", "unreachable or did not return"),
        (b"not http at all\r\n\r\n", "unreachable or did not return"),
    ],
    ids=["status-not-ok", "not-json", "not-an-object", "not-utf8", "hang-up", "not-http"],
)
def test_check_fails_cleanly_on_a_bad_reply(serve_raw: ServeRaw, raw: bytes, message: str) -> None:
    with pytest.raises(HealthCheckError, match=message):
        check(serve_raw(raw), expect_profile=Profile.DOCKER)


def test_main_prints_the_body_and_exits_zero_when_healthy(
    serve_app: Serve, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = serve_app(create_app(Profile.DOCKER))
    monkeypatch.setattr("sys.argv", ["healthcheck", str(port), "--profile", "docker"])

    assert main() == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "docker"


def test_main_exits_one_and_says_why_when_unhealthy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["healthcheck", str(unused_port()), "--profile", "docker"])

    assert main() == 1
    assert "unhealthy" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["8000"], ["8000", "--profile", "kubernetes"]])
def test_main_rejects_missing_or_unknown_arguments(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["healthcheck", *argv])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 2
