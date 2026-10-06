"""The pool contract works on values: assignment rules, parsing a metadata dict, no file IO."""

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


BANNED_IMPORTS = {
    "fcntl",
    "json",
    "yaml",
    "os",
    "pathlib",
    "contextlib",
    "io",
    "shutil",
    "tempfile",
}
BANNED_CALLS = {
    "open",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "fsync",
    "flock",
    "mkdir",
}


def io_violations(source: str) -> set[str]:
    """Names of file IO, locking or serialization modules and calls that a source uses."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            found.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return found & (BANNED_IMPORTS | BANNED_CALLS)


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("import json", "json"),
        ("from pathlib import Path", "pathlib"),
        ("open('f')", "open"),
        ("p.read_text()", "read_text"),
        ("p.read_bytes()", "read_bytes"),
        ("p.write_bytes(b'')", "write_bytes"),
    ],
)
def test_the_io_check_catches_what_it_names(source: str, name: str) -> None:
    assert io_violations(source) == {name}


def test_core_pools_does_no_file_io_and_no_locking() -> None:
    """The contract stays pure; reading, writing and locking live in scripts/pool_store.py."""
    assert io_violations(Path(pools.__file__).read_text(encoding="utf-8")) == set()


def test_late_sec_preserves_pdf_and_links_renames() -> None:
    registry = PoolRegistry()
    registry.register(Identity("Acme Widgets"), "blind")
    assert registry.assign(Identity("ACME WIDGETS INC", 1111), Source.SEC) == "blind"
    assert registry.assign(Identity("Renamed Widgets", "0000001111"), Source.PDF) == "blind"
    assert registry.assign(Identity("Renamed Widgets Co."), Source.SEC) == "blind"


def test_bridge_conflict_is_rejected_without_mutation() -> None:
    registry = PoolRegistry()
    registry.register(Identity("Acme"), "train")
    registry.register(Identity(cik=1111), "model_test")
    with pytest.raises(PoolError, match="spans pools"):
        registry.assign(Identity("Acme", 1111), Source.SEC)
    assert registry.assign(Identity("Acme"), Source.PDF) == "train"
    assert registry.assign(Identity(cik=1111), Source.SEC) == "model_test"


def test_duplicate_pin_is_idempotent_but_conflicting_pin_fails() -> None:
    registry = PoolRegistry()
    registry.register(Identity("Acme", 1111), "dev")
    registry.register(Identity("ACME CO.", "0000001111"), "dev")
    with pytest.raises(PoolError, match="spans pools"):
        registry.register(Identity("Another Name", 1111), "train")


@pytest.mark.parametrize(
    "name,cik",
    [
        (None, None),
        ("", None),
        ("Company", None),
        (123, None),
        ("Acme", 0),
        ("Acme", -1),
        ("Acme", True),
        ("Acme", "bad"),
        ("Acme", 1.5),
    ],
)
def test_invalid_identity_is_loud(name: object, cik: object) -> None:
    with pytest.raises(PoolError):
        Identity(name, cik)  # type: ignore[arg-type]


@pytest.mark.parametrize("pool", [None, "", "fit", "TRAIN", 1])
def test_invalid_pool_is_loud(pool: object) -> None:
    with pytest.raises(PoolError, match="pool"):
        PoolRegistry().register(Identity("Acme"), pool)  # type: ignore[arg-type]


def test_distinct_ciks_for_one_normalized_name_require_review() -> None:
    registry = PoolRegistry()
    registry.register(Identity("Acme", 1111), "train")
    with pytest.raises(PoolError, match="CIK"):
        registry.register(Identity("ACME CO.", 2222), "train")


def test_collision_diagnostic_names_ciks_and_recovery() -> None:
    registry = PoolRegistry()
    registry.register(Identity("Acme Corp", 1111), "train")
    with pytest.raises(PoolError, match=r"1111.*2222.*ambiguous_names"):
        registry.assign(Identity("Acme Inc", 2222), Source.SEC)
