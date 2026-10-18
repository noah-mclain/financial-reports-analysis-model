"""Training examples from prepared SEC payloads, resolved under reviewed and approved artifacts.

The reviewed crosswalk, policies and role assessments, with their approval records, are read
from a trust directory the caller controls. Each row is resolved by ``sec_crosswalk.resolve_tag``
under them; the answer for a key is the same for every filing of one role, so it is computed
once per role and key (or scope, where the crosswalk has no entry). An unresolved key is a
counted drop, never a null target. Deduplication per issuer happens on lightweight candidates,
with the key of ``dataset_builder``, before any example object is built, and issuers are
processed in batches so memory does not grow with the number of rows.

``approved.txt`` lists the content hash of every emitted example. Here it means that the example
was resolved under approved artifacts; it does not say anyone read the example itself.
"""

from __future__ import annotations

import json
import resource
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataset_builder import dedupe_group_key, dedupe_lowest
from dataset_contracts import DatasetError, NormalizationExample, Verification
from pydantic import TypeAdapter, ValidationError
from sec_crosswalk import (
    AbstainResult,
    AbstentionPolicy,
    ApprovalRecord,
    Crosswalk,
    MappedResult,
    RoleAssessment,
    TagKey,
    TrustContext,
    UnresolvedResult,
    resolve_tag,
)
from sec_crosswalk_draft import CuratedMapping
from sec_source_access import (
    AccessContext,
    SecFiling,
    SecIndex,
    SourceManifest,
    VerifiedPayload,
    iter_payloads,
    read_registry,
)
from sec_source_decoder import Classifier, Dropped, decode_payload
from sec_source_pass import directory_opener

from fra_core.schemas.statement import StatementType
from fra_core.split import Look, Move
from fra_core.taxonomy import Taxonomy

TRUST_FILES = {
    "crosswalk": "reviewed_crosswalk.json",
    "policies": "reviewed_policies.json",
    "approvals": "approvals.json",
    "roles": "reviewed_role_assessments.json",
    "role_approvals": "role_approvals.json",
    "authorities": "authorities.json",
}
OUTPUT_FILES = ("examples.jsonl", "sources.jsonl", "approved.txt", "counts.json")
_POLICIES = TypeAdapter(tuple[AbstentionPolicy, ...])
_ROLES = TypeAdapter(tuple[RoleAssessment, ...])
_APPROVALS = TypeAdapter(tuple[ApprovalRecord, ...])
_AUTHORITIES = TypeAdapter(tuple[str, ...])


@dataclass(frozen=True)
class TrustBundle:
    crosswalk: Crosswalk
    policies: tuple[AbstentionPolicy, ...]
    roles: dict[str, RoleAssessment]
    trust: TrustContext


def _load[T](directory: Path, name: str, adapter: TypeAdapter[T]) -> T:
    path = directory / name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"{path}: cannot read: {exc.strerror}") from exc
    try:
        return adapter.validate_json(data)
    except ValidationError as exc:
        raise DatasetError(f"{path}: {exc}") from exc


def load_trust(directory: Path) -> TrustBundle:
    """The reviewed artifacts and the trust snapshot, from files the caller placed here."""
    crosswalk = _load(directory, TRUST_FILES["crosswalk"], TypeAdapter(Crosswalk))
    policies = _load(directory, TRUST_FILES["policies"], _POLICIES)
    roles = _load(directory, TRUST_FILES["roles"], _ROLES)
    records = (
        *_load(directory, TRUST_FILES["approvals"], _APPROVALS),
        *_load(directory, TRUST_FILES["role_approvals"], _APPROVALS),
    )
    authorities = _load(directory, TRUST_FILES["authorities"], _AUTHORITIES)
    by_id = {r.source_id: r for r in roles}
    if len(by_id) != len(roles):
        raise DatasetError(f"{directory}: a role assessment appears twice")
    try:
        trust = TrustContext(authority_allowlist=frozenset(authorities), records=records)
    except ValidationError as exc:
        raise DatasetError(f"{directory}: {exc}") from exc
    return TrustBundle(crosswalk=crosswalk, policies=policies, roles=by_id, trust=trust)


@dataclass(frozen=True)
class Candidate:
    """A resolved row before it becomes an example; small enough to hold per batch."""

    example_id: str
    cik: int
    adsh: str
    archive: str
    statement: StatementType
    label: str
    canonical_id: str | None
    abstention_reason: str | None
    key: TagKey
    role: str
    locator: str
    approval: ApprovalRecord


def _peak_rss() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


class ExampleBuilder:
    def __init__(
        self,
        *,
        prepared: Path,
        bundle: TrustBundle,
        mapping: CuratedMapping,
        taxonomy: Taxonomy,
        registry_path: Path,
        moves: Sequence[Move],
        looks: Sequence[Look],
        batch_issuers: int = 100,
        allow_conflicts_up_to: int | None = None,
    ) -> None:
        if mapping.sha256 != bundle.crosswalk.review.sha256:
            raise DatasetError("the reviewed crosswalk was not drafted from the mapping given")
        if batch_issuers < 1:
            raise DatasetError("batch_issuers must be at least 1")
        self._prepared = prepared
        self._bundle = bundle
        self._taxonomy = taxonomy
        self._registry = read_registry(registry_path)
        self._moves = moves
        self._looks = looks
        self._batch = batch_issuers
        self._allowed = allow_conflicts_up_to
        self._classifier = Classifier(mapping, bundle.crosswalk)
        self._decisions: dict[tuple[str, object], Any] = {}
        self._authorized: set[str] = set()
        self.counts: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()
        self.resolved: Counter[str] = Counter()

    def _archives(self) -> list[tuple[str, SecIndex, SourceManifest, AccessContext]]:
        found = []
        for base in sorted(p for p in self._prepared.iterdir() if p.is_dir()):
            index = SecIndex.model_validate_json((base / "index.json").read_bytes())
            manifest = SourceManifest.model_validate_json((base / "manifest.json").read_bytes())
            context = AccessContext.model_validate_json((base / "context.json").read_bytes())
            found.append((base.name, index, manifest, context))
        if not found:
            raise DatasetError(f"{self._prepared}: no prepared archive")
        return found

    def _decide(self, role: RoleAssessment, key: TagKey, source_id: str) -> Any:
        """The resolution of a key for a role, computed once per role and key or scope."""
        subject: object = key if self._classifier.has_entry(key) else key.scope()
        cached = self._decisions.get((role.role, subject))
        if cached is None:
            cached = resolve_tag(
                key,
                crosswalk=self._bundle.crosswalk,
                taxonomy=self._taxonomy,
                source_id=source_id,
                role=role,
                policies=self._bundle.policies,
                trust=self._bundle.trust,
            )
            self._decisions[(role.role, subject)] = cached
        return cached

    def _authorize_filing(self, role: RoleAssessment, source_id: str) -> None:
        """Every filing's role is checked against the trust snapshot, not just the first."""
        if source_id in self._authorized:
            return
        resolve_tag(
            None,
            crosswalk=self._bundle.crosswalk,
            taxonomy=self._taxonomy,
            source_id=source_id,
            role=role,
            policies=self._bundle.policies,
            trust=self._bundle.trust,
        )
        self._authorized.add(source_id)

    def _candidates(
        self, archive: str, verified: VerifiedPayload, filing: SecFiling
    ) -> Iterator[Candidate]:
        source_id = filing.source_record.source_id
        role = self._bundle.roles.get(source_id)
        if role is None:
            raise DatasetError(f"{source_id}: no role assessment in the trust directory")
        self._authorize_filing(role, source_id)
        cik = int(str(filing.source_record.issuer.cik))
        for row in decode_payload(verified):
            self.counts["rows_read"] += 1
            outcome = self._classifier.classify(row)
            if isinstance(outcome, Dropped):
                self.dropped[outcome.reason] += 1
                continue
            result = self._decide(role, outcome.key, source_id)
            if isinstance(result, UnresolvedResult):
                self.dropped[f"unresolved:{result.reason}"] += 1
                continue
            if isinstance(result, MappedResult):
                target, reason, approval = (
                    result.canonical_id,
                    None,
                    result.binding.crosswalk_approval,
                )
                self.resolved["mapped"] += 1
            elif isinstance(result, AbstainResult):
                approval = (
                    result.binding.crosswalk_approval
                    if result.basis == "entry"
                    else _policy_approval(result)
                )
                target, reason = None, result.reason
                self.resolved["abstain"] += 1
            else:  # pragma: no cover - the union is closed
                raise DatasetError(f"{source_id}: unexpected resolution {type(result).__name__}")
            yield Candidate(
                example_id=f"{source_id}:{row.report}:{row.line}",
                cik=cik,
                adsh=source_id,
                archive=archive,
                statement=outcome.key.statement,
                label=" ".join(row.plabel.split()),
                canonical_id=target,
                abstention_reason=reason,
                key=outcome.key,
                role=role.role,
                locator=f"{archive}:{source_id}:{row.report}:{row.line}",
                approval=approval,
            )

    def run(self, out_dir: Path) -> dict[str, Any]:
        archives = self._archives()
        filings = {
            f.source_record.source_id: f for _, index, _, _ in archives for f in index.filings
        }
        by_issuer: dict[int, list[str]] = {}
        for source_id, filing in sorted(filings.items()):
            by_issuer.setdefault(int(str(filing.source_record.issuer.cik)), []).append(source_id)
        try:
            out_dir.mkdir(parents=True)
        except FileExistsError:
            raise DatasetError(f"{out_dir} already exists; refusing to reuse it") from None
        partial = {name: out_dir / f"{name}.partial" for name in OUTPUT_FILES[:3]}
        try:
            with (
                partial["examples.jsonl"].open("x", encoding="utf-8") as examples_file,
                partial["approved.txt"].open("x", encoding="utf-8") as approved_file,
            ):
                for batch in _batches(sorted(by_issuer), self._batch):
                    wanted = {sid for cik in batch for sid in by_issuer[cik]}
                    candidates: list[Candidate] = []
                    for archive, index, manifest, context in archives:
                        ids = [a.artifact_id for a in manifest.artifacts if a.source_id in wanted]
                        if not ids:
                            continue
                        stream = iter_payloads(
                            ids,
                            index=index,
                            manifest=manifest,
                            registry=self._registry,
                            context=context,
                            moves=self._moves,
                            looks=self._looks,
                            opener=directory_opener(self._prepared),
                        )
                        for verified in stream:
                            sid = verified.descriptor.filing.source_record.source_id
                            candidates.extend(self._candidates(archive, verified, filings[sid]))
                    self._write_batch(candidates, filings, examples_file, approved_file)
            self._check_conflicts()
            sources = [filings[s].source_record.model_dump_json() for s in sorted(filings)]
            partial["sources.jsonl"].write_text("\n".join(sources) + "\n", encoding="utf-8")
            counts = self._counts(len(filings), len(by_issuer))
            partial_counts = out_dir / "counts.json.partial"
            partial_counts.write_text(
                json.dumps(counts, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            partial["counts.json"] = partial_counts
            _publish(partial, out_dir)
        except BaseException:
            for path in out_dir.glob("*.partial"):
                path.unlink()
            out_dir.rmdir()
            raise
        return counts

    def _check_conflicts(self) -> None:
        """Stop, rather than quietly drop, when many rows fall in conflicting groups."""
        rows, total = self.counts["conflicting_rows"], self.counts["rows_before_dedupe"]
        if self._allowed is not None:
            if rows > self._allowed:
                raise DatasetError(
                    f"{rows} conflicting rows of {total} candidates exceed "
                    f"--allow-conflicts-up-to {self._allowed}"
                )
        elif rows * 20 > total:
            raise DatasetError(
                f"{rows} conflicting rows of {total} candidates are more than 5%; "
                f"review them or pass --allow-conflicts-up-to {rows}"
            )

    def _write_batch(
        self,
        candidates: list[Candidate],
        filings: dict[str, SecFiling],
        examples_file: Any,
        approved_file: Any,
    ) -> None:
        self.counts["rows_before_dedupe"] += len(candidates)
        kept, conflicts = dedupe_lowest(
            candidates,
            group=lambda c: dedupe_group_key(c.cik, c.statement.value, c.label),
            target=lambda c: c.canonical_id,
            ident=lambda c: c.example_id,
        )
        self.counts["conflicting_groups"] += len(conflicts)
        self.counts["conflicting_rows"] += sum(len(ids) for ids in conflicts)
        self.counts["rows_after_dedupe"] += len(kept)
        for candidate in kept:
            approval = candidate.approval
            example = NormalizationExample(
                example_id=candidate.example_id,
                source_record=filings[candidate.adsh].source_record,
                statement=candidate.statement,
                label=candidate.label,
                canonical_id=candidate.canonical_id,
                abstention_reason=candidate.abstention_reason,
                role="negative_control" if candidate.role == "negative_control" else "corporate",
                months=None,
                source_locator=candidate.locator,
                tag=f"{candidate.key.namespace}:{candidate.key.tag}",
                provenance=None,
                verification=Verification(
                    method="reviewed_crosswalk",
                    producer_id=approval.producer_id,
                    reviewer_id=approval.reviewer_id,
                    evidence_id=approval.evidence_reference,
                    evidence_sha256=approval.evidence_sha256,
                ),
            )
            examples_file.write(example.model_dump_json() + "\n")
            approved_file.write(example.content_sha256() + "\n")
            self.counts["examples"] += 1

    def _counts(self, filings: int, issuers: int) -> dict[str, Any]:
        return {
            "rows_read": self.counts["rows_read"],
            "dropped": dict(sorted(self.dropped.items())),
            "resolved": dict(sorted(self.resolved.items())),
            "rows_before_dedupe": self.counts["rows_before_dedupe"],
            "rows_after_dedupe": self.counts["rows_after_dedupe"],
            "conflicting_groups": self.counts["conflicting_groups"],
            "conflicting_rows": self.counts["conflicting_rows"],
            "examples": self.counts["examples"],
            "filings": filings,
            "issuers": issuers,
            "peak_rss_bytes": _peak_rss(),
        }


def _publish(partial: dict[str, Path], out_dir: Path) -> None:
    """Rename every partial file into place; on a failure put the renamed ones back."""
    done: list[tuple[Path, Path]] = []
    try:
        for name, path in partial.items():
            target = out_dir / name
            path.rename(target)
            done.append((target, path))
    except BaseException:
        for target, path in done:
            target.rename(path)
        raise


def _policy_approval(result: AbstainResult) -> ApprovalRecord:
    if result.policy_approval is None:
        raise DatasetError(f"{result.key}: a policy abstention has no policy approval")
    return result.policy_approval


def _batches(items: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
