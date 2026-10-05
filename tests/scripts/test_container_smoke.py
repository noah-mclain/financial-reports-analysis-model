from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci" / "container-smoke.sh"

# A stateful Docker protocol double: no daemon, images or application imports are needed.
DOCKER = r"""
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state["commands"].append(args)
code = 0
if args[0] == "compose":
    args = args[1:]
    project = "fra"
    if args[:1] == ["--project-name"]:
        project, args = args[1], args[2:]
    if args[:1] == ["--profile"]:
        args = args[2:]
    command = args[0]
    if project == "fra" and command in ("up", "stop", "down"):
        state["owner_running"] = False
    if command == "up":
        state["created"] = True
        state["running"] = True
    elif command == "stop":
        state["running"] = False
    elif command == "down" and state.get("fail") != "down":
        state["created"] = False
    elif command == "ps":
        services = [s for s in ("api", "worker") if s in args]
        for service in services:
            print(service + "-id")
    if command == state.get("fail"):
        print("simulated " + command + " failure", file=sys.stderr)
        code = 42
elif args[0] in ("ps", "network", "volume"):
    if state.get("collision"):
        print("preexisting-resource")
elif args[0] == "inspect":
    service = args[-1].removesuffix("-id")
    if state["running"]:
        if "Health" in args[2]:
            print(state.get("health", "healthy"))
        else:
            print(state.get("startup", "running 0 false"))
    else:
        print(state.get(service, "exited 0 false"))
else:
    print("unexpected Docker command: " + repr(args), file=sys.stderr)
    code = 99
path.write_text(json.dumps(state))
sys.exit(code)
"""


def run_smoke(
    tmp_path: Path, **options: str | bool
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], list[list[str]]]:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {"commands": [], "owner_running": True, "created": False, "running": False, **options}
        )
    )
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + DOCKER)
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=SCRIPT.parents[2],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_STATE": str(state_path),
            "FRA_API_PORT": "8000",
            "FRA_LLM_PORT": "8080",
            "COMPOSE_PROJECT_NAME": "fra",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    state = json.loads(state_path.read_text())
    return result, state, state["commands"]


@pytest.mark.parametrize("service", ["api", "worker"])
@pytest.mark.parametrize("exit_code", [0, 143])
def test_accepts_normal_stop(tmp_path: Path, service: str, exit_code: int) -> None:
    result, state, _ = run_smoke(tmp_path, **{service: f"exited {exit_code} false"})
    assert result.returncode == 0, result.stderr
    assert state["created"] is False


@pytest.mark.parametrize("service", ["api", "worker"])
@pytest.mark.parametrize(
    "status",
    ["exited 137 false", "exited 143 true", "exited 1 false", "dead 0 false", "running 0 false"],
)
def test_rejects_bad_shutdown(tmp_path: Path, service: str, status: str) -> None:
    result, state, _ = run_smoke(tmp_path, **{service: status})
    assert result.returncode != 0
    assert "did not stop gracefully" in result.stderr
    assert state["created"] is False


def test_scopes_all_compose_commands_and_preserves_owner(tmp_path: Path) -> None:
    result, state, commands = run_smoke(tmp_path)
    assert result.returncode == 0, result.stderr
    compose = [args for args in commands if args[0] == "compose"]
    assert all(args[1] == "--project-name" for args in compose)
    projects = {args[2] for args in compose}
    assert len(projects) == 1
    assert "fra" not in projects
    assert state["owner_running"] is True
    assert any(args[0] == "ps" and "--filter" in args for args in commands)


def test_each_invocation_uses_a_different_project(tmp_path: Path) -> None:
    _, _, first = run_smoke(tmp_path)
    _, _, second = run_smoke(tmp_path)
    assert next(args[2] for args in first if args[0] == "compose") != next(
        args[2] for args in second if args[0] == "compose"
    )


def test_collision_is_rejected_without_teardown(tmp_path: Path) -> None:
    result, state, commands = run_smoke(tmp_path, collision=True)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert not any(args[0] == "compose" for args in commands)
    assert state["owner_running"] is True


@pytest.mark.parametrize("command", ["logs", "stop", "down"])
def test_cleanup_failure_fails_successful_smoke(tmp_path: Path, command: str) -> None:
    result, _, commands = run_smoke(tmp_path, fail=command)
    assert result.returncode != 0
    assert any(args[0] == "compose" and "down" in args for args in commands)


def test_primary_failure_survives_cleanup_failure(tmp_path: Path) -> None:
    result, _, _ = run_smoke(tmp_path, worker="exited 137 false", fail="down")
    assert result.returncode == 1
    assert "simulated down failure" in result.stderr


@pytest.mark.parametrize("command", ["config", "build", "up", "exec"])
def test_early_failure_only_cleans_owned_resources(tmp_path: Path, command: str) -> None:
    result, state, commands = run_smoke(tmp_path, fail=command)
    assert result.returncode == 42
    cleanup = [args for args in commands if args[0] == "compose" and "down" in args]
    assert bool(cleanup) == (command in ("up", "exec"))
    assert state["created"] is False
    assert state["owner_running"] is True


@pytest.mark.parametrize("options", [{"health": "unhealthy"}, {"startup": "exited 143 false"}])
def test_shutdown_requires_healthy_running_startup(tmp_path: Path, options: dict[str, str]) -> None:
    result, state, _ = run_smoke(tmp_path, **options)
    assert result.returncode != 0
    assert state["created"] is False
