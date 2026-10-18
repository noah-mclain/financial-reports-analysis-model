"""The routed pass: stream each archive's presentation member once, keep routed filings only.

Routing comes from ``sec_source_preparation`` (submission metadata, recorded pools, the shared
split). A presentation line is matched on its accession prefix alone, as bytes. A line of an
unrouted filing is counted and dropped without being decoded or parsed, so it can neither
fail the pass nor leave a trace. A malformed line of a routed filing aborts the pass, naming
archive, filing and line number but never the line. A statement code is not validated here:
real archives hold empty ones, and the decoder drops what is not a supported statement.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dataset_contracts import Contract, DatasetError, SourceRecord, digest
from exclusive_write import write_new
from harness.holdout_records import read_looks, read_moves
from pydantic import ValidationError
from sec_source_access import (
    AccessContext,
    AuthorizedPayload,
    PayloadArtifact,
    SecFiling,
    SecIndex,
    SourceManifest,
    authorize_payloads,
    registry_sha256,
    split_records_sha256,
)
from sec_source_preparation import (
    ANNUAL_FORMS,
    INTERIM_FORMS,
    FilingRow,
    issuer_identity,
    read_roster,
    route_filings,
)

from fra_core.pools import Pool, Source
from fra_core.split import Part
from pool_store import load_registry

WRITER_VERSION = "sec-pass-1"
PRE_MEMBER = "pre.txt"
PRE_REQUIRED = ("adsh", "report", "line", "stmt", "inpth", "tag", "version", "plabel", "negating")
PAYLOAD_FIELDS = ("adsh", "stmt", "report", "line", "tag", "version", "plabel", "negating", "inpth")
_ARCHIVE_ID = re.compile(r"\d{4}q[1-4]")


class PayloadRow(Contract):
    """One routed presentation line, with only the fields the normalization data needs."""

    adsh: str
    stmt: str
    report: int
    line: int
    tag: str
    version: str
    plabel: str
    negating: bool
    inpth: bool


@dataclass
class StreamStats:
    lines: int = 0
    kept: int = 0
    discarded: int = 0


def _fail(archive: Path, number: int, adsh: str, problem: str) -> DatasetError:
    return DatasetError(f"{archive.name}: {PRE_MEMBER} line {number}: filing {adsh}: {problem}")


def stream_routed_rows(
    archive: Path, routed: Collection[str]
) -> tuple[dict[str, list[PayloadRow]], StreamStats]:
    """Rows of the routed filings; every other line is counted and never read further."""
    wanted = {accession.encode("ascii") for accession in routed}
    rows: dict[str, list[PayloadRow]] = {}
    stats = StreamStats()
    try:
        with zipfile.ZipFile(archive) as zf, zf.open(PRE_MEMBER) as raw:
            header = raw.readline().decode("utf-8").rstrip("\r\n").split("\t")
            missing = [name for name in PRE_REQUIRED if name not in header]
            if missing:
                raise DatasetError(f"{archive.name}: {PRE_MEMBER} line 1: header lacks {missing}")
            if header[0] != "adsh":
                raise DatasetError(
                    f"{archive.name}: {PRE_MEMBER} line 1: adsh must be the first column, "
                    f"not {header[0]!r}"
                )
            at = {name: header.index(name) for name in PRE_REQUIRED}
            for number, line in enumerate(raw, start=2):
                stats.lines += 1
                end = line.find(b"\t")
                if (line[:end] if end >= 0 else line.rstrip(b"\r\n")) not in wanted:
                    stats.discarded += 1
                    continue
                stats.kept += 1
                row = _parse_routed(archive, number, line, header, at)
                rows.setdefault(row.adsh, []).append(row)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise DatasetError(f"{archive}: cannot read {PRE_MEMBER}: {exc}") from exc
    return rows, stats


def _parse_routed(
    archive: Path, number: int, line: bytes, header: list[str], at: dict[str, int]
) -> PayloadRow:
    accession = line.split(b"\t", 1)[0].decode("ascii", errors="replace").strip()
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail(archive, number, accession, "not UTF-8") from None
    fields = text.rstrip("\r\n").split("\t")
    if len(fields) != len(header):
        raise _fail(archive, number, accession, f"expected {len(header)} columns")
    value = {name: fields[i] for name, i in at.items()}
    for flag in ("inpth", "negating"):
        if value[flag] not in ("0", "1"):
            raise _fail(archive, number, accession, f"{flag} is not 0 or 1")
    for count in ("report", "line"):
        if not value[count].isascii() or not value[count].isdigit():
            raise _fail(archive, number, accession, f"{count} is not a number")
    try:
        return PayloadRow(
            adsh=value["adsh"],
            stmt=value["stmt"],
            report=int(value["report"]),
            line=int(value["line"]),
            tag=value["tag"],
            version=value["version"],
            plabel=value["plabel"],
            negating=value["negating"] == "1",
            inpth=value["inpth"] == "1",
        )
    except ValidationError:
        raise _fail(archive, number, accession, "a field is not valid") from None


def encode_payload(rows: Sequence[PayloadRow]) -> bytes:
    """Deterministic gzip JSONL of one filing's rows, in file order."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        for row in rows:
            line = json.dumps(row.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            handle.write(line.encode("utf-8") + b"\n")
    return buffer.getvalue()


def directory_opener(root: Path) -> Callable[[AuthorizedPayload], bytes]:
    """Open an artifact under ``root``: no symlinks, nothing outside the root, regular files."""

    def opener(payload: AuthorizedPayload) -> bytes:
        path = root / payload.artifact.relative_path
        if path.is_symlink():
            raise PermissionError(f"{path}: symlink refused")
        resolved = path.resolve(strict=True)
        if root.resolve() not in resolved.parents or not resolved.is_file():
            raise PermissionError(f"{path}: not a regular file under the payload root")
        return resolved.read_bytes()

    return opener


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _config_sha256() -> str:
    return digest(
        {
            "writer_version": WRITER_VERSION,
            "payload_fields": list(PAYLOAD_FIELDS),
            "annual_forms": sorted(ANNUAL_FORMS),
            "interim_forms": sorted(INTERIM_FORMS),
            "routing_drops": [
                "unsupported_form",
                "missing_period",
                "unrecorded_cik",
                "pool_other_than_train",
                "name_collision",
                "holdout",
                "coregistrant_not_routed",
            ],
        }
    )


def _source_record(filing: FilingRow, source_sha: str, config_sha: str) -> SourceRecord:
    assert filing.period is not None  # routing drops filings without a period
    try:
        period_end = date(int(filing.period[:4]), int(filing.period[4:6]), int(filing.period[6:]))
    except ValueError:
        raise DatasetError(f"{filing.adsh}: period {filing.period!r} is not a date") from None
    return SourceRecord(
        source_id=filing.adsh,
        issuer=issuer_identity(filing),
        pool=Pool.TRAIN,
        source=Source.SEC,
        language="en",
        period="annual" if filing.form in ANNUAL_FORMS else "interim",
        period_end=period_end,
        source_sha256=source_sha,
        config_sha256=config_sha,
    )


def _prepare_archive(
    archive_id: str,
    archive: Path,
    filings: Sequence[FilingRow],
    *,
    out_dir: Path,
    config_sha: str,
    registry: Any,
    moves: Sequence[Any],
    looks: Sequence[Any],
) -> dict[str, Any]:
    source_sha = _sha256_file(archive)
    rows, stats = stream_routed_rows(archive, [f.adsh for f in filings])
    artifacts: list[PayloadArtifact] = []
    roster: list[SecFiling] = []
    payload_bytes = 0
    for filing in sorted(filings, key=lambda f: f.adsh):
        data = encode_payload(rows.get(filing.adsh, []))
        relative = f"{archive_id}/payloads/{filing.adsh}.jsonl.gz"
        write_new({out_dir / relative: data})
        payload_bytes += len(data)
        artifacts.append(
            PayloadArtifact(
                artifact_id=f"{archive_id}:{filing.adsh}",
                source_id=filing.adsh,
                relative_path=relative,
                payload_sha256=hashlib.sha256(data).hexdigest(),
                row_count=len(rows.get(filing.adsh, [])),
            )
        )
        roster.append(
            SecFiling(
                source_record=_source_record(filing, source_sha, config_sha),
                raw_locator=f"{archive_id}:{filing.adsh}",
            )
        )
    index = SecIndex(
        archive_id=archive_id,
        source_sha256=source_sha,
        config_sha256=config_sha,
        filings=tuple(roster),
    )
    manifest = SourceManifest(
        state="complete",
        writer_version=WRITER_VERSION,
        source_sha256=source_sha,
        config_sha256=config_sha,
        index_sha256=index.content_sha256(),
        registry_sha256=registry_sha256(registry),
        split_records_sha256=split_records_sha256(moves, looks),
        artifacts=tuple(artifacts),
    )
    context = AccessContext(
        source_sha256=source_sha,
        config_sha256=config_sha,
        index_sha256=index.content_sha256(),
        manifest_sha256=manifest.content_sha256(),
        registry_sha256=manifest.registry_sha256,
        split_records_sha256=manifest.split_records_sha256,
    )
    # The access contract must accept what was just written before the pass calls it complete.
    authorize_payloads(
        [a.artifact_id for a in artifacts],
        index=index,
        manifest=manifest,
        registry=registry,
        context=context,
        moves=moves,
        looks=looks,
    )
    records = {
        out_dir / archive_id / f"{name}.json": _json_bytes(model.model_dump(mode="json"))
        for name, model in (("index", index), ("manifest", manifest), ("context", context))
    }
    write_new(records)
    return {
        "source_sha256": source_sha,
        "pre_lines": stats.lines,
        "kept_rows": stats.kept,
        "discarded_rows": stats.discarded,
        "routed_filings": len(filings),
        "payload_bytes": payload_bytes,
        "output_bytes": payload_bytes + sum(len(data) for data in records.values()),
        "empty_payloads": sorted(f.adsh for f in filings if not rows.get(f.adsh)),
        "index_sha256": context.index_sha256,
        "manifest_sha256": context.manifest_sha256,
    }


def run_pass(
    *,
    archives: Sequence[Path],
    registry_path: Path,
    moves_path: Path,
    looks_path: Path,
    out_dir: Path,
    authorization: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_archive: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the one routed pass and write payloads, per-archive records and the pass log.

    The output directory must not exist. The registry, moves and looks files must be unchanged
    when the pass ends. Any failure leaves ``pass-abort.json`` (no line content) and raises.
    """
    if out_dir.exists():
        raise DatasetError(f"{out_dir} already exists; refusing to reuse it")
    started = clock()
    inputs = (registry_path, moves_path, looks_path)
    fingerprints = [_sha256_file(path) for path in inputs]
    registry = load_registry(registry_path)
    looks = read_looks(looks_path)
    moves = read_moves(moves_path, looks)
    names = {}
    for archive in archives:
        if not _ARCHIVE_ID.fullmatch(archive.stem) or archive.stem in names:
            raise DatasetError(f"{archive}: archive name must be a unique <yyyy>q<n>.zip")
        names[archive.stem] = archive
    rosters = {archive_id: read_roster(path) for archive_id, path in names.items()}
    everything = [f for filings in rosters.values() for f in filings]
    if len({f.adsh for f in everything}) != len(everything):
        raise DatasetError("an accession appears in more than one archive")
    routing = route_filings(everything, registry, moves, looks)
    try:
        out_dir.mkdir(parents=True)
    except FileExistsError:
        raise DatasetError(f"{out_dir} already exists; refusing to reuse it") from None
    try:
        config_sha = _config_sha256()
        archive_logs: dict[str, Any] = {}
        for archive_id, path in sorted(names.items()):
            filings = [f for f in rosters[archive_id] if f.adsh in routing.parts]
            archive_logs[archive_id] = _prepare_archive(
                archive_id,
                path,
                filings,
                out_dir=out_dir,
                config_sha=config_sha,
                registry=registry,
                moves=routing.applicable_moves,
                looks=looks,
            )
            if on_archive is not None:
                on_archive(archive_id)
        if [_sha256_file(path) for path in inputs] != fingerprints:
            raise DatasetError("the registry, moves or looks file changed during the pass")
        log = {
            "writer_version": WRITER_VERSION,
            "authorization": authorization,
            "started": started.isoformat(),
            "finished": clock().isoformat(),
            "config_sha256": config_sha,
            "registry": {
                "file_sha256": fingerprints[0],
                "metadata_sha256": registry_sha256(registry),
            },
            "moves": {
                "file_sha256": fingerprints[1],
                "applicable": [m.key for m in routing.applicable_moves],
                "split_records_sha256": split_records_sha256(routing.applicable_moves, looks),
            },
            "looks": {"file_sha256": fingerprints[2]},
            "routing": {
                "submissions": len(everything),
                "routed": {
                    part.value: sum(p == part for p in routing.parts.values())
                    for part in (Part.FIT, Part.VALIDATION)
                },
                "dropped": dict(sorted(routing.dropped.items())),
            },
            "archives": archive_logs,
            "totals": {
                "pre_lines": sum(a["pre_lines"] for a in archive_logs.values()),
                "kept_rows": sum(a["kept_rows"] for a in archive_logs.values()),
                "discarded_rows": sum(a["discarded_rows"] for a in archive_logs.values()),
                "payload_bytes": sum(a["payload_bytes"] for a in archive_logs.values()),
                "output_bytes": sum(a["output_bytes"] for a in archive_logs.values()),
                "empty_payloads": sum(len(a["empty_payloads"]) for a in archive_logs.values()),
            },
        }
        write_new({out_dir / "pass-log.json": _json_bytes(log)})
    except Exception as exc:
        write_new({out_dir / "pass-abort.json": _json_bytes({"error": str(exc)})})
        raise
    return log
