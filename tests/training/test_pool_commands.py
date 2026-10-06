"""Synthetic command integration: durable pools precede SEC labels and PDF collection."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_sec_fsds import make_zip, pre

import argaam_listing
import corpus
import sec_fsds
from fra_core.pools import Identity, Source, load_registry


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    metadata = tmp_path / "eval/corpus"
    metadata.mkdir(parents=True)
    paths = {
        "candidates": metadata / "candidates.yaml",
        "golden": tmp_path / "golden.yaml",
        "registry": tmp_path / "var/issuer-pools.json",
        "out": tmp_path / "sec",
        "deferred": metadata / "deferred.yaml",
        "fetched": metadata / "fetched.yaml",
    }
    paths["candidates"].write_text("documents: []\n")
    paths["golden"].write_text("documents: []\n")
    monkeypatch.setattr(sec_fsds, "CANDIDATES", paths["candidates"])
    monkeypatch.setattr(sec_fsds, "GOLDEN_MANIFEST", paths["golden"])
    monkeypatch.setattr(sec_fsds, "POOL_METADATA", paths["registry"])
    monkeypatch.setattr(sec_fsds, "OUT", paths["out"])
    monkeypatch.setattr(corpus, "CANDIDATES", paths["candidates"])
    monkeypatch.setattr(corpus, "GOLDEN_MANIFEST", paths["golden"])
    monkeypatch.setattr(corpus, "POOL_METADATA", paths["registry"])
    monkeypatch.setattr(corpus, "SEC_OUT", paths["out"])
    monkeypatch.setattr(corpus, "FETCHED", paths["fetched"])
    monkeypatch.setattr(corpus, "golden_index", lambda: (set(), {}))
    monkeypatch.setenv("FRA_SEC_USER_AGENT", "test test@example.com")
    archive = make_zip(
        tmp_path / "2025q1.zip",
        [["a", "1111", "ACME WIDGETS INC", "2040", "10-K", "20241231", "2024", "FY"]],
        [pre("a", "BS", "Assets", "Total assets")],
    )
    monkeypatch.setattr(sec_fsds, "download", lambda *_: archive)
    return paths


def pdf(issuer: str, pool: str, cik: int | str | None = None) -> dict[str, Any]:
    record = {
        "id": "acme",
        "issuer": issuer,
        "pool": pool,
        "role": "corporate",
        "url": "https://example.com/acme.pdf",
    }
    if cik is not None:
        record["cik"] = cik
    return record


def test_sec_command_then_late_pdf_plan_preserves_recorded_pool(files: dict[str, Path]) -> None:
    assert sec_fsds.main(["2025q1"]) == 0
    registry = load_registry(files["registry"], files["out"].glob("labels-*.jsonl.gz"))
    pool = registry.assign(Identity("Acme Widgets"), Source.PDF)
    row = argaam_listing.ListingRow(
        "ACME",
        "/en/tadawul/tasi/acme",
        "اسم",
        "Materials",
        {"Q1": {"ar": "https://example.com/ar.pdf", "en": "https://example.com/en.pdf"}},
    )
    plan = argaam_listing.build_plan(
        [row], {row.path: "Acme Widgets Co."}, [], set(), {}, argaam_listing.MAIN, registry
    )
    assert {entry["pool"] for entry in plan.entries} == {pool}
    assert {entry["cik"] for entry in plan.entries} == {1111}
    files["candidates"].write_text(yaml.safe_dump({"documents": plan.entries}))
    assert corpus.cmd_check(argparse.Namespace()) == 0
    files["candidates"].write_text(
        yaml.safe_dump({"documents": [pdf("Acme Widgets", "blind", "0000001111")]})
    )
    assert corpus.cmd_check(argparse.Namespace()) == 1
    before = files["registry"].read_bytes()
    assert sec_fsds.main(["2025q1"]) == 1
    assert files["registry"].read_bytes() == before


def test_pdf_then_sec_command_keeps_alias_pin_and_drops_blind(files: dict[str, Path]) -> None:
    files["candidates"].write_text(
        yaml.safe_dump({"documents": [pdf("Acme Widgets Co.", "blind")]})
    )
    assert sec_fsds.main(["2025q1"]) == 0
    import gzip

    with gzip.open(files["out"] / "labels-2025q1.jsonl.gz", "rt") as fh:
        assert fh.read() == ""
    registry = load_registry(files["registry"])
    assert registry.assign(Identity("Renamed Acme", 1111), Source.PDF) == "blind"


def test_golden_dev_pin_is_preserved_by_sec_command(files: dict[str, Path]) -> None:
    files["golden"].write_text(yaml.safe_dump({"documents": [{"issuer": "Acme Widgets"}]}))
    assert sec_fsds.main(["2025q1"]) == 0
    assert load_registry(files["registry"]).assign(Identity(cik=1111), Source.SEC) == "dev"


def test_deferred_pin_and_fetched_conflict_are_checked_before_download(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files["deferred"].write_text(
        yaml.safe_dump({"documents": [pdf("Acme Widgets", "blind", 1111)]})
    )
    assert sec_fsds.main(["2025q1"]) == 0
    files["fetched"].write_text(yaml.safe_dump({"documents": {"acme": {"pool": "train"}}}))
    monkeypatch.setattr(sec_fsds, "download", lambda *_: pytest.fail("download before validation"))
    assert sec_fsds.main(["2025q2"]) == 1
    assert corpus.cmd_check(argparse.Namespace()) == 1


def test_unregistered_historical_output_blocks_both_commands_without_reading_labels(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    files["out"].mkdir()
    output = files["out"] / "labels-2024q1.jsonl.gz"
    output.write_bytes(b"do not parse historical labels")
    monkeypatch.setattr(
        sec_fsds, "download", lambda *_: pytest.fail("download before historical check")
    )
    assert sec_fsds.main(["2025q1"]) == 1
    assert corpus.cmd_check(argparse.Namespace()) == 1
    assert not files["registry"].exists()


def test_failed_sec_write_keeps_assignment_metadata_and_returns_failure(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(*_: object) -> Path:
        assert files["registry"].exists()
        raise OSError("synthetic write failure")

    monkeypatch.setattr(sec_fsds, "write", fail_write)
    assert sec_fsds.main(["2025q1"]) == 1
    assert load_registry(files["registry"]).assign(
        Identity("Acme Widgets"), Source.PDF
    ) == sec_fsds.pool_for(1111, {})


def test_collection_command_uses_sec_authority_before_writing_pdf_entries(
    files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sec_fsds.main(["2025q1"]) == 0
    files["candidates"].write_text(
        "documents:\n  # ---- dev ----\n  # ---- train ----\n  - {id: other, issuer: Other Business, pool: train, role: corporate, url: 'https://example.com/other.pdf'}\n  # ---- model_test ----\n  # ---- blind ----\n"
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    row = argaam_listing.ListingRow(
        "ACME",
        "/en/tadawul/tasi/acme",
        "اسم",
        "Materials",
        {"Q1": {"ar": "https://example.com/ar.pdf"}},
    )

    def page(self: object, url: str, name: str, parse: object) -> object:
        return [row] if name == "listing.html" else "Acme Widgets Co."

    monkeypatch.setattr(argaam_listing.Requests, "page", page)
    monkeypatch.setattr(argaam_listing, "_load_decisions", lambda: {})
    monkeypatch.setattr(argaam_listing, "DEFERRED", files["deferred"])
    args = argparse.Namespace(cache=cache, market="main", report=tmp_path / "report.md", write=True)
    assert argaam_listing.cmd_collect(args) == 0
    entries = yaml.safe_load(files["candidates"].read_text())["documents"]
    added = next(d for d in entries if d["issuer"] == "Acme Widgets")
    assert added["cik"] == 1111
    assert added["pool"] == sec_fsds.pool_for(1111, {})


def test_legacy_defaults_are_preserved_over_representative_keys() -> None:
    for cik in range(1, 400):
        bucket = int(hashlib.sha256(f"cik:{cik}".encode()).hexdigest(), 16) % 100
        assert sec_fsds.pool_for(cik, {}) == ("model_test" if bucket < 15 else "train")
    for i in range(300):
        name = f"Issuer {i}"
        bucket = int(hashlib.sha256(corpus.issuer_key(name).encode()).hexdigest(), 16) % 100
        assert corpus.assign_pool(name) == (
            "train" if bucket < 65 else "model_test" if bucket < 85 else "blind"
        )


def test_invalid_quarter_is_rejected_before_allocation(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sec_fsds, "download", lambda *_: pytest.fail("invalid quarter downloaded"))
    assert sec_fsds.main(["../2025q1"]) == 2
    assert not files["registry"].exists()


def test_historical_metadata_guard_precedes_pdf_fetch_and_collection(
    files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fra_core.pools import PoolError

    files["out"].mkdir()
    (files["out"] / "labels-2024q1.jsonl.gz").touch()
    files["candidates"].write_text(yaml.safe_dump({"documents": [pdf("Acme Widgets", "train")]}))
    monkeypatch.setattr(corpus, "download", lambda *_: pytest.fail("PDF downloaded"))
    monkeypatch.setattr(
        argaam_listing.Requests, "page", lambda *_: pytest.fail("listing downloaded")
    )
    assert corpus.cmd_fetch(argparse.Namespace(pool=None, new=True, id=None, force=False)) == 1
    with pytest.raises(PoolError, match="historical SEC"):
        argaam_listing.cmd_collect(
            argparse.Namespace(
                cache=tmp_path, market="main", report=tmp_path / "report.md", write=True
            )
        )
    assert not files["registry"].exists()


def test_documented_recovery_schema_restores_synthetic_metadata_only(
    files: dict[str, Path],
) -> None:
    readme = Path(__file__).resolve().parents[2] / "eval/corpus/README.md"
    example = readme.read_text().split("```json\n", 1)[1].split("```", 1)[0]
    files["registry"].parent.mkdir(parents=True)
    files["registry"].write_text(json.dumps(json.loads(example)))
    files["out"].mkdir()
    (files["out"] / "labels-2025q1.jsonl.gz").write_bytes(b"never read label rows")
    registry = load_registry(files["registry"], files["out"].glob("labels-*.jsonl.gz"))
    assert registry.cik_pools() == {1111: "train", 2222: "model_test"}
    assert registry.assign(Identity("Widgets Brand"), Source.PDF) == "blind"
    assert corpus.cmd_check(argparse.Namespace()) == 0


@pytest.mark.parametrize("failed_sync", [1, 2])
def test_registry_sync_failure_prevents_sec_output(
    files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, failed_sync: int
) -> None:
    import fra_core.pools as pools

    calls = 0

    def fail_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError("synthetic sync failure")

    monkeypatch.setattr(pools.os, "fsync", fail_sync)
    monkeypatch.setattr(sec_fsds, "write", lambda *_: pytest.fail("output before durable record"))
    assert sec_fsds.main(["2025q1"]) == 1
    assert files["registry"].exists() == (failed_sync == 2)
    assert not files["out"].exists()


def test_existing_keyless_collection_stops_before_any_artifact_write(
    files: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fra_core.pools import PoolError

    assert sec_fsds.main(["2025q1"]) == 0
    old = {
        **pdf("Acme Widgets", "train"),
        "language": "ar",
        "fiscal_year": 2024,
        "period": "annual",
    }
    files["candidates"].write_text(yaml.safe_dump({"documents": [old]}))
    before = {key: files[key].read_bytes() for key in ("registry", "candidates")}
    row = argaam_listing.ListingRow(
        "ACME",
        "/en/tadawul/tasi/acme",
        "اسم",
        "Materials",
        {"Q1": {"ar": "https://example.com/new-ar.pdf"}},
    )

    def page(self: object, url: str, name: str, parse: object) -> object:
        return [row] if name == "listing.html" else "Acme Widgets"

    monkeypatch.setattr(argaam_listing.Requests, "page", page)
    monkeypatch.setattr(argaam_listing, "_load_decisions", lambda: {})
    monkeypatch.setattr(argaam_listing, "DEFERRED", files["deferred"])
    report = tmp_path / "report.md"
    args = argparse.Namespace(cache=tmp_path, market="main", report=report, write=True)
    with pytest.raises(PoolError, match=r"coordinated.*CIK.*review"):
        argaam_listing.cmd_collect(args)
    assert {key: files[key].read_bytes() for key in before} == before
    assert not report.exists()
