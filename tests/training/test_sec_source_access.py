"""Synthetic metadata authorization: forbidden payloads must never reach the opener."""

import hashlib
from dataclasses import dataclass, replace
from datetime import date

import pytest
from dataset_contracts import SourceRecord
from pydantic import ValidationError
from sec_source_access import (
    AccessContext,
    AuthorizedPayload,
    PayloadArtifact,
    SecFiling,
    SecIndex,
    SourceAccessError,
    SourceManifest,
    authorize_payloads,
    read_payloads,
    registry_sha256,
    split_records_sha256,
)

from fra_core.pools import Identity, Pool, PoolRegistry, Source
from fra_core.split import Look, Move, Part, SplitError, cik_key, hashed_part, issuer_key

HASH = "a" * 64
OTHER_HASH = "b" * 64


def cik_in(part: Part) -> int:
    return next(c for c in range(1, 1000) if hashed_part(cik_key(c)) == part)


def filing(cik: int, *, name: str | None = None, suffix: str = "") -> SecFiling:
    source_id = f"filing-{cik}{suffix}"
    return SecFiling(
        source_record=SourceRecord(
            source_id=source_id,
            issuer=Identity(name or f"Synthetic Filer {cik}", cik),
            pool=Pool.TRAIN,
            source=Source.SEC,
            language="en",
            period="annual",
            period_end=date(2025, 12, 31),
            source_sha256=HASH,
            config_sha256=HASH,
        ),
        raw_locator=f"synthetic-accession:{source_id}",
    )


def artifact(record: SecFiling) -> PayloadArtifact:
    source_id = record.source_record.source_id
    return PayloadArtifact(
        artifact_id=f"payload-{source_id}",
        source_id=source_id,
        relative_path=f"filings/{source_id}.jsonl",
        payload_sha256=hashlib.sha256(source_id.encode()).hexdigest(),
        row_count=1,
    )


@dataclass
class Fixture:
    index: SecIndex
    manifest: SourceManifest
    registry: PoolRegistry
    context: AccessContext
    moves: tuple[Move, ...] = ()
    looks: tuple[Look, ...] = ()

    def authorize(self, ids: tuple[str, ...]) -> tuple[AuthorizedPayload, ...]:
        return authorize_payloads(
            ids,
            index=self.index,
            manifest=self.manifest,
            registry=self.registry,
            context=self.context,
            moves=self.moves,
            looks=self.looks,
        )

    def read(self, ids: tuple[str, ...], spy: "Spy") -> None:
        read_payloads(
            ids,
            index=self.index,
            manifest=self.manifest,
            registry=self.registry,
            context=self.context,
            moves=self.moves,
            looks=self.looks,
            opener=spy,
        )


class Spy:
    def __init__(self, *, result: bytes | None = None, failure: OSError | None = None) -> None:
        self.calls: list[AuthorizedPayload] = []
        self.result = result
        self.failure = failure

    def __call__(self, descriptor: AuthorizedPayload) -> bytes:
        self.calls.append(descriptor)
        if self.failure is not None:
            raise self.failure
        return descriptor.artifact.source_id.encode() if self.result is None else self.result


def fixture(
    *records: SecFiling, moves: tuple[Move, ...] = (), looks: tuple[Look, ...] = ()
) -> Fixture:
    index = SecIndex(
        archive_id="synthetic-archive",
        source_sha256=HASH,
        config_sha256=HASH,
        filings=records,
    )
    registry = PoolRegistry()
    for record in records:
        registry.register(record.source_record.issuer, record.source_record.pool)
    manifest = SourceManifest(
        state="complete",
        writer_version="synthetic-v1",
        source_sha256=HASH,
        config_sha256=HASH,
        index_sha256=index.content_sha256(),
        registry_sha256=registry_sha256(registry),
        split_records_sha256=split_records_sha256(moves, looks),
        artifacts=tuple(artifact(record) for record in records),
    )
    context = AccessContext(
        source_sha256=HASH,
        config_sha256=HASH,
        index_sha256=index.content_sha256(),
        manifest_sha256=manifest.content_sha256(),
        registry_sha256=registry_sha256(registry),
        split_records_sha256=split_records_sha256(moves, looks),
    )
    return Fixture(index, manifest, registry, context, moves, looks)


def selected(record: SecFiling) -> str:
    return artifact(record).artifact_id


def test_fit_and_validation_bytes_and_metadata_are_deterministic() -> None:
    fit, valid = filing(cik_in(Part.FIT)), filing(cik_in(Part.VALIDATION))
    case = fixture(fit, valid)
    before = case.registry.metadata()
    descriptors = case.authorize((selected(valid), selected(fit)))
    assert {d.part for d in descriptors} == {Part.FIT, Part.VALIDATION}
    assert all(d.context == case.context for d in descriptors)
    assert descriptors == fixture(valid, fit).authorize((selected(fit), selected(valid)))
    spy = Spy()
    result = read_payloads(
        (selected(valid), selected(fit)),
        index=case.index,
        manifest=case.manifest,
        registry=case.registry,
        context=case.context,
        moves=(),
        looks=(),
        opener=spy,
    )
    assert tuple(v.descriptor for v in result) == descriptors
    assert all(v.payload == v.descriptor.artifact.source_id.encode() for v in result)
    assert case.registry.metadata() == before


@pytest.mark.parametrize("part", list(Part))
def test_aliases_years_and_filing_ids_share_the_cik_split(part: Part) -> None:
    first = filing(cik_in(part), name="Earlier Brand")
    second = filing(cik_in(part), name="Reviewed Rename", suffix="-second-year")
    second = second.model_copy(
        update={
            "source_record": second.source_record.model_copy(
                update={"period_end": date(2024, 12, 31)}
            )
        }
    )
    case = fixture(first, second)
    spy = Spy()
    if part == Part.HOLDOUT:
        with pytest.raises(SourceAccessError, match="holdout"):
            case.read((selected(first), selected(second)), spy)
        assert spy.calls == []
    else:
        assert {d.part for d in case.authorize((selected(first), selected(second)))} == {part}


@pytest.mark.parametrize("pool", [Pool.DEV, Pool.MODEL_TEST, Pool.BLIND])
def test_nontrain_selection_never_opens_any_content(pool: Pool) -> None:
    fit = filing(cik_in(Part.FIT))
    forbidden = filing(cik_in(Part.VALIDATION))
    forbidden = forbidden.model_copy(
        update={"source_record": forbidden.source_record.model_copy(update={"pool": pool})}
    )
    case = fixture(fit, forbidden)
    spy = Spy()
    with pytest.raises(SourceAccessError, match="not eligible"):
        case.read((selected(fit), selected(forbidden)), spy)
    assert spy.calls == []
    # Metadata from another pool can be validated without opening its payload.
    case.read((selected(fit),), spy)
    assert len(spy.calls) == 1


def test_holdout_metadata_is_allowed_but_mixed_selection_is_rejected_before_open() -> None:
    fit, holdout = filing(cik_in(Part.FIT)), filing(cik_in(Part.HOLDOUT))
    case = fixture(fit, holdout)
    spy = Spy()
    with pytest.raises(SourceAccessError, match="holdout"):
        case.read((selected(fit), selected(holdout)), spy)
    assert spy.calls == []


def test_whole_roster_is_checked_even_for_empty_selection() -> None:
    fit, unrecorded = filing(cik_in(Part.FIT)), filing(cik_in(Part.HOLDOUT))
    case = fixture(fit, unrecorded)
    case.registry = fixture(fit).registry
    # Bind the changed registry so the failure exercises identity authorization itself.
    registry_hash = registry_sha256(case.registry)
    case.manifest = case.manifest.model_copy(update={"registry_sha256": registry_hash})
    case.context = case.context.model_copy(
        update={"registry_sha256": registry_hash, "manifest_sha256": case.manifest.content_sha256()}
    )
    spy = Spy()
    with pytest.raises(SourceAccessError, match="not fully recorded"):
        case.read((), spy)
    assert spy.calls == []


def test_forged_pool_cannot_override_reviewed_registry() -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    registry = PoolRegistry()
    registry.register(fit.source_record.issuer, Pool.MODEL_TEST)
    case.registry = registry
    registry_hash = registry_sha256(registry)
    case.manifest = case.manifest.model_copy(update={"registry_sha256": registry_hash})
    case.context = case.context.model_copy(
        update={"registry_sha256": registry_hash, "manifest_sha256": case.manifest.content_sha256()}
    )
    spy = Spy()
    with pytest.raises(ValueError, match="spans pools"):
        case.read((selected(fit),), spy)
    assert spy.calls == []


def moved_case(*, spent: bool = False) -> Fixture:
    record = filing(cik_in(Part.HOLDOUT), name="Moved Filer")
    look = Look("2026-10-01", "dry_run", ("holdout",), ("sec",), "synthetic")
    move = Move(
        "Moved Filer",
        issuer_key("Moved Filer"),
        "spent_look" if spent else "override",
        "holdout",
        "fit",
        "2026-10-02",
        1 if spent else 0,
        "Reviewed synthetic move",
    )
    return fixture(record, moves=(move,), looks=(look,) if spent else ())


@pytest.mark.parametrize("spent", [False, True])
def test_recorded_moves_use_the_complete_roster_and_do_not_write(spent: bool) -> None:
    case = moved_case(spent=spent)
    before = (case.registry.metadata(), case.moves, case.looks)
    assert case.authorize((case.manifest.artifacts[0].artifact_id,))[0].part == Part.FIT
    assert (case.registry.metadata(), case.moves, case.looks) == before


def test_missing_move_target_is_rejected_before_open() -> None:
    case = moved_case()
    other = filing(cik_in(Part.FIT))
    case = fixture(other, moves=case.moves)
    spy = Spy()
    with pytest.raises(SplitError, match="no train document"):
        case.read((selected(other),), spy)
    assert spy.calls == []


def test_missing_look_chronology_is_rejected_before_open() -> None:
    case = moved_case(spent=True)
    case.looks = ()
    spy = Spy()
    with pytest.raises(SplitError, match="log holds 0"):
        case.read((case.manifest.artifacts[0].artifact_id,), spy)
    assert spy.calls == []


@pytest.mark.parametrize(
    "field",
    [
        "source_sha256",
        "config_sha256",
        "index_sha256",
        "registry_sha256",
        "split_records_sha256",
        "manifest_sha256",
    ],
)
def test_stale_trusted_context_never_opens_content(field: str) -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    case.context = case.context.model_copy(update={field: OTHER_HASH})
    spy = Spy()
    with pytest.raises(SourceAccessError, match=field):
        case.read((selected(fit),), spy)
    assert spy.calls == []


@pytest.mark.parametrize("state", ["staging", "incomplete", "reserved"])
def test_incomplete_manifest_cannot_claim_a_commit_marker(state: str) -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    case.manifest = case.manifest.model_copy(update={"state": state})
    spy = Spy()
    with pytest.raises(ValidationError, match="state"):
        case.read((selected(fit),), spy)
    assert spy.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/absolute",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "C:/x",
        "a/%2e%2e/b",
        "a\x00b",
        ".staging/a",
        "https://host/x",
    ],
)
def test_ambiguous_or_escaping_paths_are_rejected(path: str) -> None:
    case = fixture(filing(cik_in(Part.FIT)))
    bad = case.manifest.artifacts[0].model_copy(update={"relative_path": path})
    case.manifest = case.manifest.model_copy(update={"artifacts": (bad,)})
    spy = Spy()
    with pytest.raises(ValidationError, match="relative_path"):
        case.read((bad.artifact_id,), spy)
    assert spy.calls == []


def test_duplicate_filing_ids_and_raw_locators_fail_before_selection() -> None:
    fit = filing(cik_in(Part.FIT))
    for other in (
        fit,
        fit.model_copy(
            update={
                "source_record": fit.source_record.model_copy(update={"source_id": "different"})
            }
        ),
    ):
        case = fixture(fit, other)
        spy = Spy()
        with pytest.raises(SourceAccessError, match="duplicate"):
            case.read((), spy)
        assert spy.calls == []


@pytest.mark.parametrize("field", ["artifact_id", "source_id", "relative_path"])
def test_conflicting_artifact_metadata_fails_even_if_not_selected(field: str) -> None:
    fit, valid = filing(cik_in(Part.FIT)), filing(cik_in(Part.VALIDATION))
    case = fixture(fit, valid)
    first, second = case.manifest.artifacts
    second = second.model_copy(update={field: getattr(first, field)})
    case.manifest = case.manifest.model_copy(update={"artifacts": (first, second)})
    case.context = case.context.model_copy(
        update={"manifest_sha256": case.manifest.content_sha256()}
    )
    spy = Spy()
    with pytest.raises(SourceAccessError, match="duplicate"):
        case.read((), spy)
    assert spy.calls == []


@pytest.mark.parametrize("field", ["source_sha256", "config_sha256"])
def test_inconsistent_source_record_hashes_fail_before_open(field: str) -> None:
    fit = filing(cik_in(Part.FIT))
    bad = fit.model_copy(
        update={"source_record": fit.source_record.model_copy(update={field: OTHER_HASH})}
    )
    case = fixture(bad)
    spy = Spy()
    with pytest.raises(SourceAccessError, match=field):
        case.read((selected(bad),), spy)
    assert spy.calls == []


def test_unknown_or_duplicate_selection_is_rejected_before_any_open() -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    for ids in ((selected(fit), "unknown"), (selected(fit), selected(fit))):
        spy = Spy()
        with pytest.raises(SourceAccessError):
            case.read(ids, spy)
        assert spy.calls == []


@pytest.mark.parametrize("payload", [b"", b"changed bytes"])
def test_missing_or_changed_payload_bytes_cannot_be_returned_as_verified(payload: bytes) -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    spy = Spy(result=payload)
    with pytest.raises(SourceAccessError, match="payload_sha256"):
        case.read((selected(fit),), spy)
    assert len(spy.calls) == 1


def test_opener_failure_never_returns_a_partial_dataset() -> None:
    fit, valid = filing(cik_in(Part.FIT)), filing(cik_in(Part.VALIDATION))
    case = fixture(fit, valid)
    calls: list[AuthorizedPayload] = []

    def fail_second(descriptor: AuthorizedPayload) -> bytes:
        calls.append(descriptor)
        if len(calls) == 2:
            raise FileNotFoundError("synthetic missing payload")
        return descriptor.artifact.source_id.encode()

    with pytest.raises(SourceAccessError, match="cannot retrieve") as error:
        read_payloads(
            (selected(fit), selected(valid)),
            index=case.index,
            manifest=case.manifest,
            registry=case.registry,
            context=case.context,
            moves=(),
            looks=(),
            opener=fail_second,
        )
    assert isinstance(error.value.__cause__, FileNotFoundError)
    assert len(calls) == 2


def test_name_only_and_unknown_pool_contracts_are_refused() -> None:
    fit = filing(cik_in(Part.FIT))
    for change in ({"issuer": Identity("Name Only")}, {"pool": "unknown"}):
        bad = fit.model_copy(update={"source_record": fit.source_record.model_copy(update=change)})
        case = fixture(fit)
        case.index = case.index.model_copy(update={"filings": (bad,)})
        spy = Spy()
        with pytest.raises(ValueError):
            case.read((selected(fit),), spy)
        assert spy.calls == []


def test_contracts_are_strict_frozen_and_forbid_extra_fields() -> None:
    case = fixture(filing(cik_in(Part.FIT)))
    with pytest.raises(ValidationError, match="frozen"):
        case.index.archive_id = "changed"
    with pytest.raises(ValidationError, match="extra"):
        PayloadArtifact.model_validate(
            {**case.manifest.artifacts[0].model_dump(), "pool": Pool.TRAIN}
        )
    with pytest.raises(ValidationError, match="row_count"):
        PayloadArtifact.model_validate(
            {**case.manifest.artifacts[0].model_dump(), "row_count": "1"}
        )
    changed_context = case.context.model_copy(update={"config_sha256": OTHER_HASH})
    assert replace(case, context=changed_context).context != case.context


@pytest.mark.parametrize("change", ["missing", "unexpected"])
def test_manifest_must_cover_exactly_the_full_filing_roster(change: str) -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    artifacts = (
        ()
        if change == "missing"
        else (case.manifest.artifacts[0].model_copy(update={"source_id": "unknown-filing"}),)
    )
    case.manifest = case.manifest.model_copy(update={"artifacts": artifacts})
    case.context = case.context.model_copy(
        update={"manifest_sha256": case.manifest.content_sha256()}
    )
    spy = Spy()
    with pytest.raises(SourceAccessError, match="missing"):
        case.read((), spy)
    assert spy.calls == []


def test_a_new_alias_is_not_inferred_from_an_existing_cik() -> None:
    fit = filing(cik_in(Part.FIT), name="Recorded Brand")
    case = fixture(fit)
    renamed = filing(cik_in(Part.FIT), name="Unrecorded Rename")
    case.index = case.index.model_copy(update={"filings": (renamed,)})
    index_hash = case.index.content_sha256()
    case.manifest = case.manifest.model_copy(update={"index_sha256": index_hash})
    case.context = case.context.model_copy(
        update={"index_sha256": index_hash, "manifest_sha256": case.manifest.content_sha256()}
    )
    before = case.registry.metadata()
    spy = Spy()
    with pytest.raises(SourceAccessError, match="not fully recorded"):
        case.read((selected(fit),), spy)
    assert spy.calls == []
    assert case.registry.metadata() == before


@pytest.mark.parametrize("subject", ["index", "manifest", "split", "registry"])
def test_changed_content_is_detected_without_updating_trusted_hashes(subject: str) -> None:
    case = moved_case()
    if subject == "index":
        case.index = case.index.model_copy(update={"archive_id": "changed-archive"})
    elif subject == "manifest":
        changed = case.manifest.artifacts[0].model_copy(update={"payload_sha256": OTHER_HASH})
        case.manifest = case.manifest.model_copy(update={"artifacts": (changed,)})
    elif subject == "split":
        case.moves = (replace(case.moves[0], reason="Changed reviewed reason"),)
    else:
        case.registry.register(Identity("Another Recorded Issuer", 999), Pool.BLIND)
    spy = Spy()
    with pytest.raises(SourceAccessError, match="sha256"):
        case.read((case.manifest.artifacts[0].artifact_id,), spy)
    assert spy.calls == []


@pytest.mark.parametrize(
    "field",
    ["source_sha256", "config_sha256", "index_sha256", "registry_sha256", "split_records_sha256"],
)
def test_stale_manifest_fields_fail_even_if_its_digest_is_trusted(field: str) -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    case.manifest = case.manifest.model_copy(update={field: OTHER_HASH})
    case.context = case.context.model_copy(
        update={"manifest_sha256": case.manifest.content_sha256()}
    )
    spy = Spy()
    with pytest.raises(SourceAccessError, match=field):
        case.read((selected(fit),), spy)
    assert spy.calls == []


@pytest.mark.parametrize("pool", ["unknown", "fit", "holdout", "train"])
def test_unchecked_row_pool_values_cannot_enter_the_boundary(pool: str) -> None:
    fit = filing(cik_in(Part.FIT))
    bad_source = fit.source_record.model_copy(update={"pool": pool})
    bad = fit.model_copy(update={"source_record": bad_source})
    case = fixture(fit)
    case.index = case.index.model_copy(update={"filings": (bad,)})
    spy = Spy()
    with pytest.raises(ValidationError, match="pool"):
        case.read((selected(fit),), spy)
    assert spy.calls == []


def test_unchecked_identity_and_nonsec_records_are_revalidated() -> None:
    fit = filing(cik_in(Part.FIT))
    raw = object.__new__(Identity)
    object.__setattr__(raw, "name", "UNNORMALIZED CO.")
    object.__setattr__(raw, "cik", cik_in(Part.FIT))
    for changes in ({"issuer": raw}, {"source": Source.PDF}):
        case = fixture(fit)
        bad = fit.model_copy(update={"source_record": fit.source_record.model_copy(update=changes)})
        case.index = case.index.model_copy(update={"filings": (bad,)})
        spy = Spy()
        with pytest.raises(ValueError):
            case.read((selected(fit),), spy)
        assert spy.calls == []


@pytest.mark.parametrize("sha", ["", "A" * 64, "a" * 63, "z" * 64])
def test_invalid_payload_hashes_are_revalidated_before_open(sha: str) -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    bad = case.manifest.artifacts[0].model_copy(update={"payload_sha256": sha})
    case.manifest = case.manifest.model_copy(update={"artifacts": (bad,)})
    spy = Spy()
    with pytest.raises(ValidationError, match="payload_sha256"):
        case.read((selected(fit),), spy)
    assert spy.calls == []


def test_unknown_selection_is_never_hidden_by_a_duplicate_eligible_payload() -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    spy = Spy()
    with pytest.raises(SourceAccessError):
        case.read((selected(fit), "unknown", selected(fit)), spy)
    assert spy.calls == []


def test_opener_oserror_names_the_artifact_and_keeps_its_cause() -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    spy = Spy(failure=PermissionError("synthetic unavailable artifact"))
    with pytest.raises(SourceAccessError, match=selected(fit)) as error:
        case.read((selected(fit),), spy)
    assert isinstance(error.value.__cause__, PermissionError)


def test_declared_hashes_are_not_evidence_of_annotation_truth() -> None:
    fit = filing(cik_in(Part.FIT))
    case = fixture(fit)
    # No decoder: even a declared count is metadata, never measured label content.
    declared = case.manifest.artifacts[0].model_copy(update={"row_count": 100})
    case.manifest = case.manifest.model_copy(update={"artifacts": (declared,)})
    case.context = case.context.model_copy(
        update={"manifest_sha256": case.manifest.content_sha256()}
    )
    assert case.authorize((selected(fit),))[0].artifact.row_count == 100


@pytest.mark.parametrize("version", [True, "1", 1.0, 2])
def test_schema_versions_require_the_exact_integer(version: object) -> None:
    case = fixture(filing(cik_in(Part.FIT)))
    for contract in (case.index, case.manifest):
        with pytest.raises(ValidationError, match="version"):
            type(contract).model_validate(contract.model_copy(update={"version": version}))
