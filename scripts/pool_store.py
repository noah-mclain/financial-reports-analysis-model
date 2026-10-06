"""The files behind the pool registry: read, write, lock, and the recorded document metadata.

``fra_core.pools`` holds the rules and parses an already loaded dict; this is the one place that
touches the disk for it. Shared by ``scripts/corpus.py``, ``scripts/argaam_listing.py`` and
``training/sources/sec_fsds.py``, which finds it with ``PYTHONPATH=scripts`` (see the Makefile).
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import yaml

from fra_core.pools import PoolRegistry, document_rows

REGISTRY_RELATIVE_PATH = Path("var/issuer-pools.json")
SEC_OUTPUT_RELATIVE_PATH = Path("training/data/sec_fsds")


def load_registry(path: Path, sec_outputs: Iterable[Path] = ()) -> PoolRegistry:
    registry = PoolRegistry()
    if path.exists():
        registry = PoolRegistry.from_metadata(
            json.loads(path.read_text(encoding="utf-8")), str(path)
        )
    registry.require_sec_outputs((output.name for output in sec_outputs), str(path))
    return registry


def save_registry(registry: PoolRegistry, path: Path) -> None:
    """Atomic metadata replacement; command writers must hold ``locked_registry``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(registry.metadata(), indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def locked_registry(path: Path, sec_outputs: Iterable[Path] = ()) -> Iterator[PoolRegistry]:
    """Serialize command writers; no metadata is saved implicitly on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield load_registry(path, sec_outputs)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def record_pdf_metadata(registry: PoolRegistry, corpus_dir: Path, golden_manifest: Path) -> None:
    """Load recorded candidate/deferred pools and verify fetched pools by document id.

    These are metadata files only. Golden identities are dev; no document bytes or
    label datasets are opened. A fetched row without identity metadata needs review.
    """

    def documents(path: Path, *, optional: bool = False) -> list[Mapping[str, object]]:
        if optional and not path.exists():
            return []
        return document_rows(
            yaml.safe_load(path.read_text(encoding="utf-8")), str(path), optional=optional
        )

    registry.record_golden(documents(golden_manifest))
    recorded = documents(corpus_dir / "candidates.yaml") + documents(
        corpus_dir / "deferred.yaml", optional=True
    )
    registry.record_documents(recorded)
    fetched = corpus_dir / "fetched.yaml"
    if fetched.exists():
        registry.record_fetched(
            yaml.safe_load(fetched.read_text(encoding="utf-8")), str(fetched), recorded
        )
