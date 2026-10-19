"""Synthetic crosswalk authorization; no source labels or corpus records."""

import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from sec_crosswalk import (
    AbstentionPolicy,
    ApprovalRecord,
    Crosswalk,
    CrosswalkEntry,
    CrosswalkError,
    ReviewEvidence,
    RoleAssessment,
    TagKey,
    TagScope,
    TrustContext,
    _digest,
    resolve_tag,
    taxonomy_sha256,
)

from fra_core.schemas.statement import StatementType
from fra_core.taxonomy import load_taxonomy

HASH = "a" * 64
TAXONOMY = load_taxonomy()


def review(**changes: object) -> ReviewEvidence:
    return ReviewEvidence.model_validate(
        {
            "producer_id": "synthetic-producer",
            "status": "reviewed",
            "reference": "synthetic-evidence",
            "sha256": HASH,
            **changes,
        }
    )


def key(**changes: object) -> TagKey:
    return TagKey.model_validate(
        {
            "namespace": "synthetic-namespace",
            "tag_taxonomy_version": "synthetic-v1",
            "statement": StatementType.INCOME,
            "tag": "SyntheticSales",
            **changes,
        }
    )


def entry(**changes: object) -> CrosswalkEntry:
    return CrosswalkEntry.model_validate(
        {
            "key": key(),
            "canonical_id": "revenue",
            "abstention_reason": None,
            "semantic_reference": "synthetic-semantic-review",
            "semantic_evidence_sha256": HASH,
            **changes,
        }
    )


def crosswalk(*entries: CrosswalkEntry, **changes: object) -> Crosswalk:
    return Crosswalk.model_validate(
        {
            "schema_version": 1,
            "canonical_taxonomy_version": TAXONOMY.version,
            "canonical_taxonomy_sha256": taxonomy_sha256(TAXONOMY),
            "entries": entries or (entry(),),
            "review": review(),
            **changes,
        }
    )


def role(**changes: object) -> RoleAssessment:
    return RoleAssessment.model_validate(
        {
            "source_id": "synthetic-filing",
            "role": "corporate",
            "review": review(),
            **changes,
        }
    )


def policy(kind: str = "outside_crosswalk", **changes: object) -> AbstentionPolicy:
    return AbstentionPolicy.model_validate(
        {
            "policy_id": f"synthetic-{kind}",
            "kind": kind,
            "canonical_taxonomy_version": TAXONOMY.version,
            "canonical_taxonomy_sha256": taxonomy_sha256(TAXONOMY),
            "scopes": (
                TagScope(
                    namespace=key().namespace,
                    tag_taxonomy_version=key().tag_taxonomy_version,
                    statement=key().statement,
                ),
            ),
            "reason": "synthetic reviewed exclusion",
            "review": review(),
            **changes,
        }
    )


def approval(
    subject: Crosswalk | AbstentionPolicy | RoleAssessment, **changes: object
) -> ApprovalRecord:
    if isinstance(subject, Crosswalk):
        kind = "crosswalk"
    elif isinstance(subject, AbstentionPolicy):
        kind = "abstention_policy"
    else:
        kind = "role_assessment"
    return ApprovalRecord.model_validate(
        {
            "subject_kind": kind,
            "subject_sha256": subject.content_sha256(),
            "producer_id": subject.review.producer_id,
            "reviewer_id": "synthetic-independent-reviewer",
            "authority_id": "synthetic-authority",
            "status": "approved",
            "evidence_reference": "synthetic-approval-evidence",
            "evidence_sha256": HASH,
            **changes,
        }
    )


def trust(*records: ApprovalRecord) -> TrustContext:
    return TrustContext(authority_allowlist=frozenset({"synthetic-authority"}), records=records)


def resolve(
    artifact: Crosswalk | None = None,
    *,
    query: TagKey | None = None,
    assessment: RoleAssessment | None = None,
    policies: tuple[AbstentionPolicy, ...] = (),
    context: TrustContext | None = None,
    companion_present: bool = False,
):
    artifact = artifact if artifact is not None else crosswalk()
    assessment = assessment if assessment is not None else role()
    context = (
        context
        if context is not None
        else trust(approval(artifact), approval(assessment), *(approval(p) for p in policies))
    )
    return resolve_tag(
        query if query is not None else key(),
        crosswalk=artifact,
        taxonomy=TAXONOMY,
        source_id="synthetic-filing",
        role=assessment,
        policies=policies,
        trust=context,
        companion_present=companion_present,
    )


def test_exact_mapping_carries_approval_and_semantic_evidence() -> None:
    artifact = crosswalk()
    result = resolve(artifact)
    assert result.state == "mapped"
    assert result.canonical_id == "revenue"
    assert result.key == key()
    assert result.semantic_evidence_sha256 == HASH
    assert result.binding.crosswalk_sha256 == artifact.content_sha256()
    assert result.binding.crosswalk_approval == approval(artifact)
    assert result.binding.canonical_taxonomy_sha256 == taxonomy_sha256(TAXONOMY)
    assert result.binding.role_sha256 == role().content_sha256()
    assert result == resolve(artifact)


def test_companion_requirement_is_hash_bound_and_fails_closed() -> None:
    conditional = entry(requires_companion="ProfitLoss")
    artifact = crosswalk(conditional)
    assert artifact.content_sha256() != crosswalk(entry()).content_sha256()
    result = resolve(artifact)
    assert result.state == "unresolved" and result.reason == "companion_required"
    assert resolve(artifact, companion_present=True).state == "mapped"


def test_entries_without_a_companion_requirement_keep_their_old_hash() -> None:
    artifact = crosswalk(entry())
    old = artifact.model_dump(mode="json")
    old["entries"] = sorted(
        ({k: v for k, v in e.items() if k != "requires_companion"} for e in old["entries"]),
        key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False),
    )
    assert artifact.content_sha256() == _digest(old)


def test_companion_requirement_is_invalid_for_null_target() -> None:
    with pytest.raises(ValidationError, match="positive target"):
        entry(canonical_id=None, abstention_reason="unmapped", requires_companion="ProfitLoss")


@pytest.mark.parametrize(
    "changes",
    [
        {"namespace": "other-namespace"},
        {"tag_taxonomy_version": "other-version"},
        {"statement": StatementType.BALANCE},
        {"tag": "OtherSyntheticTag"},
    ],
)
def test_lookup_never_broadens_key(changes: dict[str, object]) -> None:
    result = resolve(query=key(**changes))
    assert result.state == "unresolved"
    assert result.reason == "missing_entry"
    assert not hasattr(result, "canonical_id")


def test_same_tag_has_independent_statement_and_version_entries() -> None:
    balance_key = key(statement=StatementType.BALANCE)
    later_key = key(tag_taxonomy_version="synthetic-v2")
    artifact = crosswalk(
        entry(),
        entry(key=balance_key, canonical_id="total_assets"),
        entry(key=later_key, canonical_id=None, abstention_reason="explicit synthetic null"),
    )
    assert resolve(artifact, query=balance_key).canonical_id == "total_assets"
    result = resolve(artifact, query=later_key)
    assert result.state == "abstain"
    assert result.reason == "explicit synthetic null"
    assert result.basis == "entry"
    assert result.policy is None


@pytest.mark.parametrize("target", ["does-not-exist", "total_assets"])
def test_every_original_target_is_validated_before_lookup(target: str) -> None:
    bad = entry(key=key(tag="UnusedSyntheticTag"), canonical_id=target)
    artifact = crosswalk(entry(), bad)
    with pytest.raises(CrosswalkError, match="canonical target"):
        resolve(artifact)


def test_conflicting_duplicate_fails_and_identical_occurrences_are_preserved() -> None:
    with pytest.raises(ValidationError, match="conflicting duplicate"):
        crosswalk(entry(), entry(canonical_id="net_income"))
    single = crosswalk(entry())
    repeated = crosswalk(entry(), entry())
    assert single.content_sha256() != repeated.content_sha256()
    assert resolve(repeated).canonical_id == "revenue"


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_taxonomy_version": 2},
        {"canonical_taxonomy_sha256": "b" * 64},
    ],
)
def test_stale_canonical_identity_fails(changes: dict[str, object]) -> None:
    with pytest.raises(CrosswalkError, match="canonical taxonomy"):
        resolve(crosswalk(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"natural_sign": "-"},
        {"critical": False},
        {"subtotal": True},
        {"per_share": True},
        {"aliases": {"en": ("Changed synthetic alias",)}},
        {"headings": {"en": ("Changed synthetic heading",)}},
        {"statement": StatementType.BALANCE},
    ],
)
def test_taxonomy_fingerprint_covers_semantics(changes: dict[str, object]) -> None:
    original = TAXONOMY.items[0]
    changed = replace(original, **changes)
    taxonomy = replace(TAXONOMY, items=(changed, *TAXONOMY.items[1:]))
    assert taxonomy_sha256(taxonomy) != taxonomy_sha256(TAXONOMY)


def test_order_replay_is_deterministic_without_collapsing_occurrences() -> None:
    one = entry()
    two = entry(key=key(tag="OtherSyntheticTag"), canonical_id="net_income")
    assert crosswalk(one, two).content_sha256() == crosswalk(two, one).content_sha256()
    reversed_taxonomy = replace(TAXONOMY, items=tuple(reversed(TAXONOMY.items)))
    assert taxonomy_sha256(TAXONOMY) == taxonomy_sha256(reversed_taxonomy)


def test_artifact_cannot_approve_itself() -> None:
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(context=trust(approval(role())))
    with pytest.raises(ValidationError, match="approved"):
        crosswalk(approved=True)
    with pytest.raises(ValidationError, match="approved"):
        crosswalk(review=review(status="approved"))


@pytest.mark.parametrize("status", ["draft", "rejected", "revoked"])
def test_inactive_approval_is_refused(status: str) -> None:
    artifact = crosswalk()
    with pytest.raises(CrosswalkError, match="active approval"):
        resolve(artifact, context=trust(approval(artifact, status=status), approval(role())))


def test_authority_producer_and_independence_are_checked() -> None:
    artifact = crosswalk()
    with pytest.raises(CrosswalkError, match="authority"):
        resolve(
            artifact,
            context=trust(approval(artifact, authority_id="untrusted"), approval(role())),
        )
    with pytest.raises(CrosswalkError, match="producer"):
        resolve(
            artifact,
            context=trust(approval(artifact, producer_id="someone-else"), approval(role())),
        )
    with pytest.raises(ValidationError, match="independent"):
        approval(artifact, reviewer_id=artifact.review.producer_id)


def test_draft_representation_is_hashable_but_cannot_be_used() -> None:
    artifact = crosswalk(review=review(status="draft"))
    assert len(artifact.content_sha256()) == 64
    with pytest.raises(CrosswalkError, match="draft"):
        resolve(artifact)


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_id": "net_income"},
        {"semantic_reference": "changed-reference"},
        {"semantic_evidence_sha256": "b" * 64},
    ],
)
def test_changed_content_cannot_replay_approval(changes: dict[str, object]) -> None:
    original = crosswalk()
    changed = crosswalk(entry(**changes))
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(changed, context=trust(approval(original), approval(role())))


def test_approved_outside_policy_is_exactly_scoped_and_bound() -> None:
    exclusion = policy()
    query = key(tag="UnlistedSyntheticTag")
    result = resolve(query=query, policies=(exclusion,))
    assert result.state == "abstain"
    assert result.basis == "outside_crosswalk"
    assert result.policy == exclusion
    assert result.policy_approval == approval(exclusion)
    assert result.policy_sha256 == exclusion.content_sha256()
    assert resolve(query=key(namespace="unknown"), policies=(exclusion,)).state == "unresolved"
    assert resolve(policies=(exclusion,)).state == "mapped"


def test_outside_policy_requires_separate_active_approval() -> None:
    exclusion = policy()
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(policies=(exclusion,), context=trust(approval(crosswalk()), approval(role())))


def test_negative_control_policy_overrides_positive_and_explicit_null() -> None:
    assessment = role(role="negative_control")
    exclusion = policy("negative_control")
    for artifact in (crosswalk(), crosswalk(entry(canonical_id=None, abstention_reason="null"))):
        result = resolve(artifact, assessment=assessment, policies=(exclusion,))
        assert result.state == "abstain"
        assert result.basis == "negative_control"
        assert result.policy == exclusion
    result = resolve(assessment=assessment)
    assert result.state == "unresolved"
    assert result.reason == "negative_control_policy_missing"


def test_unknown_classification_never_defaults_to_corporate() -> None:
    result = resolve(assessment=role(role="unknown"), policies=(policy(),))
    assert result.state == "unresolved"
    assert result.reason == "unknown_role"
    with pytest.raises(ValidationError, match="role"):
        role(role="malformed-sic")


def test_role_requires_review_and_exact_source_binding() -> None:
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(context=trust(approval(crosswalk())))
    with pytest.raises(CrosswalkError, match="source_id"):
        resolve(assessment=role(source_id="other-filing"))
    with pytest.raises(CrosswalkError, match="draft"):
        resolve(assessment=role(review=review(status="draft")))


@pytest.mark.parametrize(
    "statement", [StatementType.COMPREHENSIVE_INCOME, StatementType.CASH_FLOW, StatementType.EQUITY]
)
def test_unsupported_statements_are_distinct_and_need_explicit_exclusion(
    statement: StatementType,
) -> None:
    query = key(statement=statement)
    assert resolve(query=query).state == "unresolved"
    with pytest.raises(CrosswalkError, match="canonical target"):
        resolve(crosswalk(entry(key=query)))
    artifact = crosswalk(
        entry(key=query, canonical_id=None, abstention_reason="reviewed exclusion")
    )
    assert resolve(artifact, query=query).basis == "entry"


@pytest.mark.parametrize(
    "changes",
    [
        {"namespace": ""},
        {"tag_taxonomy_version": ""},
        {"tag": 7},
        {"statement": "income"},
        {"extra": True},
    ],
)
def test_strict_key_schema(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        key(**changes)


@pytest.mark.parametrize("bad_hash", ["bad", "A" * 64, 1])
def test_invalid_evidence_hash_is_rejected(bad_hash: object) -> None:
    with pytest.raises(ValidationError):
        entry(semantic_evidence_sha256=bad_hash)


def test_null_requires_reason_and_positive_forbids_reason() -> None:
    with pytest.raises(ValidationError, match="abstention"):
        entry(canonical_id=None)
    with pytest.raises(ValidationError, match="abstention"):
        entry(abstention_reason="not allowed")


def test_boundary_revalidates_constructed_and_copied_nested_models() -> None:
    artifact = crosswalk()
    forged_entry = entry().model_copy(update={"semantic_evidence_sha256": "bad"})
    forged = artifact.model_copy(update={"entries": (entry(), forged_entry)})
    with pytest.raises(ValidationError, match="semantic_evidence_sha256"):
        resolve(forged, context=trust())
    forged_key = TagKey.model_construct(**{**key().model_dump(), "namespace": 5})
    with pytest.raises(ValidationError, match="namespace"):
        resolve(query=forged_key)
    bad_review = approval(artifact).model_copy(update={"evidence_sha256": "bad"})
    bad_context = trust().model_copy(update={"records": (bad_review,)})
    with pytest.raises(ValidationError, match="evidence_sha256"):
        resolve(context=bad_context)
    with pytest.raises(ValidationError, match="frozen"):
        artifact.entries = ()


def test_conflicting_trust_records_and_overlapping_policies_fail() -> None:
    record = approval(crosswalk())
    with pytest.raises(ValidationError, match="duplicate approval"):
        trust(record, record.model_copy(update={"status": "revoked"}))
    with pytest.raises(CrosswalkError, match="overlapping"):
        resolve(policies=(policy(), policy(policy_id="another-policy")))


def test_missing_key_is_unresolved_and_partial_key_is_invalid() -> None:
    artifact = crosswalk()
    assessment = role()
    result = resolve_tag(
        None,
        crosswalk=artifact,
        taxonomy=TAXONOMY,
        source_id=assessment.source_id,
        role=assessment,
        policies=(),
        trust=trust(approval(artifact), approval(assessment)),
    )
    assert result.state == "unresolved"
    assert result.reason == "missing_key"
    for missing in ("namespace", "tag_taxonomy_version", "statement", "tag"):
        partial = key().model_dump()
        del partial[missing]
        with pytest.raises(ValidationError, match=missing):
            TagKey.model_validate(partial)


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": "changed reason"},
        {"kind": "negative_control"},
        {"review": review(sha256="b" * 64)},
        {"scopes": (key(namespace="changed").scope(),)},
    ],
)
def test_changed_policy_invalidates_approval(changes: dict[str, object]) -> None:
    original = policy()
    changed = policy(**changes)
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(
            policies=(changed,),
            context=trust(approval(crosswalk()), approval(role()), approval(original)),
        )


def test_changed_role_and_review_evidence_cannot_replay_approval() -> None:
    original = role()
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(
            assessment=role(role="negative_control"),
            context=trust(approval(crosswalk()), approval(original)),
        )
    changed = crosswalk(review=review(sha256="b" * 64))
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(changed, context=trust(approval(crosswalk()), approval(original)))


@pytest.mark.parametrize("subject_kind", ["role", "policy"])
def test_revoked_role_and_policy_are_rejected(subject_kind: str) -> None:
    exclusion = policy()
    assessment = role()
    records = (
        approval(crosswalk()),
        approval(assessment, status="revoked" if subject_kind == "role" else "approved"),
        approval(exclusion, status="revoked" if subject_kind == "policy" else "approved"),
    )
    with pytest.raises(CrosswalkError, match="active approval"):
        resolve(policies=(exclusion,), context=trust(*records))


def test_negative_control_and_missing_key_cannot_hide_invalid_original() -> None:
    artifact = crosswalk(entry(), entry(key=key(tag="unused"), canonical_id="missing"))
    assessment = role(role="negative_control")
    exclusion = policy("negative_control")
    with pytest.raises(CrosswalkError, match="canonical target"):
        resolve(artifact, assessment=assessment, policies=(exclusion,))
    with pytest.raises(CrosswalkError, match="canonical target"):
        resolve_tag(
            None,
            crosswalk=artifact,
            taxonomy=TAXONOMY,
            source_id=assessment.source_id,
            role=assessment,
            policies=(),
            trust=trust(),
        )


def test_taxonomy_resolution_uses_fingerprinted_items_not_derived_indexes() -> None:
    artifact = crosswalk()
    corrupted_indexes = replace(TAXONOMY, _by_id={})
    result = resolve_tag(
        key(),
        crosswalk=artifact,
        taxonomy=corrupted_indexes,
        source_id=role().source_id,
        role=role(),
        policies=(),
        trust=trust(approval(artifact), approval(role())),
    )
    assert result.state == "mapped"
    assert result.canonical_id == "revenue"


def test_policy_order_and_scope_order_are_replay_stable() -> None:
    scopes = (key().scope(), key(statement=StatementType.BALANCE).scope())
    one = policy(scopes=scopes)
    reversed_scopes = policy(scopes=tuple(reversed(scopes)))
    assert one.content_sha256() == reversed_scopes.content_sha256()
    negative = policy("negative_control", scopes=scopes)
    assert resolve(policies=(one, negative)) == resolve(policies=(negative, one))


def test_strict_immutable_artifact_and_policy_containers() -> None:
    for changes in ({"schema_version": True}, {"entries": [entry()]}, {"schema_version": 2}):
        with pytest.raises(ValidationError):
            crosswalk(**changes)
    with pytest.raises(ValidationError, match="scopes"):
        policy(scopes=())
    with pytest.raises(ValidationError, match="duplicate"):
        policy(scopes=(key().scope(), key().scope()))
    with pytest.raises(ValidationError, match="authority_allowlist"):
        TrustContext(authority_allowlist={"synthetic-authority"}, records=())


def test_forged_policy_role_and_record_are_revalidated() -> None:
    forged_policy = policy().model_copy(update={"reason": None})
    with pytest.raises(ValidationError, match="reason"):
        resolve(policies=(forged_policy,))
    forged_role = role().model_copy(update={"role": "unrecognized"})
    with pytest.raises(ValidationError, match="role"):
        resolve(assessment=forged_role)
    record = approval(crosswalk())
    forged_record = record.model_copy(update={"reviewer_id": record.producer_id})
    with pytest.raises(ValidationError, match="independent"):
        resolve(context=trust().model_copy(update={"records": (forged_record,)}))


def test_extra_fields_and_hash_method_override_cannot_bypass_boundary() -> None:
    artifact = crosswalk()
    extra = artifact.model_copy(update={"approved": True})
    with pytest.raises(ValidationError, match="approved"):
        resolve(extra, context=trust())

    class ForgedCrosswalk(Crosswalk):
        def content_sha256(self) -> str:
            return artifact.content_sha256()

    forged = ForgedCrosswalk.model_validate(
        {**artifact.model_dump(), "entries": (entry(canonical_id="net_income"),)}
    )
    with pytest.raises(CrosswalkError, match="trusted approval"):
        resolve(forged, context=trust(approval(artifact), approval(role())))
