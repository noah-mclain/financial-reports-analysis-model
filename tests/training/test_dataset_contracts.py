"""Synthetic normalization supervision and issuer authorization boundaries."""

import json
from dataclasses import replace
from datetime import date

import pytest
from dataset_contracts import (
    DatasetError,
    NormalizationExample,
    PartitionConfig,
    PartitionedExamples,
    SourceRecord,
    Verification,
    partition_examples,
)
from pydantic import ValidationError

from fra_core.pools import Identity, Pool, PoolRegistry, Source
from fra_core.schemas.statement import BBox, Provenance, StatementType
from fra_core.split import Look, Move, Part, SplitError, cik_key, hashed_part, issuer_key
from fra_core.taxonomy import load_taxonomy

HASH = "a" * 64


def cik_in(part: Part) -> int:
    return next(c for c in range(1, 1000) if hashed_part(cik_key(c)) == part)


def source(cik: int, *, name: str = "Synthetic Widgets", **changes: object) -> SourceRecord:
    return SourceRecord.model_validate(
        {
            "source_id": f"filing-{cik}",
            "issuer": Identity(name, cik),
            "pool": Pool.TRAIN,
            "source": Source.SEC,
            "language": "en",
            "period": "annual",
            "period_end": date(2025, 12, 31),
            "source_sha256": HASH,
            "config_sha256": HASH,
            **changes,
        }
    )


def example(record: SourceRecord, **changes: object) -> NormalizationExample:
    return NormalizationExample.model_validate(
        {
            "example_id": f"example-{record.source_id}",
            "source_record": record,
            "statement": StatementType.INCOME,
            "label": "Net sales",
            "canonical_id": "revenue",
            "abstention_reason": None,
            "role": "corporate",
            "months": 12,
            "source_locator": "accession:synthetic/pre:1",
            "tag": "us-gaap:Revenues",
            "provenance": None,
            "verification": Verification(
                method="independent_review",
                producer_id="preparer",
                reviewer_id="reviewer",
                evidence_id="review-record",
                evidence_sha256=HASH,
            ),
            **changes,
        }
    )


def registry_for(*records: SourceRecord) -> PoolRegistry:
    registry = PoolRegistry()
    for record in records:
        registry.register(record.issuer, record.pool)
    return registry


def config(*examples: NormalizationExample, cap: int | None = None) -> PartitionConfig:
    return PartitionConfig(
        approved_example_sha256=frozenset(e.content_sha256() for e in examples),
        max_per_issuer=cap,
    )


def build(
    records: tuple[SourceRecord, ...],
    examples: tuple[NormalizationExample, ...],
    *,
    registry: PoolRegistry | None = None,
    settings: PartitionConfig | None = None,
    moves: tuple[Move, ...] = (),
    looks: tuple[Look, ...] = (),
) -> PartitionedExamples:
    return partition_examples(
        examples,
        sources=records,
        registry=registry if registry is not None else registry_for(*records),
        config=settings if settings is not None else config(*examples),
        taxonomy=load_taxonomy(),
        moves=moves,
        looks=looks,
    )


def test_partitions_are_reproducible_and_manifest_retains_provenance() -> None:
    fit = source(cik_in(Part.FIT))
    valid = source(cik_in(Part.VALIDATION), name="Other Widgets")
    rows = (example(fit), example(valid))
    result = build((fit, valid), rows)
    assert result.fit == (rows[0],)
    assert result.validation == (rows[1],)
    assert result == build((valid, fit), tuple(reversed(rows)))
    assert result.manifest.fit_sha256 != result.manifest.validation_sha256
    changed = example(
        fit, verification=rows[0].verification.model_copy(update={"evidence_sha256": "b" * 64})
    )
    assert (
        build((fit,), (changed,)).manifest.fit_sha256
        != build((fit,), (rows[0],)).manifest.fit_sha256
    )


@pytest.mark.parametrize("pool", [Pool.DEV, Pool.MODEL_TEST, Pool.BLIND])
def test_non_train_roster_is_refused_before_empty_input_filtering(pool: Pool) -> None:
    record = source(cik_in(Part.FIT), pool=pool)
    with pytest.raises(DatasetError, match="only train"):
        build((record,), ())


def test_holdout_metadata_does_not_materialize_examples() -> None:
    record = source(cik_in(Part.HOLDOUT))
    assert build((record,), ()).fit == ()
    with pytest.raises(DatasetError, match="holdout"):
        build((record,), (example(record),))


@pytest.mark.parametrize("kind", ["override", "spent_look"])
def test_recorded_moves_use_shared_contracts(kind: str) -> None:
    record = source(cik_in(Part.HOLDOUT))
    row = example(record)
    looks = (
        ()
        if kind == "override"
        else (Look("2026-10-01", "dry_run", ("holdout",), ("en",), "synthetic"),)
    )
    move = Move(
        "SYNTHETIC WIDGETS CO. PLC",
        issuer_key("Synthetic Widgets"),
        kind,
        "holdout",
        "fit",
        "2026-10-02",
        len(looks),
        "synthetic review",
    )
    assert build((record,), (row,), moves=(move,), looks=looks).fit == (row,)
    mismatched = replace(move, issuer="Unrelated Issuer")
    with pytest.raises(SplitError, match="normalized issuer"):
        build((record,), (row,), moves=(mismatched,), looks=looks)
    invalid_looks = (
        () if looks else (Look("2026-10-01", "dry_run", ("holdout",), ("en",), "synthetic"),)
    )
    with pytest.raises(ValueError, match=r"override|logged|look"):
        build((record,), (row,), moves=(move,), looks=invalid_looks)


@pytest.mark.parametrize("kind", ["override", "spent_look"])
def test_name_key_move_releases_reviewed_cik_aliases_without_mutating_registry(kind: str) -> None:
    cik = cik_in(Part.HOLDOUT)
    record = source(cik)
    alias = source(cik, name="Historical Rename", source_id="alias-filing")
    registry = registry_for(record, alias)
    before = registry.metadata()
    rows = (example(record), example(alias))
    looks = (Look("2026-10-02", "dry_run", ("holdout",), ("en",), "synthetic"),)
    move = Move(
        "SYNTHETIC WIDGETS CO. PLC",
        issuer_key("Synthetic Widgets"),
        kind,
        "holdout",
        "fit",
        "2026-10-01" if kind == "override" else "2026-10-02",
        int(kind == "spent_look"),
        "synthetic review",
    )
    result = build((record, alias), rows, registry=registry, moves=(move,), looks=looks)
    assert len(result.fit) == len(rows)
    assert all(row in result.fit for row in rows)
    assert registry.metadata() == before


def test_registry_is_authoritative_and_is_not_mutated() -> None:
    record = source(cik_in(Part.FIT))
    registry = registry_for(record)
    original = registry.metadata()
    build((record,), (example(record),), registry=registry)
    assert registry.metadata() == original
    with pytest.raises(DatasetError, match="recorded"):
        build((record,), (example(record),), registry=PoolRegistry())
    forbidden = PoolRegistry()
    forbidden.register(record.issuer, Pool.BLIND)
    with pytest.raises(ValueError, match="spans pools"):
        build((record,), (example(record),), registry=forbidden)
    new_alias = source(cik_in(Part.FIT), name="Unreviewed Rename")
    with pytest.raises(DatasetError, match="recorded"):
        build((new_alias,), (example(new_alias),), registry=registry)


def test_cik_aliases_keep_year_language_and_source_in_one_partition() -> None:
    cik = cik_in(Part.FIT)
    sec = source(cik)
    pdf = source(
        cik,
        name="Reviewed Rename",
        source_id="pdf-2024",
        source=Source.PDF,
        language="ar",
        period="interim",
        period_end=date(2024, 6, 30),
    )
    registry = registry_for(sec)
    registry.alias("Reviewed Rename", sec.issuer)
    pdf_row = example(
        pdf,
        label="الإيرادات",
        tag=None,
        months=6,
        provenance=Provenance(
            page_no=1,
            bbox=BBox(left=0, top=0, right=10, bottom=10),
            table_ref="table-1",
            row=1,
            col=0,
        ),
    )
    result = build((sec, pdf), (example(sec), pdf_row), registry=registry)
    assert len(result.fit) == 2
    assert not result.validation
    assert source(cik, issuer=Identity("Synthetic Widgets", f"{cik:010d}")).issuer == sec.issuer


def test_identity_conflicts_and_name_only_alias_boundary_fail_loudly() -> None:
    record = source(cik_in(Part.FIT))
    other = source(cik_in(Part.VALIDATION))
    with pytest.raises(ValueError, match="CIKs"):
        registry_for(record, other)
    no_cik = source(cik_in(Part.FIT), issuer=Identity("Reviewed Name"))
    with pytest.raises(DatasetError, match="CIK"):
        build((no_cik,), (example(no_cik),))
    ambiguous = PoolRegistry(ambiguous_names=(record.issuer.name,))
    ambiguous.register(record.issuer, Pool.TRAIN)
    ambiguous.register(other.issuer, Pool.TRAIN)
    with pytest.raises(ValueError, match="keyed both"):
        build((record, other), (), registry=ambiguous)


def test_draft_and_self_declared_review_are_not_trusted_even_when_capped() -> None:
    record = source(cik_in(Part.FIT))
    trusted = example(record, example_id="a")
    draft = example(
        record,
        example_id="z",
        verification=Verification(
            method="extraction_draft",
            producer_id="extractor",
            reviewer_id=None,
            evidence_id="extraction",
            evidence_sha256=HASH,
        ),
    )
    with pytest.raises(DatasetError, match="draft"):
        build((record,), (trusted, draft), settings=config(trusted, draft, cap=1))
    with pytest.raises(DatasetError, match="approved"):
        build((record,), (trusted,), settings=config())
    altered = example(record, example_id="a", canonical_id="net_income")
    with pytest.raises(DatasetError, match="approved"):
        build((record,), (altered,), settings=config(trusted))
    with pytest.raises(ValidationError, match="independent"):
        Verification(
            method="independent_review",
            producer_id="same",
            reviewer_id="same",
            evidence_id="review",
            evidence_sha256=HASH,
        )


def test_original_duplicate_authorization_precedes_deduplication() -> None:
    record = source(cik_in(Part.FIT))
    trusted = example(record)
    draft = example(
        record,
        verification=Verification(
            method="extraction_draft",
            producer_id="extractor",
            reviewer_id=None,
            evidence_id="extraction",
            evidence_sha256=HASH,
        ),
    )
    with pytest.raises(DatasetError, match="draft"):
        build((record,), (trusted, draft), settings=config(trusted, draft))
    result = build((record,), (trusted, trusted))
    assert result.fit == (trusted,)
    assert result.manifest.input_examples == 2
    distinct = example(record, example_id="z", source_locator="pre:2")
    assert len(build((record,), (trusted, distinct)).fit) == 2
    assert (
        len(build((record,), (trusted, distinct), settings=config(trusted, distinct, cap=1)).fit)
        == 1
    )
    conflict = example(record, source_locator="pre:2")
    with pytest.raises(DatasetError, match="example_id"):
        build((record,), (trusted, conflict))


def test_source_identity_and_hash_must_match_authorized_roster() -> None:
    record = source(cik_in(Part.FIT))
    forged = source(cik_in(Part.FIT), source_sha256="b" * 64)
    with pytest.raises(DatasetError, match="source record"):
        build((record,), (example(forged),))
    with pytest.raises(DatasetError, match="source_id"):
        build((record, forged), ())


@pytest.mark.parametrize(
    "changes", [{"canonical_id": "invented"}, {"canonical_id": "total_assets"}]
)
def test_targets_are_checked_against_injected_taxonomy(changes: dict[str, object]) -> None:
    record = source(cik_in(Part.FIT))
    with pytest.raises(DatasetError, match="canonical"):
        build((record,), (example(record, **changes),))


def test_null_abstention_is_explicit_and_reviewed() -> None:
    record = source(cik_in(Part.FIT))
    row = example(record, canonical_id=None, abstention_reason="outside reviewed taxonomy")
    assert build((record,), (row,)).fit[0].canonical_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_id": None},
        {"abstention_reason": "unsure"},
        {"role": "negative_control"},
        {"tag": None},
        {"label": " "},
        {"example_id": ""},
        {"months": True},
    ],
)
def test_invalid_example_combinations(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        example(source(cik_in(Part.FIT)), **changes)


@pytest.mark.parametrize(
    "changes", [{"source_sha256": "invalid"}, {"config_sha256": "A" * 64}, {"language": "ar"}]
)
def test_invalid_sec_source_metadata(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        source(cik_in(Part.FIT), **changes)


def test_pdf_requires_real_provenance_and_duration_rules() -> None:
    record = source(cik_in(Part.FIT), source=Source.PDF)
    with pytest.raises(ValidationError, match="provenance"):
        example(record, tag=None)
    row = example(
        source(cik_in(Part.FIT)),
        statement=StatementType.BALANCE,
        canonical_id="total_assets",
        months=None,
    )
    assert row.months is None
    with pytest.raises(ValidationError, match="months"):
        example(
            row.source_record,
            statement=StatementType.BALANCE,
            canonical_id="total_assets",
            months=12,
        )


def test_numeric_payload_and_missing_explicit_target_are_refused() -> None:
    row = example(source(cik_in(Part.FIT)))
    with pytest.raises(ValidationError, match="extra"):
        example(row.source_record, value="123.45")
    data = row.model_dump(mode="json")
    del data["canonical_id"]
    with pytest.raises(ValidationError, match="canonical_id"):
        NormalizationExample.model_validate_json(json.dumps(data))
    assert NormalizationExample.model_validate_json(row.model_dump_json()) == row


def test_crosswalk_and_negative_control_remain_review_bound() -> None:
    record = source(cik_in(Part.FIT))
    row = example(
        record,
        canonical_id=None,
        abstention_reason="reviewed financial issuer decline",
        role="negative_control",
        verification=Verification(
            method="reviewed_crosswalk",
            producer_id="preparer",
            reviewer_id="reviewer",
            evidence_id="crosswalk-v1",
            evidence_sha256=HASH,
        ),
    )
    assert build((record,), (row,)).fit == (row,)
    with pytest.raises(ValidationError, match=r"SHA|pattern|string_pattern"):
        Verification(
            method="independent_review",
            producer_id="preparer",
            reviewer_id="reviewer",
            evidence_id="review",
            evidence_sha256="bad",
        )


@pytest.mark.parametrize("cap", [0, -1, True])
def test_invalid_cap_is_refused(cap: object) -> None:
    with pytest.raises(ValidationError):
        PartitionConfig.model_validate(
            {"approved_example_sha256": frozenset(), "max_per_issuer": cap}
        )


def test_unvalidated_model_copies_cannot_bypass_boundary_validation() -> None:
    record = source(cik_in(Part.FIT))
    row = example(record)
    forged = record.model_copy(update={"source_sha256": "bad"})
    with pytest.raises(ValidationError, match="source_sha256"):
        build((forged,), (), settings=config())
    malformed = row.model_copy(update={"canonical_id": None})
    with pytest.raises(ValidationError, match="abstention"):
        build((record,), (malformed,), settings=config())
