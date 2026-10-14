# Reviewed SEC crosswalk contract

`training/sec_crosswalk.py` is an injected, synthetic contract for exact tag resolution.
It implements the review boundary needed before the Week 3 normalization adapter in
[the revised plan](../blueprint/08-revised-plan.md). It neither supplies real SEC mappings
nor generates training examples. The blanket outside-crosswalk null behavior sketched in
[execution phases 2.3](../blueprint/04-execution-phases.md#23-normalization-dataset) is usable
here only with a separately approved, explicit scope policy. Missing semantics otherwise
remain unresolved.

## Input and authority boundary

The caller supplies the current loaded `fra_core.taxonomy.Taxonomy`, a `Crosswalk`, a
`RoleAssessment` for the exact `source_id`, a tuple of `AbstentionPolicy` objects, and a
`TrustContext`. None of these inputs is read from disk by this module. Its strict, frozen
models reject unknown fields, coercible Python types, blank identifiers and invalid
lowercase SHA-256 values. Public resolution and content-hash methods revalidate instances,
including nested models produced with unchecked construction or copying.

`TagKey` consists of namespace, `tag_taxonomy_version`, `StatementType` and tag. Every
component matches exactly, without case folding, aliases or source-version parsing.
`canonical_taxonomy_version` is a separate integer identifying the injected canonical
taxonomy; it does not identify the SEC tag taxonomy. A future reviewed adapter must provide
namespace and tag taxonomy version explicitly. An arbitrary raw source `version` string
cannot establish either field.

A `CrosswalkEntry` contains its key, a canonical id or an explicit abstention reason, and
a semantic reference and evidence hash. A null id requires a reason; a positive id forbids
one. Every positive target must exist in the current taxonomy and belong to the same
statement. All original entries are checked before lookup, including entries the requested
key will never use. Conflicting duplicate keys fail; identical occurrences are permitted
and remain in the fingerprint. There is no last-write-wins selection.

`ReviewEvidence.status` is `draft` or `reviewed`. Drafts can be represented and hashed for
review, but cannot be used. Neither this metadata nor a self-declared flag establishes
approval. Each usable crosswalk, policy and known role assessment needs its own externally
injected `ApprovalRecord` for the exact subject kind and content hash. The record must be
active (`approved`), name an allowlisted authority, match the artifact producer and name a
different reviewer. Draft, rejected and revoked records fail. A current snapshot permits
only one record per subject kind/hash, so a revoked and approved pair cannot be selected
by order.

The caller must authenticate the real reviewer and authority, resolve identity aliases,
retain evidence bytes, verify those bytes against the evidence hashes, and supply a complete,
fresh snapshot including revocations. A fabricated record becomes trusted if the caller
incorrectly injects it as authoritative. This module cannot authenticate a person, verify a
signature, discover missing revocations or distinguish a stale externally supplied snapshot
from a current one. Synthetic identifiers in tests establish none of these properties.
The owner still needs to decide real review authority, approval retention and revocation
operations, version interpretation and the semantic review procedure before real use.

## Roles and abstention

`RoleAssessment` records source identity, `corporate`, `negative_control` or `unknown`, and
review evidence. Known roles require independent approval of that exact assessment. Unknown
roles produce unresolved results, never corporate supervision. No SIC parser or classifier
is duplicated here; a future caller must resolve malformed or missing industry information
through a reviewed classification boundary. A role assessment for another source fails.

Each policy carries its own id, review evidence, canonical taxonomy identity, reason and
nonempty scopes. A scope lists exact namespace, tag taxonomy version and statement, and
covers all tags within that scope. There are no namespace/version wildcards. Repeated
scopes, duplicate policy ids and overlapping policies of the same kind fail. Crosswalk and
all injected policies are validated and authorized before selection, even when unused.

| Reviewed role and input | Result |
| --- | --- |
| Corporate, positive exact entry | `mapped` with the canonical id and semantic evidence |
| Corporate, explicit null exact entry | `abstain`, basis `entry`, with the reviewed reason |
| Corporate, absent entry, approved matching outside policy | `abstain`, basis `outside_crosswalk` |
| Corporate, absent entry, no matching outside policy | `unresolved`, reason `missing_entry` |
| Negative control, approved matching negative-control policy | `abstain`, basis `negative_control`, overriding positive and null entries |
| Negative control, no matching negative-control policy | `unresolved`, reason `negative_control_policy_missing` |
| Unknown role | `unresolved`, reason `unknown_role` |
| Entire tag key absent | `unresolved`, reason `missing_key` |

A partial or malformed key raises `ValidationError`. Invalid targets or stale taxonomy
identity raise `CrosswalkError`. Draft artifacts, absent trusted approvals, untrusted
authorities, producer mismatches and inactive approvals also raise `CrosswalkError`.
An injected unapproved policy fails rather than being silently ignored. Authorization
errors must be corrected or sent for review; they are not null labels.

CI (comprehensive income), CF (cash flow) and EQ (equity) remain separate `StatementType`
values. The existing canonical taxonomy only covers income and balance statements. No
positive mapping for CI/CF/EQ can pass against it. These statements need approved explicit
null entries or separately approved scope policies to abstain; otherwise they remain
unresolved. No taxonomy additions or statement collapsing are included in this task.

## Output and reproducibility

`resolve_tag` returns a discriminated `MappedResult`, `AbstainResult` or `UnresolvedResult`.
Unresolved output has no canonical target and cannot be mistaken for null supervision.
Every result retains the exact query and a binding containing source identity, crosswalk
hash and approval record, canonical taxonomy version/hash, and role hash and approval
record (absent for an unknown role). Mapped and entry-abstention results carry the selected
semantic reference/hash. Policy abstentions retain the complete policy, its content hash
and its separate approval record, including approval evidence. No implicit confidence is
added. These outputs are resolution evidence, not authenticated or approved training rows.

Fingerprints use SHA-256 over UTF-8 JSON with sorted object keys, compact separators,
Unicode preserved and nonfinite numbers forbidden. Crosswalk entry order and policy scope
order are canonicalized; identical entry occurrence counts are retained. Policy tuple order
does not select a winner. The canonical taxonomy fingerprint covers version, id, statement,
natural sign, critical/subtotal/per-share flags, aliases and headings. Item and mapping-key
order are ignored, while alias and heading tuple order is preserved. Target checks use
these fingerprinted items rather than separate derived lookup indexes.

Changed targets, scope, policy reasons, roles, artifact review metadata or semantic evidence
change their subject hash and cannot replay an earlier approval. External approval metadata
is retained exactly in the result; its authenticity and revisions remain the caller's
responsibility. Hash equality proves identity under this serialization convention, not
semantic equivalence or human verification. There is no broader ordering guarantee for
other serializers or future schema versions.

## Verification and future adapter

The synthetic tests exercise schema rejection, exact namespace/version/statement/tag lookup,
canonical target checks, duplicate conflicts, approval replay and revocation, taxonomy
semantics, negative-control precedence, missing keys, unknown roles, order stability and
unchecked model mutations. They use invented tags and review ids and the existing canonical
taxonomy. They open no real label files or corpus rows. Passing these tests demonstrates
mechanical contract behavior, not annotation accuracy, coverage, Gate C readiness or real
review authenticity. Semantic equivalence, industry decisions and policy appropriateness
require separate human verification.

Focused checks run from the assigned worktree:

```sh
uv run pytest tests/training/test_sec_crosswalk.py
uv run mypy --strict training/sec_crosswalk.py
make test
make lint
make typecheck
make docs-check
git diff --check
```

The task packet's offline environment variables select installed tooling without syncing
or downloading. Private `.pi/checks/` artifacts record the commands, exits and output.
`make typecheck` permanently includes `training/sec_crosswalk.py` under the repository's
existing strict mypy configuration. The explicit strict command above remains useful for a
focused check of this module.

A future adapter must authorize source access first, provide reviewed statement-code and
namespace/version interpretation, supply the exact source-bound role assessment and current
trusted approvals, and retain resolution evidence in separately approved examples. It must
not convert unresolved results to null, infer periods from filing metadata, or use positive
entries for negative controls. This module does not produce `NormalizationExample`, alter
labels, infer numbers, duration, sign or hierarchy, or import the unfinished source-access
sibling. The presentation-label duration clarification is a separate decision.

Real crosswalk YAML, approval archives, source acquisition, historical reconciliation,
annotation, dataset deduplication/capping, model calls, training, scoring and taxonomy changes
are deliberately omitted. No aliases are used to guess real tags. D13/D16/D17/D18 remain
unchanged: issuer-separated fit/validation and authorized provisional train-holdout
evaluation precede the owner checkpoint; `model_test` and `blind` are not opened or scored
here. Independent review follows this implementation before finalization.
