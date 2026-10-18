"""Synthetic normalization supervision and issuer authorization boundaries."""

import json
import math
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
    # SEC presentation rows have no column duration; a PDF flow statement needs one.
    statement = changes.get("statement", StatementType.INCOME)
    instant = record.source == Source.SEC or statement == StatementType.BALANCE
    return NormalizationExample.model_validate(
        {
            "example_id": f"example-{record.source_id}",
            "source_record": record,
            "statement": StatementType.INCOME,
            "label": "Net sales",
            "canonical_id": "revenue",
            "abstention_reason": None,
            "role": "corporate",
            "months": None if instant else 12,
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


def test_partition_progress_hook_does_not_change_output() -> None:
    fit = source(cik_in(Part.FIT))
    valid = source(cik_in(Part.VALIDATION), name="Other Widgets")
    rows = (example(fit), example(valid))
    calls = 0

    def on_example() -> None:
        nonlocal calls
        calls += 1

    plain = build((fit, valid), rows)
    hooked = partition_examples(
        rows,
        sources=(fit, valid),
        registry=registry_for(fit, valid),
        config=config(*rows),
        taxonomy=load_taxonomy(),
        moves=(),
        looks=(),
        on_example=on_example,
    )
    assert hooked == plain
    assert calls == 3 * len(rows)


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


def _many_sources(count: int) -> tuple[SourceRecord, ...]:
    return tuple(source(cik, name=f"Issuer {cik}") for cik in range(1, count + 1))


def test_roster_check_does_not_rebuild_the_registry_per_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _many_sources(40)
    registry = registry_for(*records)
    rebuilt = 0
    original = PoolRegistry.from_metadata.__func__  # type: ignore[attr-defined]

    def counting(cls: type[PoolRegistry], data: object, label: str) -> PoolRegistry:
        nonlocal rebuilt
        rebuilt += 1
        return original(cls, data, label)  # type: ignore[no-any-return]

    monkeypatch.setattr(PoolRegistry, "from_metadata", classmethod(counting))
    build(records, (), registry=registry)
    assert rebuilt <= 2


def test_unrecorded_source_among_recorded_ones_is_named() -> None:
    records = _many_sources(10)
    registry = registry_for(*records[:4], *records[5:])
    with pytest.raises(DatasetError, match=records[4].source_id):
        build(records, (), registry=registry)


def test_first_unrecorded_source_is_named_even_when_a_later_one_conflicts() -> None:
    records = _many_sources(6)
    registry = registry_for(*records[:2], *records[3:5])
    # source 2 is unrecorded; the last source reuses its name under another CIK, which only
    # conflicts once source 2 has been registered
    clash = source(records[5].issuer.cik or 1, name=records[2].issuer.name or "x")
    with pytest.raises((DatasetError, ValueError), match=records[2].source_id):
        build((*records[:5], clash), (), registry=registry)


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


def test_months_rule_depends_on_the_source() -> None:
    sec = source(cik_in(Part.FIT))
    pdf = source(cik_in(Part.FIT), source=Source.PDF)
    bbox = BBox(left=0, top=0, right=10, bottom=10)
    provenance = Provenance(page_no=1, bbox=bbox, table_ref="table-1", row=1, col=0)
    pdf_args: dict[str, object] = {"tag": None, "provenance": provenance}
    # SEC presentation rows have no column: months is None for every statement.
    for statement, target in (
        (StatementType.INCOME, "revenue"),
        (StatementType.BALANCE, "total_assets"),
    ):
        assert example(sec, statement=statement, canonical_id=target).months is None
        with pytest.raises(ValidationError, match="SEC"):
            example(sec, statement=statement, canonical_id=target, months=12)
    # PDF flow statements keep requiring months, and balance sheets still forbid them.
    assert example(pdf, **pdf_args).months == 12
    with pytest.raises(ValidationError, match="months"):
        example(pdf, months=None, **pdf_args)
    with pytest.raises(ValidationError, match="instant"):
        example(
            pdf, statement=StatementType.BALANCE, canonical_id="total_assets", months=12, **pdf_args
        )
    assert (
        example(
            pdf, statement=StatementType.BALANCE, canonical_id="total_assets", **pdf_args
        ).months
        is None
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


@pytest.mark.parametrize("kind", ["override", "spent_look"])
def test_move_naming_one_issuer_cannot_release_another_issuers_holdout_key(kind: str) -> None:
    holdout = source(cik_in(Part.HOLDOUT), name="Holdout Widgets")
    fit = source(cik_in(Part.FIT), name="Fit Widgets")
    looks = (
        ()
        if kind == "override"
        else (Look("2026-10-01", "dry_run", ("holdout",), ("en",), "synthetic"),)
    )
    rows = (example(holdout), example(fit))
    forged = Move(
        "Fit Widgets",
        issuer_key("Holdout Widgets"),
        kind,
        "holdout",
        "fit",
        "2026-10-02",
        len(looks),
        "synthetic review",
    )
    with pytest.raises(SplitError, match="normalized issuer"):
        build((holdout, fit), rows, moves=(forged,), looks=looks)
    with pytest.raises(SplitError, match="normalized issuer"):
        build((holdout, fit), (rows[1],), moves=(forged,), looks=looks)


def pdf_provenance() -> Provenance:
    return Provenance(
        page_no=1, bbox=BBox(left=0, top=0, right=10, bottom=10), table_ref="table-1", row=1, col=0
    )


def pdf_row(provenance: Provenance) -> tuple[SourceRecord, NormalizationExample]:
    record = source(cik_in(Part.FIT), source=Source.PDF)
    return record, example(record, tag=None, provenance=provenance)


def test_genuine_pdf_provenance_is_accepted_and_kept() -> None:
    record, row = pdf_row(pdf_provenance())
    assert build((record,), (row,)).fit[0].provenance == pdf_provenance()


@pytest.mark.parametrize("field", ["left", "top", "right", "bottom"])
def test_forged_nested_bbox_cannot_stay_on_an_approved_example(field: str) -> None:
    good = pdf_provenance()
    forged_box = good.bbox.model_copy(update={field: "0"})
    forged = good.model_copy(update={"bbox": forged_box})
    record, row = pdf_row(good)
    with pytest.raises(ValidationError, match="provenance"):
        example(record, tag=None, provenance=forged)
    malformed = row.model_copy(update={"provenance": forged})
    with pytest.raises(ValidationError, match="provenance"):
        build((record,), (malformed,), settings=config(row))


def test_constructed_provenance_that_skips_validation_is_refused() -> None:
    good = pdf_provenance()
    record, row = pdf_row(good)
    constructed = Provenance.model_construct(
        page_no="1", bbox=good.bbox, table_ref="table-1", row=1, col=0, source=good.source
    )
    with pytest.raises(ValidationError, match="provenance"):
        build(
            (record,), (row.model_copy(update={"provenance": constructed}),), settings=config(row)
        )
    unordered = good.model_copy(
        update={"bbox": BBox.model_construct(left=5, top=0, right=1, bottom=9)}
    )
    with pytest.raises(ValidationError, match="provenance"):
        build((record,), (row.model_copy(update={"provenance": unordered}),), settings=config(row))


def test_targets_come_from_fingerprinted_taxonomy_items_not_derived_indexes() -> None:
    record = source(cik_in(Part.FIT))
    row = example(record)
    taxonomy = load_taxonomy()
    without_revenue = replace(taxonomy, items=tuple(i for i in taxonomy.items if i.id != "revenue"))
    assert without_revenue.by_id("revenue") is not None
    with pytest.raises(DatasetError, match="canonical"):
        partition_examples(
            (row,),
            sources=(record,),
            registry=registry_for(record),
            config=config(row),
            taxonomy=without_revenue,
            moves=(),
            looks=(),
        )
    emptied_index = replace(taxonomy, _by_id={})
    result = partition_examples(
        (row,),
        sources=(record,),
        registry=registry_for(record),
        config=config(row),
        taxonomy=emptied_index,
        moves=(),
        looks=(),
    )
    assert result.fit == (row,)


def test_duplicate_taxonomy_ids_are_refused() -> None:
    record = source(cik_in(Part.FIT))
    row = example(record)
    taxonomy = load_taxonomy()
    duplicated = replace(taxonomy, items=(*taxonomy.items, taxonomy.items[0]))
    with pytest.raises(DatasetError, match="duplicate"):
        partition_examples(
            (row,),
            sources=(record,),
            registry=registry_for(record),
            config=config(row),
            taxonomy=duplicated,
            moves=(),
            looks=(),
        )


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_a_non_finite_provenance_coordinate_names_its_example(value: float) -> None:
    good = pdf_provenance()
    record, _row = pdf_row(good)
    forged = good.model_copy(update={"bbox": good.bbox.model_copy(update={"right": value})})
    with pytest.raises(ValidationError, match="provenance"):
        example(record, tag=None, provenance=forged)


def test_a_bbox_left_as_a_plain_mapping_is_refused() -> None:
    good = pdf_provenance()
    record, _row = pdf_row(good)
    loose = good.model_copy(update={"bbox": good.bbox.model_dump()})
    with pytest.raises(ValidationError, match="provenance differs"):
        example(record, tag=None, provenance=loose)
