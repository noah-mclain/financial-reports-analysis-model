"""Stdlib-only model configuration and server command, also used in the CPU image."""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path


def load_values(config: Path, root: Path) -> dict[str, object]:
    values: dict[str, object] = tomllib.loads(config.read_text(encoding="utf-8"))
    raw = values["model_path"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{config}: model_path must be a nonempty string")
    path = Path(raw)
    # Both smoke state and the checkout may link to one shared model directory.
    # Resolve without requiring the weights when normalization is disabled.
    values["model_path"] = (path if path.is_absolute() else root / path).resolve()
    return values


def server_command(
    model_path: Path, *, executable: str, host: str, port: int, gpu_layers: int
) -> list[str]:
    if not 1 <= port <= 65535:
        raise ValueError("FRA_LLM_PORT must be a port from 1 to 65535")
    return [
        executable,
        "-m",
        str(model_path),
        "--alias",
        str(model_path),
        "--host",
        host,
        "--port",
        str(port),
        "--jinja",
        "-ngl",
        str(gpu_layers),
        "-c",
        "2048",
        "-np",
        "1",
        "--cache-ram",
        "0",
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    path = load_values(args.config, args.root)["model_path"]
    assert isinstance(path, Path)
    command = server_command(
        path, executable="/app/llama-server", host="0.0.0.0", port=args.port, gpu_layers=0
    )
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
