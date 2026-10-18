"""Review packet, owner sample and approval records for a draft crosswalk."""

import hashlib
import json

import pytest
from dataset_contracts import DatasetError
from sec_crosswalk import (
    ApprovalRecord,
    MappedResult,
    ReviewEvidence,
    RoleAssessment,
    TagKey,
    TrustContext,
    resolve_tag,
)
from sec_crosswalk_draft import DRAFT_PRODUCER_ID, build_draft, build_policies
from sec_crosswalk_fixtures import committed_mapping, synthetic_vocabulary, taxonomy
from sec_crosswalk_review import (
    approval_records,
    entry_key,
    exclusion_key,
    owner_sample,
    policy_key,
    review_packet,
    subjects_header,
)

from fra_core.schemas.statement import StatementType
from fra_core.taxonomy.loader import PLAN_CRITICAL_IDS

REVIEWER = "reviewer-agent-1"
AUTHORITY = "owner-sample-check"


def draft_package():  # type: ignore[no-untyped-def]
    mapping, tax = committed_mapping(), taxonomy()
    vocabulary = synthetic_vocabulary(mapping)
    return (
        build_draft(vocabulary, mapping, tax),
        build_policies(vocabulary, mapping, tax),
        vocabulary,
        mapping,
        tax,
    )


def verdict_lines(  # type: ignore[no-untyped-def]
    crosswalk, policies, *, skip=(), reject=(), extra=(), header="default"
):
    keys = (
        [entry_key(e.key) for e in crosswalk.entries]
        + [policy_key(p) for p in policies]
        + [exclusion_key(x) for x in committed_mapping().excluded]
    )
    rows = [
        {"key": k, "verdict": "reject" if k in reject else "approve", "note": "ok"}
        for k in keys
        if k not in skip
    ] + list(extra)
    if header == "default":
        header = subjects_header(crosswalk, policies, committed_mapping())
    lines = ([header] if header is not None else []) + rows
    return ("\n".join(json.dumps(r) for r in lines) + "\n").encode("utf-8")


def approve(crosswalk, policies, data, **changes):  # type: ignore[no-untyped-def]
    arguments = {
        "reviewer_id": REVIEWER,
        "authority_id": AUTHORITY,
        "evidence_reference": "verdicts.jsonl",
        **changes,
    }
    return approval_records(crosswalk, policies, committed_mapping(), data, **arguments)


def test_packet_has_one_row_per_entry_and_policy_with_target_semantics() -> None:
    crosswalk, policies, vocabulary, mapping, tax = draft_package()
    packet = review_packet(crosswalk, policies, vocabulary, mapping, tax)
    entries = [row for row in packet if row["kind"] == "entry"]
    assert len(entries) == len(crosswalk.entries)
    assert len([row for row in packet if row["kind"] == "policy"]) == len(policies)
    excluded = [row for row in packet if row["kind"] == "excluded"]
    assert {row["key"] for row in excluded} == {
        "excluded:IS:NetIncomeLoss",
        "excluded:BS:StockholdersEquity",
    }
    shown = next(r for r in excluded if r["tag"] == "NetIncomeLoss")
    assert shown["reason"]
    assert shown["definitions"]["us-gaap/2024"]["doc"] == "NetIncomeLoss excluded definition"
    assert len({row["key"] for row in packet[1:]}) == len(packet) - 1
    assets = next(r for r in entries if r["tag"] == "Assets" and r["version"] == "us-gaap/2024")
    assert assets["key"] == "us-gaap|2024|balance|Assets"
    assert assets["tlabel"] == "Assets label"
    assert assets["doc"].endswith("(synthetic)")
    assert assets["canonical_id"] == "total_assets"
    target = assets["target"]
    assert target["statement"] == "balance"
    assert target["natural_sign"] == "+"
    assert "total assets" in target["aliases"]
    assert assets["reason"]
    null = next(r for r in entries if r["canonical_id"] is None)
    assert null["target"] is None
    assert null["reason"]


def test_packet_starts_with_the_subjects_header() -> None:
    crosswalk, policies, vocabulary, mapping, tax = draft_package()
    packet = review_packet(crosswalk, policies, vocabulary, mapping, tax)
    assert packet[0] == {
        "kind": "subjects",
        "mapping_sha256": mapping.sha256,
        "crosswalk_sha256": crosswalk.content_sha256(),
        "policies": {p.policy_id: p.content_sha256() for p in policies},
    }
    assert packet[0] == subjects_header(crosswalk, policies, mapping)
    assert all(row["kind"] != "subjects" for row in packet[1:])


def test_packet_is_deterministic_and_json_serialisable() -> None:
    crosswalk, policies, vocabulary, mapping, tax = draft_package()
    first = review_packet(crosswalk, policies, vocabulary, mapping, tax)
    again = review_packet(crosswalk, policies, vocabulary, mapping, tax)
    assert first == again
    json.dumps(first, ensure_ascii=False)


def test_sample_includes_every_critical_target_and_some_nulls_deterministically() -> None:
    crosswalk, policies, vocabulary, mapping, tax = draft_package()
    packet = review_packet(crosswalk, policies, vocabulary, mapping, tax)
    entries = [r for r in packet if r["kind"] == "entry"]
    critical = {r["key"] for r in entries if r["canonical_id"] in PLAN_CRITICAL_IDS}
    assert critical
    sample = owner_sample(packet, len(critical) + 10, seed=7)
    assert sample[0] == packet[0]
    keys = [r["key"] for r in sample[1:]]
    assert critical <= set(keys)
    assert any(r["kind"] == "entry" and r["canonical_id"] is None for r in sample)
    assert {r["key"] for r in packet if r["kind"] in ("policy", "excluded")} <= set(keys)
    assert len(keys) == len(set(keys))
    assert sample == owner_sample(packet, len(critical) + 10, seed=7)
    other = owner_sample(packet, len(critical) + 10, seed=8)
    assert keys != [r["key"] for r in other[1:]]


def test_sample_never_drops_critical_rows_when_n_is_small() -> None:
    crosswalk, policies, vocabulary, mapping, tax = draft_package()
    packet = review_packet(crosswalk, policies, vocabulary, mapping, tax)
    sample = owner_sample(packet, 1, seed=1)
    critical = {
        r["key"] for r in packet if r["kind"] == "entry" and r["canonical_id"] in PLAN_CRITICAL_IDS
    }
    assert critical <= {r["key"] for r in sample[1:]}
    with pytest.raises(DatasetError, match="n"):
        owner_sample(packet, 0, seed=1)


def test_approvals_need_an_explicit_approve_for_every_entry_and_policy() -> None:
    crosswalk, policies, *_ = draft_package()
    first = entry_key(crosswalk.entries[0].key)
    cases = {
        "missing entry": verdict_lines(crosswalk, policies, skip=(first,)),
        "missing policy": verdict_lines(crosswalk, policies, skip=(policy_key(policies[0]),)),
        "missing exclusion": verdict_lines(
            crosswalk, policies, skip=("excluded:IS:NetIncomeLoss",)
        ),
        "rejected": verdict_lines(crosswalk, policies, reject=(first,)),
        "unknown key": verdict_lines(
            crosswalk, policies, extra=[{"key": "nope", "verdict": "approve", "note": ""}]
        ),
        "duplicate": verdict_lines(
            crosswalk, policies, extra=[{"key": first, "verdict": "approve", "note": ""}]
        ),
        "bad verdict": verdict_lines(
            crosswalk, policies, extra=[{"key": first, "verdict": "maybe", "note": ""}]
        ),
        "not json": b"{oops\n",
        "empty": b"",
    }
    for name, data in cases.items():
        with pytest.raises(DatasetError):
            approval_records(
                crosswalk,
                policies,
                committed_mapping(),
                data,
                reviewer_id=REVIEWER,
                authority_id=AUTHORITY,
                evidence_reference="verdicts.jsonl",
            )
        assert name


def test_rejection_error_names_the_entry() -> None:
    crosswalk, policies, *_ = draft_package()
    first = entry_key(crosswalk.entries[0].key)
    with pytest.raises(DatasetError, match=first.replace("|", r"\|")):
        approval_records(
            crosswalk,
            policies,
            committed_mapping(),
            verdict_lines(crosswalk, policies, reject=(first,)),
            reviewer_id=REVIEWER,
            authority_id=AUTHORITY,
            evidence_reference="verdicts.jsonl",
        )


def test_reviewer_must_differ_from_the_producer() -> None:
    crosswalk, policies, *_ = draft_package()
    with pytest.raises(ValueError, match="independent"):
        approval_records(
            crosswalk,
            policies,
            committed_mapping(),
            verdict_lines(crosswalk, policies),
            reviewer_id=crosswalk.review.producer_id,
            authority_id=AUTHORITY,
            evidence_reference="verdicts.jsonl",
        )


def test_approved_package_resolves_a_tag_end_to_end() -> None:
    crosswalk, policies, _, _, tax = draft_package()
    data = verdict_lines(crosswalk, policies)
    package = approval_records(
        crosswalk,
        policies,
        committed_mapping(),
        data,
        reviewer_id=REVIEWER,
        authority_id=AUTHORITY,
        evidence_reference="verdicts.jsonl",
    )
    # The artifacts that were approved are the reviewed copies, never the drafts.
    assert package.crosswalk.review.status == "reviewed"
    assert crosswalk.review.status == "draft"
    assert all(p.review.status == "reviewed" for p in package.policies)
    assert len(package.approvals) == 1 + len(policies)
    assert {a.status for a in package.approvals} == {"approved"}
    assert {a.evidence_sha256 for a in package.approvals} == {hashlib.sha256(data).hexdigest()}
    assert {a.reviewer_id for a in package.approvals} == {REVIEWER}
    assert {a.producer_id for a in package.approvals} == {crosswalk.review.producer_id}
    assert {a.evidence_reference for a in package.approvals} == {"verdicts.jsonl"}

    role = RoleAssessment(
        review=ReviewEvidence(
            producer_id=crosswalk.review.producer_id,
            status="reviewed",
            reference="synthetic",
            sha256="d" * 64,
        ),
        source_id="filing-1",
        role="corporate",
    )
    role_approval = ApprovalRecord(
        subject_kind="role_assessment",
        subject_sha256=role.content_sha256(),
        producer_id=role.review.producer_id,
        reviewer_id=REVIEWER,
        authority_id=AUTHORITY,
        status="approved",
        evidence_reference="synthetic",
        evidence_sha256="d" * 64,
    )
    result = resolve_tag(
        TagKey(
            namespace="us-gaap",
            tag_taxonomy_version="2024",
            statement=StatementType.BALANCE,
            tag="Assets",
        ),
        crosswalk=package.crosswalk,
        taxonomy=tax,
        source_id="filing-1",
        role=role,
        policies=package.policies,
        trust=TrustContext(
            authority_allowlist=frozenset({AUTHORITY}),
            records=(*package.approvals, role_approval),
        ),
    )
    assert isinstance(result, MappedResult)
    assert result.canonical_id == "total_assets"


def test_a_header_missing_or_foreign_is_refused() -> None:
    crosswalk, policies, *_ = draft_package()
    good = subjects_header(crosswalk, policies, committed_mapping())
    for header in (
        None,
        {**good, "mapping_sha256": "0" * 64},
        {**good, "crosswalk_sha256": "0" * 64},
        {**good, "policies": {**good["policies"], "extra": "0" * 64}},
        {**good, "kind": "other"},
    ):
        with pytest.raises(DatasetError, match="header"):
            approve(crosswalk, policies, verdict_lines(crosswalk, policies, header=header))


def test_a_crosswalk_edited_after_the_packet_is_refused_even_with_unchanged_keys() -> None:
    crosswalk, policies, *_ = draft_package()
    data = verdict_lines(crosswalk, policies)
    first = next(e for e in crosswalk.entries if e.canonical_id)
    index = crosswalk.entries.index(first)
    other = "total_liabilities" if first.canonical_id != "total_liabilities" else "total_assets"
    edited = crosswalk.model_copy(
        update={
            "entries": (
                *crosswalk.entries[:index],
                first.model_copy(update={"canonical_id": other}),
                *crosswalk.entries[index + 1 :],
            )
        }
    )
    assert entry_key(edited.entries[index].key) == entry_key(first.key)
    with pytest.raises(DatasetError, match="header"):
        approve(edited, policies, data)


def test_a_policy_edited_after_the_packet_is_refused() -> None:
    crosswalk, policies, *_ = draft_package()
    data = verdict_lines(crosswalk, policies)
    edited = (policies[0].model_copy(update={"reason": "Changed after review."}), *policies[1:])
    with pytest.raises(DatasetError, match="header"):
        approve(crosswalk, edited, data)


def test_only_unreviewed_drafts_from_the_draft_producer_are_approved() -> None:
    crosswalk, policies, *_ = draft_package()
    reviewed = crosswalk.model_copy(
        update={"review": crosswalk.review.model_copy(update={"status": "reviewed"})}
    )
    with pytest.raises(DatasetError, match="draft"):
        approve(reviewed, policies, verdict_lines(reviewed, policies))
    foreign = crosswalk.model_copy(
        update={"review": crosswalk.review.model_copy(update={"producer_id": "someone-else"})}
    )
    with pytest.raises(DatasetError, match=DRAFT_PRODUCER_ID):
        approve(foreign, policies, verdict_lines(foreign, policies))
    stale = policies[0].model_copy(
        update={"review": policies[0].review.model_copy(update={"status": "reviewed"})}
    )
    with pytest.raises(DatasetError, match="draft"):
        approve(crosswalk, (stale, *policies[1:]), verdict_lines(crosswalk, (stale, *policies[1:])))


def test_every_artifact_must_be_drafted_from_the_mapping_given() -> None:
    crosswalk, policies, *_ = draft_package()
    other = policies[0].review.model_copy(update={"sha256": "e" * 64})
    drifted = (policies[0].model_copy(update={"review": other}), *policies[1:])
    with pytest.raises(DatasetError, match="mapping"):
        approve(crosswalk, drifted, verdict_lines(crosswalk, drifted))
    wrong = crosswalk.model_copy(
        update={"review": crosswalk.review.model_copy(update={"sha256": "e" * 64})}
    )
    with pytest.raises(DatasetError, match="mapping"):
        approve(wrong, policies, verdict_lines(wrong, policies))


def test_duplicate_policies_entries_and_exclusions_are_refused() -> None:
    crosswalk, policies, *_ = draft_package()
    data = verdict_lines(crosswalk, policies)
    altered = policies[0].model_copy(update={"reason": "Altered duplicate."})
    with pytest.raises(DatasetError, match="policy_id"):
        approve(crosswalk, (*policies, altered), data)
    with pytest.raises(DatasetError, match="policy_id"):
        approve(crosswalk, (*policies, policies[0]), data)
    doubled = crosswalk.model_copy(update={"entries": (*crosswalk.entries, crosswalk.entries[0])})
    with pytest.raises(DatasetError, match="entry"):
        approve(doubled, policies, data)
    mapping = committed_mapping()
    twice = mapping.model_copy(update={"excluded": (*mapping.excluded, mapping.excluded[0])})
    with pytest.raises(DatasetError, match="excluded"):
        approval_records(
            crosswalk,
            policies,
            twice,
            data,
            reviewer_id=REVIEWER,
            authority_id=AUTHORITY,
            evidence_reference="verdicts.jsonl",
        )
