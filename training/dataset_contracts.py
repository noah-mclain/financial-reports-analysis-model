"""Injected normalization supervision contracts; no data loading or scoring."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from fra_core.pools import Identity, Pool, PoolRegistry, Source
from fra_core.schemas.statement import BBox, Provenance, StatementType
from fra_core.split import (
    Look,
    Move,
    Part,
    check_looks,
    check_moves,
    cik_key,
    development_documents,
)
from fra_core.taxonomy import CanonicalItem, Taxonomy

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class DatasetError(ValueError):
    """Invalid supervision, authorization or dataset identity."""


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


class SourceRecord(Contract):
    """Caller-supplied source metadata, checked against a reviewed registry."""

    source_id: Text
    issuer: Identity
    pool: Pool
    source: Source
    language: Literal["en", "ar"]
    period: Literal["annual", "interim"]
    period_end: date
    source_sha256: Sha256
    config_sha256: Sha256

    @model_validator(mode="after")
    def _source_language(self) -> SourceRecord:
        if Identity(self.issuer.name, self.issuer.cik) != self.issuer:
            raise ValueError(f"{self.source_id}: issuer identity is not normalized")
        if self.source == Source.SEC and self.language != "en":
            raise ValueError(f"{self.source_id}: SEC presentation labels must be English")
        return self


class Verification(Contract):
    """Evidence description, which alone does not authorize supervision."""

    method: Literal["extraction_draft", "reviewed_crosswalk", "independent_review"]
    producer_id: Text
    reviewer_id: Text | None
    evidence_id: Text
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def _independence(self) -> Verification:
        if self.method == "extraction_draft":
            if self.reviewer_id is not None:
                raise ValueError("extraction draft cannot claim a reviewer")
        elif self.reviewer_id is None or self.reviewer_id.strip() == self.producer_id.strip():
            raise ValueError("verification requires an independent reviewer")
        return self


class NormalizationExample(Contract):
    """One label/target with source and verification identity; no numeric payload."""

    example_id: Text
    source_record: SourceRecord
    statement: StatementType
    label: Text
    canonical_id: Text | None
    abstention_reason: Text | None
    role: Literal["corporate", "negative_control"]
    months: int | None = Field(ge=1, le=36)
    source_locator: Text
    tag: Text | None
    provenance: Provenance | None
    verification: Verification

    @model_validator(mode="after")
    def _combinations(self) -> NormalizationExample:
        if (self.canonical_id is None) != (self.abstention_reason is not None):
            raise ValueError(f"{self.example_id}: null target requires an abstention reason only")
        if self.role == "negative_control" and self.canonical_id is not None:
            raise ValueError(f"{self.example_id}: negative control must abstain")
        if self.statement == StatementType.BALANCE:
            if self.months is not None:
                raise ValueError(f"{self.example_id}: instant balance has no duration months")
        elif self.months is None:
            raise ValueError(f"{self.example_id}: flow statement needs column duration months")
        if self.source_record.source == Source.SEC:
            if self.tag is None or self.provenance is not None:
                raise ValueError(
                    f"{self.example_id}: SEC needs a tag and cannot claim PDF provenance"
                )
        elif self.provenance is None or self.tag is not None:
            raise ValueError(f"{self.example_id}: PDF needs provenance and cannot claim an SEC tag")
        if self.provenance is not None:
            # model_copy/model_construct can leave an unvalidated nested value behind: refuse
            # any provenance whose strict representation differs from what is stored and hashed.
            stored = self.provenance.model_dump(mode="json", warnings=False)
            try:
                checked = Provenance.model_validate(self.provenance.model_dump(warnings=False))
            except ValueError as exc:
                raise ValueError(f"{self.example_id}: provenance is not valid: {exc}") from exc
            bbox = checked.bbox
            if not all(math.isfinite(v) for v in (bbox.left, bbox.top, bbox.right, bbox.bottom)):
                raise ValueError(f"{self.example_id}: provenance coordinates must be finite")
            if not isinstance(self.provenance.bbox, BBox) or canonical_json(
                checked.model_dump(mode="json")
            ) != canonical_json(stored):
                raise ValueError(f"{self.example_id}: provenance differs from its validated form")
            if not checked.table_ref.strip():
                raise ValueError(f"{self.example_id}: provenance needs a table reference")
        if (
            self.verification.method == "reviewed_crosswalk"
            and self.source_record.source != Source.SEC
        ):
            raise ValueError(f"{self.example_id}: reviewed tag crosswalk applies only to SEC")
        return self

    def content_sha256(self) -> str:
        """Bind approval to every field, including target, source hashes and review evidence."""
        return _digest(self.model_dump(mode="json"))


class PartitionConfig(Contract):
    """Approvals established independently by the caller, plus an optional issuer cap."""

    approved_example_sha256: frozenset[Sha256]
    max_per_issuer: int | None = Field(default=None, ge=1)


class DatasetManifest(Contract):
    version: Literal[1] = 1
    input_examples: int
    input_sha256: Sha256
    sources_sha256: Sha256
    registry_sha256: Sha256
    split_records_sha256: Sha256
    taxonomy_sha256: Sha256
    config_sha256: Sha256
    fit_sha256: Sha256
    validation_sha256: Sha256


class PartitionedExamples(Contract):
    fit: tuple[NormalizationExample, ...]
    validation: tuple[NormalizationExample, ...]
    manifest: DatasetManifest


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _target_items(taxonomy: Taxonomy) -> dict[str, CanonicalItem]:
    # Targets resolve against the same items the manifest fingerprints, never the derived index.
    targets = {item.id: item for item in taxonomy.items}
    if len(targets) != len(taxonomy.items):
        raise DatasetError("canonical taxonomy has duplicate ids")
    return targets


def example_digest(examples: Sequence[NormalizationExample]) -> str:
    # Sorting full records retains duplicate occurrences in the input fingerprint.
    return _digest(sorted(canonical_json(e.model_dump(mode="json")) for e in examples))


def _taxonomy_digest(taxonomy: Taxonomy) -> str:
    return _digest(
        {
            "version": taxonomy.version,
            "items": [
                {
                    "id": item.id,
                    "statement": item.statement,
                    "natural_sign": item.natural_sign,
                    "critical": item.critical,
                    "subtotal": item.subtotal,
                    "per_share": item.per_share,
                    "aliases": dict(item.aliases),
                    "headings": dict(item.headings),
                }
                for item in sorted(taxonomy.items, key=lambda item: item.id)
            ],
        }
    )


def partition_examples(
    examples: Sequence[NormalizationExample],
    *,
    sources: Sequence[SourceRecord],
    registry: PoolRegistry,
    config: PartitionConfig,
    taxonomy: Taxonomy,
    moves: Sequence[Move],
    looks: Sequence[Look],
) -> PartitionedExamples:
    """Return fit/validation supervision only; reject every unauthorized original row.

    ``sources`` is an authoritative TRAIN metadata roster including move targets, not a
    selection inferred from examples. Callers must select permitted content using metadata
    before opening labels. No assignments or scoring records are changed by this function.
    Name-only identities are refused pending a shared public alias/split-key contract.
    """
    # Revalidate model instances too: model_copy/model_construct can bypass field validation.
    config = PartitionConfig.model_validate(config)
    sources = tuple(SourceRecord.model_validate(s) for s in sources)
    examples = tuple(NormalizationExample.model_validate(e) for e in examples)
    check_looks(looks)
    check_moves(moves, looks)
    targets = _target_items(taxonomy)
    metadata = registry.metadata()
    by_source: dict[str, SourceRecord] = {}
    documents: list[dict[str, object]] = []
    for record in sources:
        if record.pool != Pool.TRAIN:
            raise DatasetError(f"{record.source_id}: only train sources are authorized")
        if record.issuer.cik is None:
            raise DatasetError(
                f"{record.source_id}: reviewed CIK required for alias-safe splitting"
            )
        probe = PoolRegistry.from_metadata(metadata, "injected registry")
        probe.register(record.issuer, record.pool)
        if probe.metadata() != metadata:
            raise DatasetError(
                f"{record.source_id}: identity/pool is not fully recorded in registry"
            )
        if record.source_id in by_source:
            raise DatasetError(f"{record.source_id}: duplicate source_id in metadata roster")
        by_source[record.source_id] = record
        documents.append(
            {
                "id": record.source_id,
                "issuer": record.issuer.name or cik_key(record.issuer.cik),
                "cik": record.issuer.cik,
                "pool": record.pool,
            }
        )
    source_parts: dict[str, Part] = {}
    for part in (Part.FIT, Part.VALIDATION):
        for document in development_documents(documents, moves, {part}):
            source_id = str(document["id"])
            if source_id in source_parts:
                raise DatasetError(f"{source_id}: source overlaps fit and validation")
            source_parts[source_id] = part

    # Authorize all original examples before deduplication or caps can discard anything.
    for row in examples:
        source_id = row.source_record.source_id
        if row.source_record != by_source.get(source_id):
            raise DatasetError(f"{row.example_id}: source record differs from authorized roster")
        if source_id not in source_parts:
            raise DatasetError(f"{row.example_id}: holdout source is excluded from supervision")
        if row.verification.method == "extraction_draft":
            raise DatasetError(f"{row.example_id}: extraction draft is not trusted supervision")
        if row.content_sha256() not in config.approved_example_sha256:
            raise DatasetError(f"{row.example_id}: exact example content has not been approved")
        if row.canonical_id is not None:
            item = targets.get(row.canonical_id)
            if item is None or item.statement != row.statement:
                raise DatasetError(
                    f"{row.example_id}: invalid canonical target for {row.statement}"
                )

    unique: dict[str, NormalizationExample] = {}
    for row in examples:
        previous = unique.setdefault(row.example_id, row)
        if previous != row:
            raise DatasetError(f"{row.example_id}: example_id identifies conflicting content")
    selected: dict[Part, list[NormalizationExample]] = {Part.FIT: [], Part.VALIDATION: []}
    counts: Counter[str] = Counter()
    issuer_parts: dict[str, Part] = {}
    for row in sorted(unique.values(), key=lambda row: row.example_id):
        part = source_parts[row.source_record.source_id]
        # CIK is required above; use the core key contract rather than a name alias.
        key = cik_key(str(row.source_record.issuer.cik))
        if issuer_parts.setdefault(key, part) != part:
            raise DatasetError(f"{row.example_id}: issuer overlaps fit and validation")
        if config.max_per_issuer is None or counts[key] < config.max_per_issuer:
            selected[part].append(row)
            counts[key] += 1
    fit, validation = tuple(selected[Part.FIT]), tuple(selected[Part.VALIDATION])
    return PartitionedExamples(
        fit=fit,
        validation=validation,
        manifest=DatasetManifest(
            input_examples=len(examples),
            input_sha256=example_digest(examples),
            sources_sha256=_digest(
                [s.model_dump(mode="json") for s in sorted(sources, key=lambda s: s.source_id)]
            ),
            registry_sha256=_digest(metadata),
            split_records_sha256=_digest(
                {"moves": [asdict(m) for m in moves], "looks": [asdict(look) for look in looks]}
            ),
            taxonomy_sha256=_taxonomy_digest(taxonomy),
            config_sha256=_digest(
                {
                    "approved_example_sha256": sorted(config.approved_example_sha256),
                    "max_per_issuer": config.max_per_issuer,
                }
            ),
            fit_sha256=example_digest(fit),
            validation_sha256=example_digest(validation),
        ),
    )
