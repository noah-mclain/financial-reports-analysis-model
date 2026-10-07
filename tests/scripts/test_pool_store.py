"""The registry file: round trips, malformed metadata, durable writes. The rules are core's."""

import json
import os
from pathlib import Path

import pytest

from fra_core.pools import Identity, PoolError, PoolRegistry, Source
from pool_store import load_registry, save_registry


def test_late_pdf_preserves_sec_default(tmp_path: Path) -> None:
    registry = PoolRegistry()
    identity = Identity("Acme Widgets Inc.", "0000001111")
    pool = registry.assign(identity, Source.SEC)
    path = tmp_path / "pools.json"
    save_registry(registry, path)
    restored = load_registry(path)
    assert restored.assign(Identity("ACME WIDGETS CO.", 1111), Source.PDF) == pool
    assert restored.assign(Identity("Acme Widgets"), Source.PDF) == pool


def test_historical_sec_outputs_require_recorded_metadata(tmp_path: Path) -> None:
    output = tmp_path / "labels-2025q1.jsonl.gz"
    output.touch()
    path = tmp_path / "pools.json"
    with pytest.raises(PoolError, match="historical SEC"):
        load_registry(path, [output])
    save_registry(PoolRegistry(), path)
    with pytest.raises(PoolError, match="historical SEC"):
        load_registry(path, [output])


def test_covered_sec_outputs_are_metadata_only(tmp_path: Path) -> None:
    output = tmp_path / "labels-2025q1.jsonl.gz"
    output.write_bytes(b"not a label dataset")
    registry = PoolRegistry()
    registry.sec_outputs.add(output.name)
    registry.register(Identity("Acme", 1111), "train")
    path = tmp_path / "pools.json"
    save_registry(registry, path)
    assert load_registry(path, [output]).assign(Identity("Acme"), Source.PDF) == "train"


def test_explicit_alias_survives_a_later_cik_link(tmp_path: Path) -> None:
    registry = PoolRegistry()
    registry.register(Identity("Original Widgets"), "model_test")
    registry.alias("New Brand", Identity("Original Widgets"))
    registry.assign(Identity("New Brand", 1111), Source.SEC)
    path = tmp_path / "pools.json"
    save_registry(registry, path)
    restored = load_registry(path)
    assert restored.assign(Identity("Original Widgets", 1111), Source.PDF) == "model_test"


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"version": true}',
        '{"version": 2}',
        '{"version": 1, "assignments": [], "sec_outputs": ["bad"]}',
        '{"version": 1, "assignments": [{"names": [], "pool": "train"}], "sec_outputs": []}',
    ],
)
def test_malformed_metadata_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "pools.json"
    path.write_text(text)
    with pytest.raises(PoolError):
        load_registry(path)


def test_name_only_alias_group_survives_reload_before_cik_arrives(tmp_path: Path) -> None:
    registry = PoolRegistry()
    registry.register(Identity("Original Widgets"), "train")
    registry.alias("New Brand", Identity("Original Widgets"))
    path = tmp_path / "pools.json"
    save_registry(registry, path)
    restored = load_registry(path)
    restored.assign(Identity("New Brand", 1111), Source.SEC)
    assert restored.cik_for(Identity("Original Widgets")) == 1111


def test_reviewed_ambiguous_name_keeps_ciks_separate_and_refuses_bare_pdf(tmp_path: Path) -> None:
    registry = PoolRegistry(ambiguous_names=["acme"])
    registry.register(Identity("Acme Corp", 1111), "train")
    registry.register(Identity("Acme Inc", 2222), "model_test")
    path = tmp_path / "pools.json"
    save_registry(registry, path)
    restored = load_registry(path)
    assert restored.cik_pools() == {1111: "train", 2222: "model_test"}
    assert restored.metadata()["ambiguous_names"] == ["acme"]
    with pytest.raises(PoolError, match=r"ambiguous.*supply.*CIK"):
        restored.assign(Identity("Acme"), Source.PDF)
    assert restored.assign(Identity("Acme", 1111), Source.PDF) == "train"
    assert restored.assign(Identity("Acme", 2222), Source.PDF) == "model_test"
    with pytest.raises(PoolError, match="spans pools"):
        restored.register(Identity("Acme", 1111), "model_test")


def test_ambiguity_cannot_discard_existing_name_only_assignment(tmp_path: Path) -> None:
    path = tmp_path / "pools.json"
    registry = PoolRegistry()
    registry.register(Identity("Acme"), "blind")
    metadata = registry.metadata()
    metadata["ambiguous_names"] = ["acme"]
    path.write_text(json.dumps(metadata))
    with pytest.raises(PoolError, match=r"ambiguous.*reviewed.*CIK"):
        load_registry(path)


def test_ambiguity_declaration_preserves_existing_cik_pinned_pool(tmp_path: Path) -> None:
    path = tmp_path / "pools.json"
    registry = PoolRegistry()
    registry.register(Identity("Acme", 1111), "blind")
    metadata = registry.metadata()
    metadata["ambiguous_names"] = ["acme"]
    path.write_text(json.dumps(metadata))
    restored = load_registry(path)
    assert restored.cik_pools() == {1111: "blind"}
    assert restored.assign(Identity("Acme", 1111), Source.SEC) == "blind"


def test_legacy_version_one_metadata_without_ambiguity_list_loads(tmp_path: Path) -> None:
    registry = PoolRegistry()
    registry.register(Identity("Acme", 1111), "train")
    metadata = registry.metadata()
    del metadata["ambiguous_names"]
    path = tmp_path / "pools.json"
    path.write_text(json.dumps(metadata))
    assert load_registry(path).assign(Identity("Acme"), Source.PDF) == "train"


@pytest.mark.parametrize("names", [None, "acme", [1], [""], ["ACME CORP"], ["acme", "acme"]])
def test_ambiguous_names_metadata_is_validated(tmp_path: Path, names: object) -> None:
    path = tmp_path / "pools.json"
    metadata = PoolRegistry().metadata()
    metadata["ambiguous_names"] = names
    path.write_text(json.dumps(metadata))
    with pytest.raises(PoolError, match="ambiguous_names"):
        load_registry(path)


def test_durable_registry_flushes_before_replace_and_syncs_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pools.json"
    events = []
    fsync, replace = os.fsync, Path.replace

    def sync(fd: int) -> None:
        events.append("sync")
        if len(events) == 1:
            assert json.loads(path.with_suffix(".tmp").read_text())["version"] == 1
        fsync(fd)

    def move(self: Path, target: Path) -> Path:
        events.append("replace")
        return replace(self, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", move)
    save_registry(PoolRegistry(), path)
    assert events == ["sync", "replace", "sync"]


@pytest.mark.parametrize("failed_sync", [1, 2])
def test_registry_sync_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_sync: int
) -> None:
    path = tmp_path / "pools.json"
    path.write_text("old allocation records")
    calls = 0

    def fail_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError("synthetic durability failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(OSError, match="durability"):
        save_registry(PoolRegistry(), path)
    if failed_sync == 1:
        assert path.read_text() == "old allocation records"
