"""Review tooling for a draft SEC crosswalk: packet, owner sample and approval records.

The tooling prepares evidence and records a reviewer's verdicts as approval records; it decides
nothing. Approval is refused unless the verdict file approves every crosswalk entry and every
policy. The approval records name the reviewed copies of the artifacts (status ``reviewed``),
because a draft cannot be used and a status change changes the content hash an approval binds.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from typing import Any, Literal

from dataset_contracts import Contract, DatasetError
from pydantic import ValidationError
from sec_crosswalk import AbstentionPolicy, ApprovalRecord, Crosswalk, TagKey
from sec_crosswalk_draft import (
    DRAFT_PRODUCER_ID,
    STATEMENT_CODES,
    CuratedMapping,
    MappingExclusion,
    MappingRow,
    Vocabulary,
)

from fra_core.taxonomy import Taxonomy
from fra_core.taxonomy.loader import PLAN_CRITICAL_IDS

Row = dict[str, Any]
_CODE_BY_STATEMENT = {statement: code for code, statement in STATEMENT_CODES.items()}


class Verdict(Contract):
    key: str
    verdict: Literal["approve", "reject"]
    note: str


class ReviewedPackage(Contract):
    """The reviewed copies of the artifacts and the approval records that bind them."""

    crosswalk: Crosswalk
    policies: tuple[AbstentionPolicy, ...]
    approvals: tuple[ApprovalRecord, ...]


def entry_key(key: TagKey) -> str:
    return f"{key.namespace}|{key.tag_taxonomy_version}|{key.statement.value}|{key.tag}"


def policy_key(policy: AbstentionPolicy) -> str:
    return f"policy:{policy.policy_id}"


def exclusion_key(exclusion: MappingExclusion) -> str:
    return f"excluded:{exclusion.statement}:{exclusion.tag}"


def subjects_header(
    crosswalk: Crosswalk, policies: Sequence[AbstentionPolicy], mapping: CuratedMapping
) -> Row:
    """The exact artifacts a review covers; the reviewer echoes it as the verdicts' first line."""
    return {
        "kind": "subjects",
        "mapping_sha256": mapping.sha256,
        "crosswalk_sha256": crosswalk.content_sha256(),
        "policies": {p.policy_id: p.content_sha256() for p in policies},
    }


def review_packet(
    crosswalk: Crosswalk,
    policies: Sequence[AbstentionPolicy],
    vocabulary: Vocabulary,
    mapping: CuratedMapping,
    taxonomy: Taxonomy,
) -> list[Row]:
    """The subjects header, then one JSON-ready row per entry, policy and excluded tag."""
    items = {item.id: item for item in taxonomy.items}
    rows: dict[tuple[str, str], MappingRow] = {
        (row.statement, row.tag): row for row in mapping.rows
    }
    packet: list[Row] = []
    for entry in crosswalk.entries:
        key = entry.key
        version = f"{key.namespace}/{key.tag_taxonomy_version}"
        definition = vocabulary[version][key.tag]
        mapped = rows[(_CODE_BY_STATEMENT[key.statement], key.tag)]
        item = items[entry.canonical_id] if entry.canonical_id else None
        packet.append(
            {
                "kind": "entry",
                "key": entry_key(key),
                "tag": key.tag,
                "version": version,
                "statement": key.statement.value,
                "tlabel": definition.tlabel,
                "doc": definition.doc,
                "canonical_id": entry.canonical_id,
                "target": None
                if item is None
                else {
                    "id": item.id,
                    "statement": item.statement.value,
                    "natural_sign": item.natural_sign,
                    "aliases": list(item.aliases_for("en")),
                    "headings": list(item.headings_for("en")),
                },
                "reason": mapped.reason,
            }
        )
    for policy in policies:
        packet.append(
            {
                "kind": "policy",
                "key": policy_key(policy),
                "policy_kind": policy.kind,
                "reason": policy.reason,
                "scopes": [scope.model_dump(mode="json") for scope in policy.scopes],
            }
        )
    for exclusion in mapping.excluded:
        packet.append(
            {
                "kind": "excluded",
                "key": exclusion_key(exclusion),
                "statement": exclusion.statement,
                "tag": exclusion.tag,
                "reason": exclusion.reason,
                "definitions": {
                    version: {"tlabel": tags[exclusion.tag].tlabel, "doc": tags[exclusion.tag].doc}
                    for version, tags in sorted(vocabulary.items())
                    if version.startswith(f"{mapping.namespace}/") and exclusion.tag in tags
                },
            }
        )
    header = subjects_header(crosswalk, policies, mapping)
    return [header, *sorted(packet, key=lambda row: (row["kind"], row["key"]))]


def owner_sample(packet: Sequence[Row], n: int, seed: int) -> list[Row]:
    """Every critical-item entry, every policy and a seeded draw from the rest, nulls included.

    ``n`` counts entries; the critical entries and the policies are always kept, so a small
    ``n`` still returns them.
    """
    if n < 1:
        raise DatasetError(f"sample size n must be at least 1, got {n}")
    headers = [r for r in packet if r["kind"] == "subjects"]
    if len(headers) != 1:
        raise DatasetError(f"a review packet has one subjects header, found {len(headers)}")
    entries = sorted((r for r in packet if r["kind"] == "entry"), key=lambda r: r["key"])
    critical = [r for r in entries if r["canonical_id"] in PLAN_CRITICAL_IDS]
    rest = [r for r in entries if r["canonical_id"] not in PLAN_CRITICAL_IDS]
    nulls = [r for r in rest if r["canonical_id"] is None]
    mapped = [r for r in rest if r["canonical_id"] is not None]
    room = max(0, n - len(critical))
    null_quota = min(len(nulls), max(1, round(room * len(nulls) / len(rest)))) if rest else 0
    null_quota = min(null_quota, room)
    mapped_quota = min(len(mapped), room - null_quota)
    rng = random.Random(seed)
    chosen = critical + rng.sample(nulls, null_quota) + rng.sample(mapped, mapped_quota)
    chosen += [r for r in packet if r["kind"] not in ("entry", "subjects")]
    return [*headers, *sorted(chosen, key=lambda r: (r["kind"], r["key"]))]


def parse_verdicts(data: bytes) -> tuple[Row, dict[str, Verdict]]:
    """The echoed subjects header and the verdicts by key; malformed lines are refused."""
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise DatasetError(f"the verdict file is not UTF-8: {exc.reason}") from exc
    numbered = [(n, line) for n, line in enumerate(lines, start=1) if line.strip()]
    if not numbered:
        raise DatasetError("the verdict file holds no verdicts")
    number, first = numbered[0]
    try:
        header = json.loads(first)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"verdicts line {number}: not JSON: {exc.msg}") from exc
    if not isinstance(header, dict):
        raise DatasetError(f"verdicts line {number}: the subjects header is missing")
    verdicts: dict[str, Verdict] = {}
    for number, line in numbered[1:]:
        try:
            verdict = Verdict.model_validate_json(line)
        except ValidationError as exc:
            raise DatasetError(f"verdicts line {number}: {exc}") from exc
        if verdict.key in verdicts:
            raise DatasetError(f"verdicts line {number}: {verdict.key} has two verdicts")
        verdicts[verdict.key] = verdict
    return header, verdicts


def _check_draft(subject: Crosswalk | AbstentionPolicy, name: str, mapping: CuratedMapping) -> None:
    review = subject.review
    if review.status != "draft":
        raise DatasetError(f"{name}: only a draft can be sent for approval, not {review.status}")
    if review.producer_id != DRAFT_PRODUCER_ID:
        raise DatasetError(f"{name}: producer {review.producer_id!r} is not {DRAFT_PRODUCER_ID!r}")
    if review.sha256 != mapping.sha256:
        raise DatasetError(f"{name}: not drafted from the mapping {mapping.source_name} given")


def approval_records(
    crosswalk: Crosswalk,
    policies: Sequence[AbstentionPolicy],
    mapping: CuratedMapping,
    verdicts: bytes,
    *,
    reviewer_id: str,
    authority_id: str,
    evidence_reference: str,
) -> ReviewedPackage:
    """Approval records for the crosswalk and policies, only if every verdict is an approval.

    The verdict file starts with the subjects header copied from the packet; it must equal the
    header recomputed from the artifacts given, so a verdict cannot be reused for edited content.
    The verdicts must also cover the mapping's excluded tags, which no artifact carries.
    """
    crosswalk = Crosswalk.model_validate(crosswalk)
    policies = tuple(AbstentionPolicy.model_validate(p) for p in policies)
    ids = [p.policy_id for p in policies]
    if len(set(ids)) != len(ids):
        raise DatasetError(
            f"policy_id is not unique: {sorted({i for i in ids if ids.count(i) > 1})}"
        )
    entry_keys = [entry_key(e.key) for e in crosswalk.entries]
    if len(set(entry_keys)) != len(entry_keys):
        raise DatasetError("the crosswalk lists an entry key twice")
    exclusions = [exclusion_key(x) for x in mapping.excluded]
    if len(set(exclusions)) != len(exclusions):
        raise DatasetError("the mapping lists an excluded tag twice")
    _check_draft(crosswalk, "crosswalk", mapping)
    for policy in policies:
        _check_draft(policy, f"policy {policy.policy_id}", mapping)
    header, parsed = parse_verdicts(verdicts)
    if header != subjects_header(crosswalk, policies, mapping):
        raise DatasetError(
            "the verdict file's subjects header differs from the artifacts given; "
            "copy the header of the review packet that was reviewed"
        )
    expected = (
        {entry_key(e.key) for e in crosswalk.entries}
        | {policy_key(p) for p in policies}
        | {exclusion_key(x) for x in mapping.excluded}
    )
    unknown = sorted(set(parsed) - expected)
    if unknown:
        raise DatasetError(f"verdicts for keys that are not in the package: {unknown[:5]}")
    missing = sorted(expected - set(parsed))
    if missing:
        raise DatasetError(
            f"{len(missing)} entries or policies have no verdict, e.g. {missing[:5]}"
        )
    rejected = sorted(key for key, v in parsed.items() if v.verdict == "reject")
    if rejected:
        raise DatasetError(f"{len(rejected)} rejected, e.g. {rejected[:5]}")
    reviewed_crosswalk = crosswalk.model_copy(
        update={"review": crosswalk.review.model_copy(update={"status": "reviewed"})}
    )
    reviewed_policies = tuple(
        p.model_copy(update={"review": p.review.model_copy(update={"status": "reviewed"})})
        for p in policies
    )
    evidence = hashlib.sha256(verdicts).hexdigest()

    def record(kind: Literal["crosswalk", "abstention_policy"], subject: Any) -> ApprovalRecord:
        return ApprovalRecord(
            subject_kind=kind,
            subject_sha256=subject.content_sha256(),
            producer_id=subject.review.producer_id,
            reviewer_id=reviewer_id,
            authority_id=authority_id,
            status="approved",
            evidence_reference=evidence_reference,
            evidence_sha256=evidence,
        )

    return ReviewedPackage(
        crosswalk=Crosswalk.model_validate(reviewed_crosswalk),
        policies=tuple(AbstentionPolicy.model_validate(p) for p in reviewed_policies),
        approvals=(
            record("crosswalk", reviewed_crosswalk),
            *(record("abstention_policy", p) for p in reviewed_policies),
        ),
    )
