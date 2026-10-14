"""Synthetic SEC tag resolution with externally supplied review authorization.

No source-version parser, industry classifier or training-example builder lives here.
The caller authenticates reviewers and injects a complete current approval snapshot.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from dataset_contracts import Sha256
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from fra_core.schemas.statement import StatementType
from fra_core.taxonomy import Taxonomy

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(?:[^\r\n]*\S)?$")]


class CrosswalkError(ValueError):
    """Invalid taxonomy, scope or review authorization; no usable result."""


class _Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


def _digest(value: object) -> str:
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def taxonomy_sha256(taxonomy: Taxonomy) -> str:
    """Fingerprint all canonical item semantics, independent of item/map order.

    Alias and heading tuple order is preserved. Derived lookup indexes are not used
    for resolution: targets are checked directly against these fingerprinted items.
    """
    if len({item.id for item in taxonomy.items}) != len(taxonomy.items):
        raise CrosswalkError("canonical taxonomy has duplicate ids")
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


class TagScope(_Contract):
    """Exact namespace, tag taxonomy version and statement; no version parsing."""

    namespace: Text
    tag_taxonomy_version: Text
    statement: StatementType


class TagKey(TagScope):
    tag: Text

    def scope(self) -> TagScope:
        checked = TagKey.model_validate(self)
        return TagScope(
            namespace=checked.namespace,
            tag_taxonomy_version=checked.tag_taxonomy_version,
            statement=checked.statement,
        )


class ReviewEvidence(_Contract):
    """Artifact review metadata; even 'reviewed' does not establish trust."""

    producer_id: Text
    status: Literal["draft", "reviewed"]
    reference: Text
    sha256: Sha256


class _Subject(_Contract):
    review: ReviewEvidence

    def content_sha256(self) -> str:
        checked = type(self).model_validate(self)
        return _digest(checked.model_dump(mode="json"))


class _TaxonomySubject(_Subject):
    canonical_taxonomy_version: int = Field(ge=1)
    canonical_taxonomy_sha256: Sha256


class CrosswalkEntry(_Contract):
    key: TagKey
    canonical_id: Text | None
    abstention_reason: Text | None
    semantic_reference: Text
    semantic_evidence_sha256: Sha256

    @model_validator(mode="after")
    def _target(self) -> CrosswalkEntry:
        if (self.canonical_id is None) != (self.abstention_reason is not None):
            raise ValueError("null target requires an abstention reason only")
        return self


class Crosswalk(_TaxonomySubject):
    schema_version: int = Field(ge=1, le=1)
    entries: tuple[CrosswalkEntry, ...]

    @model_validator(mode="after")
    def _keys(self) -> Crosswalk:
        seen: dict[TagKey, CrosswalkEntry] = {}
        for entry in self.entries:
            previous = seen.get(entry.key)
            if previous is not None and previous != entry:
                raise ValueError(f"conflicting duplicate key: {entry.key}")
            seen[entry.key] = entry
        return self

    def content_sha256(self) -> str:
        """Entry order is irrelevant; every identical occurrence remains hash-bound."""
        checked = Crosswalk.model_validate(self)
        payload = checked.model_dump(mode="json")
        payload["entries"] = sorted(
            (entry.model_dump(mode="json") for entry in checked.entries),
            key=lambda entry: json.dumps(entry, sort_keys=True, ensure_ascii=False),
        )
        return _digest(payload)


class AbstentionPolicy(_TaxonomySubject):
    """Separately reviewed exclusion for all tags in explicitly listed scopes."""

    policy_id: Text
    kind: Literal["outside_crosswalk", "negative_control"]
    scopes: tuple[TagScope, ...] = Field(min_length=1)
    reason: Text

    @model_validator(mode="after")
    def _scopes(self) -> AbstentionPolicy:
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError(f"{self.policy_id}: duplicate policy scope")
        return self

    def content_sha256(self) -> str:
        checked = AbstentionPolicy.model_validate(self)
        payload = checked.model_dump(mode="json")
        payload["scopes"] = sorted(
            (scope.model_dump(mode="json") for scope in checked.scopes),
            key=lambda scope: json.dumps(scope, sort_keys=True, ensure_ascii=False),
        )
        return _digest(payload)


class RoleAssessment(_Subject):
    """Injected reviewed classification, bound to a source identity; no SIC fallback."""

    source_id: Text
    role: Literal["corporate", "negative_control", "unknown"]


SubjectKind = Literal["crosswalk", "abstention_policy", "role_assessment"]


class ApprovalRecord(_Contract):
    """Trusted only when independently supplied by the caller's review authority."""

    subject_kind: SubjectKind
    subject_sha256: Sha256
    producer_id: Text
    reviewer_id: Text
    authority_id: Text
    status: Literal["approved", "draft", "rejected", "revoked"]
    evidence_reference: Text
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def _independent(self) -> ApprovalRecord:
        if self.producer_id == self.reviewer_id:
            raise ValueError("approval requires an independent reviewer")
        return self


class TrustContext(_Contract):
    """Authoritative current snapshot, never loaded from a crosswalk artifact.

    Completeness, authenticity, identity equivalence and freshness are caller obligations.
    No embedded record or synthetic identifier authenticates a person or an authority.
    """

    authority_allowlist: frozenset[Text]
    records: tuple[ApprovalRecord, ...]

    @model_validator(mode="after")
    def _unique(self) -> TrustContext:
        subjects = [(record.subject_kind, record.subject_sha256) for record in self.records]
        if len(set(subjects)) != len(subjects):
            raise ValueError("duplicate approval subject in current snapshot")
        return self


class ResolutionBinding(_Contract):
    crosswalk_sha256: Sha256
    crosswalk_approval: ApprovalRecord
    canonical_taxonomy_version: int = Field(ge=1)
    canonical_taxonomy_sha256: Sha256
    role_sha256: Sha256
    role_approval: ApprovalRecord | None
    source_id: Text


class _Resolution(_Contract):
    key: TagKey | None
    binding: ResolutionBinding


class MappedResult(_Resolution):
    key: TagKey
    state: Literal["mapped"] = "mapped"
    canonical_id: Text
    semantic_reference: Text
    semantic_evidence_sha256: Sha256


class AbstainResult(_Resolution):
    key: TagKey
    state: Literal["abstain"] = "abstain"
    reason: Text
    basis: Literal["entry", "outside_crosswalk", "negative_control"]
    semantic_reference: Text | None
    semantic_evidence_sha256: Sha256 | None
    policy: AbstentionPolicy | None
    policy_sha256: Sha256 | None
    policy_approval: ApprovalRecord | None


class UnresolvedResult(_Resolution):
    state: Literal["unresolved"] = "unresolved"
    reason: Literal[
        "missing_key", "missing_entry", "unknown_role", "negative_control_policy_missing"
    ]


Resolution = Annotated[
    MappedResult | AbstainResult | UnresolvedResult, Field(discriminator="state")
]


def _approval(subject: _Subject, kind: SubjectKind, trust: TrustContext) -> ApprovalRecord:
    if subject.review.status == "draft":
        raise CrosswalkError(f"{kind}: draft cannot be used")
    digest = subject.content_sha256()
    record = next(
        (
            record
            for record in trust.records
            if record.subject_kind == kind and record.subject_sha256 == digest
        ),
        None,
    )
    if record is None:
        raise CrosswalkError(f"{kind}: no trusted approval for exact content {digest}")
    if record.status != "approved":
        raise CrosswalkError(f"{kind}: no active approval ({record.status})")
    if record.authority_id not in trust.authority_allowlist:
        raise CrosswalkError(f"{kind}: untrusted authority {record.authority_id}")
    if record.producer_id != subject.review.producer_id:
        raise CrosswalkError(f"{kind}: approval producer differs from artifact producer")
    return record


def _check_taxonomy(subject: _TaxonomySubject, taxonomy: Taxonomy, digest: str) -> None:
    if (
        subject.canonical_taxonomy_version != taxonomy.version
        or subject.canonical_taxonomy_sha256 != digest
    ):
        raise CrosswalkError("stale canonical taxonomy version or content hash")


def resolve_tag(
    key: TagKey | None,
    *,
    crosswalk: Crosswalk,
    taxonomy: Taxonomy,
    source_id: str,
    role: RoleAssessment,
    policies: tuple[AbstentionPolicy, ...],
    trust: TrustContext,
) -> Resolution:
    """Authorize all originals before resolving one exact key.

    Schema failures raise ValidationError; invalid targets/taxonomy/authorization raise
    CrosswalkError. Missing semantics and unknown roles return explicit unresolved states,
    never null supervision. Policy order and entry order cannot select a conflicting result.
    """
    crosswalk = Crosswalk.model_validate(crosswalk)
    role = RoleAssessment.model_validate(role)
    trust = TrustContext.model_validate(trust)
    policies = tuple(AbstentionPolicy.model_validate(policy) for policy in policies)
    key = TagKey.model_validate(key) if key is not None else None
    digest = taxonomy_sha256(taxonomy)
    _check_taxonomy(crosswalk, taxonomy, digest)
    targets = {item.id: item for item in taxonomy.items}
    for original in crosswalk.entries:
        if original.canonical_id is not None:
            item = targets.get(original.canonical_id)
            if item is None or item.statement != original.key.statement:
                raise CrosswalkError(
                    f"{original.key}: invalid canonical target {original.canonical_id}"
                )
    crosswalk_approval = _approval(crosswalk, "crosswalk", trust)
    if role.source_id != source_id:
        raise CrosswalkError(f"role source_id {role.source_id!r} does not match {source_id!r}")
    role_approval = None if role.role == "unknown" else _approval(role, "role_assessment", trust)
    approved_policies: dict[tuple[str, TagScope], tuple[AbstentionPolicy, ApprovalRecord]] = {}
    policy_ids: set[str] = set()
    for policy in policies:
        _check_taxonomy(policy, taxonomy, digest)
        policy_approval = _approval(policy, "abstention_policy", trust)
        if policy.policy_id in policy_ids:
            raise CrosswalkError(f"duplicate policy_id: {policy.policy_id}")
        policy_ids.add(policy.policy_id)
        for scope in policy.scopes:
            scope_key = (policy.kind, scope)
            if scope_key in approved_policies:
                raise CrosswalkError(f"overlapping {policy.kind} policy scope: {scope}")
            approved_policies[scope_key] = (policy, policy_approval)
    binding = ResolutionBinding(
        crosswalk_sha256=crosswalk.content_sha256(),
        crosswalk_approval=crosswalk_approval,
        canonical_taxonomy_version=taxonomy.version,
        canonical_taxonomy_sha256=digest,
        role_sha256=role.content_sha256(),
        role_approval=role_approval,
        source_id=source_id,
    )
    if key is None:
        return UnresolvedResult(key=key, binding=binding, reason="missing_key")
    if role.role == "unknown":
        return UnresolvedResult(key=key, binding=binding, reason="unknown_role")
    policy_kind = "negative_control" if role.role == "negative_control" else "outside_crosswalk"
    exclusion = approved_policies.get((policy_kind, key.scope()))
    entry = next((entry for entry in crosswalk.entries if entry.key == key), None)
    if role.role == "negative_control" or entry is None:
        if exclusion is not None:
            policy, policy_approval = exclusion
            return AbstainResult(
                key=key,
                binding=binding,
                reason=policy.reason,
                basis=policy.kind,
                semantic_reference=None,
                semantic_evidence_sha256=None,
                policy=policy,
                policy_sha256=policy.content_sha256(),
                policy_approval=policy_approval,
            )
        reason: Literal["negative_control_policy_missing", "missing_entry"] = (
            "negative_control_policy_missing"
            if role.role == "negative_control"
            else "missing_entry"
        )
        return UnresolvedResult(key=key, binding=binding, reason=reason)
    if entry.canonical_id is None:
        # The entry validator guarantees a reason whenever its target is null.
        assert entry.abstention_reason is not None
        return AbstainResult(
            key=key,
            binding=binding,
            reason=entry.abstention_reason,
            basis="entry",
            semantic_reference=entry.semantic_reference,
            semantic_evidence_sha256=entry.semantic_evidence_sha256,
            policy=None,
            policy_sha256=None,
            policy_approval=None,
        )
    return MappedResult(
        key=key,
        binding=binding,
        canonical_id=entry.canonical_id,
        semantic_reference=entry.semantic_reference,
        semantic_evidence_sha256=entry.semantic_evidence_sha256,
    )
