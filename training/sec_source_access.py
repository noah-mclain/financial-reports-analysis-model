"""Metadata-first SEC filing access against injected, independently reviewed authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from typing import Literal

from dataset_contracts import Sha256, SourceRecord, Text
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fra_core.pools import Pool, PoolRegistry, Source
from fra_core.split import (
    Look,
    Move,
    Part,
    check_looks,
    check_moves,
    cik_key,
    development_documents,
)


class SourceAccessError(ValueError):
    """An unbound, unauthorized or unavailable filing artifact."""


class _Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


class _VersionedContract(_Contract):
    version: Literal[1] = 1

    @field_validator("version", mode="before")
    @classmethod
    def _integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value


class SecFiling(_Contract):
    """Metadata only; source_id identifies a filing, not an issuer/year pair."""

    source_record: SourceRecord
    raw_locator: Text

    @model_validator(mode="after")
    def _sec_identity(self) -> SecFiling:
        if self.source_record.source != Source.SEC:
            raise ValueError(f"{self.source_record.source_id}: SEC source required")
        if self.source_record.issuer.cik is None:
            raise ValueError(f"{self.source_record.source_id}: reviewed CIK required")
        return self


class SecIndex(_VersionedContract):
    """Complete metadata roster for one immutable archive; contains no labels."""

    archive_id: Text
    source_sha256: Sha256
    config_sha256: Sha256
    filings: tuple[SecFiling, ...]

    def content_sha256(self) -> str:
        checked = SecIndex.model_validate(self)
        value = checked.model_dump(mode="json")
        value["filings"] = [
            f.model_dump(mode="json")
            for f in sorted(checked.filings, key=lambda f: f.source_record.source_id)
        ]
        return _digest(value)


class PayloadArtifact(_Contract):
    """One filing's payload description; pool/part claims cannot be supplied here."""

    artifact_id: Text
    source_id: Text
    relative_path: Text
    payload_sha256: Sha256
    row_count: int = Field(ge=0)

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        # Canonical ASCII POSIX components: no normalization, escapes or URI syntax.
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", p) for p in value.split("/")):
            raise ValueError(f"relative_path {value!r}: expected canonical relative components")
        return value


class SourceManifest(_VersionedContract):
    """Complete-artifact claim, trusted only through an independently supplied digest."""

    state: Literal["complete"]
    writer_version: Text
    source_sha256: Sha256
    config_sha256: Sha256
    index_sha256: Sha256
    registry_sha256: Sha256
    split_records_sha256: Sha256
    artifacts: tuple[PayloadArtifact, ...]

    def content_sha256(self) -> str:
        checked = SourceManifest.model_validate(self)
        value = checked.model_dump(mode="json")
        value["artifacts"] = [
            a.model_dump(mode="json")
            for a in sorted(checked.artifacts, key=lambda a: a.artifact_id)
        ]
        return _digest(value)


class AccessContext(_Contract):
    """Exact expected hashes from the caller's reviewed authority, never label rows."""

    source_sha256: Sha256
    config_sha256: Sha256
    index_sha256: Sha256
    manifest_sha256: Sha256
    registry_sha256: Sha256
    split_records_sha256: Sha256


class AuthorizedPayload(_Contract):
    """Access eligibility evidence for this call; not a persistent bearer capability."""

    filing: SecFiling
    artifact: PayloadArtifact
    part: Literal[Part.FIT, Part.VALIDATION]
    context: AccessContext


class VerifiedPayload(_Contract):
    """Hash-checked bytes, still requiring source/row-count validation by a decoder."""

    descriptor: AuthorizedPayload
    payload: bytes


class _SplitRecords(_Contract):
    moves: tuple[Move, ...]
    looks: tuple[Look, ...]


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def registry_sha256(registry: PoolRegistry) -> str:
    """Fingerprint public registry metadata; computing it is not approval."""
    return _digest(registry.metadata())


def split_records_sha256(moves: Sequence[Move], looks: Sequence[Look]) -> str:
    """Fingerprint the exact ordered records, without writing or spending a look."""
    records = _SplitRecords(moves=tuple(moves), looks=tuple(looks))
    return _digest(
        {
            "moves": [asdict(m) for m in records.moves],
            "looks": [asdict(look) for look in records.looks],
        }
    )


def _require_hash(actual: str, expected: str, field: str, subject: str) -> None:
    if actual != expected:
        raise SourceAccessError(f"{subject}: {field} differs from reviewed context")


def authorize_payloads(
    artifact_ids: Sequence[str],
    *,
    index: SecIndex,
    manifest: SourceManifest,
    registry: PoolRegistry,
    context: AccessContext,
    moves: Sequence[Move],
    looks: Sequence[Look],
) -> tuple[AuthorizedPayload, ...]:
    """Validate the entire roster and every selection before returning eligible descriptors.

    The injected context must independently establish index completeness, registry review
    and the complete manifest's commit evidence. No allocator or filesystem is called.
    Even empty selections validate the roster and move targets. Metadata may describe
    holdout/nontrain filings, but their payloads are never eligible here.
    """
    index = SecIndex.model_validate(index)
    manifest = SourceManifest.model_validate(manifest)
    context = AccessContext.model_validate(context)
    records = _SplitRecords(moves=tuple(moves), looks=tuple(looks))
    check_looks(records.looks)
    check_moves(records.moves, records.looks)
    metadata = registry.metadata()
    for field in ("source_sha256", "config_sha256"):
        _require_hash(getattr(index, field), getattr(context, field), field, "index")
    for field in (
        "source_sha256",
        "config_sha256",
        "index_sha256",
        "registry_sha256",
        "split_records_sha256",
    ):
        _require_hash(getattr(manifest, field), getattr(context, field), field, "manifest")
    _require_hash(index.content_sha256(), context.index_sha256, "index_sha256", "index")
    _require_hash(manifest.content_sha256(), context.manifest_sha256, "manifest_sha256", "manifest")
    _require_hash(_digest(metadata), context.registry_sha256, "registry_sha256", "registry")
    _require_hash(
        split_records_sha256(records.moves, records.looks),
        context.split_records_sha256,
        "split_records_sha256",
        "split records",
    )

    # One disposable probe serves every filing: any assignment or alias it would add is
    # refused below, so it never drifts from the reviewed snapshot between filings.
    probe = PoolRegistry.from_metadata(metadata, "injected reviewed registry")
    by_source: dict[str, SecFiling] = {}
    locators: set[str] = set()
    documents: list[dict[str, object]] = []
    for filing in index.filings:
        source = filing.source_record
        if source.source_id in by_source:
            raise SourceAccessError(f"{source.source_id}: duplicate filing id in metadata roster")
        if filing.raw_locator in locators:
            raise SourceAccessError(f"{source.source_id}: duplicate raw locator in metadata roster")
        by_source[source.source_id] = filing
        locators.add(filing.raw_locator)
        for field in ("source_sha256", "config_sha256"):
            _require_hash(getattr(source, field), getattr(index, field), field, source.source_id)
        # Public register validates conflicts on the probe, never the authority.
        probe.register(source.issuer, source.pool)
        if probe.metadata() != metadata:
            raise SourceAccessError(
                f"{source.source_id}: identity/pool is not fully recorded in registry"
            )
        if source.pool == Pool.TRAIN:
            documents.append(
                {
                    "id": source.source_id,
                    "issuer": source.issuer.name or cik_key(str(source.issuer.cik)),
                    "cik": source.issuer.cik,
                    "pool": source.pool,
                }
            )
    source_parts: dict[str, Part] = {}
    for part in (Part.FIT, Part.VALIDATION):
        for document in development_documents(documents, records.moves, {part}):
            source_id = str(document["id"])
            if source_id in source_parts:
                raise SourceAccessError(f"{source_id}: source overlaps fit and validation")
            source_parts[source_id] = part

    by_artifact: dict[str, PayloadArtifact] = {}
    artifact_sources: set[str] = set()
    paths: set[str] = set()
    for artifact in manifest.artifacts:
        if artifact.artifact_id in by_artifact:
            raise SourceAccessError(f"{artifact.artifact_id}: duplicate artifact id")
        if artifact.source_id in artifact_sources:
            raise SourceAccessError(f"{artifact.artifact_id}: duplicate artifact source_id")
        if artifact.relative_path in paths:
            raise SourceAccessError(f"{artifact.artifact_id}: duplicate artifact relative_path")
        if artifact.source_id not in by_source:
            raise SourceAccessError(f"{artifact.artifact_id}: source_id is missing from index")
        by_artifact[artifact.artifact_id] = artifact
        artifact_sources.add(artifact.source_id)
        paths.add(artifact.relative_path)
    missing = sorted(set(by_source) - artifact_sources)
    if missing:
        raise SourceAccessError(f"manifest: missing filing artifacts: {', '.join(missing)}")

    selected: list[AuthorizedPayload] = []
    seen: set[str] = set()
    for artifact_id in artifact_ids:
        if not isinstance(artifact_id, str) or artifact_id not in by_artifact:
            raise SourceAccessError(f"{artifact_id!r}: unknown artifact selection")
        if artifact_id in seen:
            raise SourceAccessError(f"{artifact_id}: duplicate artifact selection")
        seen.add(artifact_id)
        artifact = by_artifact[artifact_id]
        filing = by_source[artifact.source_id]
        if filing.source_record.pool != Pool.TRAIN:
            raise SourceAccessError(f"{artifact_id}: {filing.source_record.pool} is not eligible")
        selected_part = source_parts.get(artifact.source_id)
        if selected_part is None:
            raise SourceAccessError(f"{artifact_id}: holdout is not eligible")
        if selected_part not in (Part.FIT, Part.VALIDATION):
            raise SourceAccessError(f"{artifact_id}: part {selected_part} is not eligible")
        selected.append(
            AuthorizedPayload(filing=filing, artifact=artifact, part=selected_part, context=context)
        )
    return tuple(sorted(selected, key=lambda d: d.artifact.artifact_id))


def iter_payloads(
    artifact_ids: Sequence[str],
    *,
    index: SecIndex,
    manifest: SourceManifest,
    registry: PoolRegistry,
    context: AccessContext,
    moves: Sequence[Move],
    looks: Sequence[Look],
    opener: Callable[[AuthorizedPayload], bytes],
) -> Iterator[VerifiedPayload]:
    """Authorize all selections now, then retrieve and hash one payload per step.

    Authorization runs before this function returns, so a forbidden selection or a metadata
    failure raises here and the opener is not called. Each step opens exactly one payload and
    holds no earlier one, which keeps memory to the largest single payload. A retrieval or
    hash failure surfaces at its own step, after earlier payloads were already yielded; a
    caller that needs all-or-nothing must treat any failure as invalidating what it consumed.
    The opener enforces the production filesystem boundary. Bytes are not decoded, so their
    source identity, row count and absence of mixed-source content remain unchecked.
    Descriptors supplied by callers are deliberately not accepted as access capabilities.
    """
    descriptors = authorize_payloads(
        artifact_ids,
        index=index,
        manifest=manifest,
        registry=registry,
        context=context,
        moves=moves,
        looks=looks,
    )
    return _retrieve(descriptors, opener)


def _retrieve(
    descriptors: Sequence[AuthorizedPayload], opener: Callable[[AuthorizedPayload], bytes]
) -> Iterator[VerifiedPayload]:
    for descriptor in descriptors:
        artifact = descriptor.artifact
        try:
            payload = opener(descriptor)
        except OSError as exc:
            raise SourceAccessError(
                f"{artifact.artifact_id}: cannot retrieve payload: {exc}"
            ) from exc
        if not isinstance(payload, bytes):
            raise SourceAccessError(f"{artifact.artifact_id}: opener must return bytes")
        _require_hash(
            hashlib.sha256(payload).hexdigest(),
            artifact.payload_sha256,
            "payload_sha256",
            artifact.artifact_id,
        )
        verified = VerifiedPayload(descriptor=descriptor, payload=payload)
        # Drop the local name so a caller that releases a payload holds only the next one.
        del payload
        yield verified
        del verified


def read_payloads(
    artifact_ids: Sequence[str],
    *,
    index: SecIndex,
    manifest: SourceManifest,
    registry: PoolRegistry,
    context: AccessContext,
    moves: Sequence[Move],
    looks: Sequence[Look],
    opener: Callable[[AuthorizedPayload], bytes],
) -> tuple[VerifiedPayload, ...]:
    """`iter_payloads` collected: every selected payload is held in memory until it returns.

    Authorizes all selections, then retrieves and hashes the permitted bytes; never returns
    a partial tuple. Manifests record no payload size, so no byte limit can be checked
    before opening; use `iter_payloads` when the selection may not fit in memory.
    """
    return tuple(
        iter_payloads(
            artifact_ids,
            index=index,
            manifest=manifest,
            registry=registry,
            context=context,
            moves=moves,
            looks=looks,
            opener=opener,
        )
    )
