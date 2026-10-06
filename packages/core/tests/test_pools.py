"""The pool contract works on values: parsing a metadata dict, document rows, no file IO."""

import ast
from pathlib import Path

import pytest

import fra_core.pools as pools
from fra_core.pools import Identity, PoolError, PoolRegistry, Source


def test_metadata_round_trips_through_from_metadata() -> None:
    registry = PoolRegistry(ambiguous_names=["acme"])
    registry.assign(Identity("Acme Widgets Inc.", "0000001111"), Source.SEC)
    registry.sec_outputs.add("labels-2025q1.jsonl.gz")
    restored = PoolRegistry.from_metadata(registry.metadata(), "pools.json")
    assert restored.metadata() == registry.metadata()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"version": True, "assignments": [], "sec_outputs": []},
        {"version": 1, "assignments": {}, "sec_outputs": []},
        {"version": 1, "assignments": [], "sec_outputs": [], "ambiguous_names": {}},
        {"version": 1, "assignments": [], "sec_outputs": ["labels-2025.jsonl.gz"]},
    ],
)
def test_from_metadata_names_the_source_in_every_error(data: object) -> None:
    with pytest.raises(PoolError) as error:
        PoolRegistry.from_metadata(data, "pools.json")
    assert str(error.value).startswith("pools.json: ")


def test_require_sec_outputs_lists_the_missing_ones_sorted() -> None:
    registry = PoolRegistry()
    registry.sec_outputs.add("labels-2025q1.jsonl.gz")
    registry.require_sec_outputs(["labels-2025q1.jsonl.gz"], "pools.json")
    with pytest.raises(PoolError, match=r"pools.json: .*: labels-2025q2.*, labels-2025q3"):
        registry.require_sec_outputs(
            ["labels-2025q3.jsonl.gz", "labels-2025q2.jsonl.gz"], "pools.json"
        )


def test_document_rows_validates_the_loaded_mapping() -> None:
    rows = [{"issuer": "Acme", "pool": "train"}]
    assert pools.document_rows({"documents": rows}, "c.yaml") == rows
    assert pools.document_rows({}, "d.yaml", optional=True) == []
    with pytest.raises(PoolError, match=r"c\.yaml: expected document metadata mapping"):
        pools.document_rows([], "c.yaml")
    with pytest.raises(PoolError, match=r"c\.yaml: expected documents list"):
        pools.document_rows({"documents": ["x"]}, "c.yaml")


def test_core_pools_does_no_file_io_and_no_locking() -> None:
    """The contract stays pure; reading, writing and locking live in scripts/pool_store.py."""
    tree = ast.parse(Path(pools.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"fcntl", "json", "yaml", "os", "pathlib", "contextlib"}
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    assert not called & {"open", "read_text", "write_text", "fsync", "flock", "replace", "mkdir"}
