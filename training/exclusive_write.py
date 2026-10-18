"""Create files that must not exist: all of them or none, never over an existing path."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dataset_contracts import DatasetError


def write_new(files: Mapping[Path, str | bytes]) -> None:
    """Create every file exclusively. A path that exists, even as a dangling link, stops the
    call with a message; a failure of any kind removes the files this call created."""
    created: list[Path] = []
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) else content
            with path.open("xb") as handle:
                created.append(path)
                handle.write(data)
    except FileExistsError as exc:
        _remove(created)
        raise DatasetError(f"{exc.filename} already exists; refusing to overwrite") from exc
    except BaseException:
        _remove(created)
        raise


def _remove(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
