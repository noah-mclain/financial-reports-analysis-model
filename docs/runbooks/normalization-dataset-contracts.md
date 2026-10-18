# Normalization dataset contracts

`training/dataset_contracts.py` validates injected metadata and independently approved
normalization examples. `partition_examples` returns immutable in-memory fit and validation
tuples plus a deterministic manifest. It performs no file access, downloads, model calls,
training, narration or scoring. Synthetic tests establish contract behavior, not accuracy,
dataset readiness or Gate C success.

## Source and supervision records

`SourceRecord` records a stable source id, the core `Identity`, recorded top-level `Pool`,
SEC/PDF source, language, annual/interim stratum, period end, source SHA-256 and preparation
configuration SHA-256. Use a filing/document identity, not just an issuer-year or label.
Hashes are lowercase SHA-256 hex strings. Syntax validation does not verify the bytes;
the caller must compute them from the retained source and exact configuration.

`NormalizationExample` records a stable example id, its source record, statement, printed
label, explicit canonical id or `None`, role, column duration, source locator and verification.
SEC presentation rows have no column, so their months is always `None`. For PDF rows,
balance-sheet instants have no duration and flow statements require months. Period stratum and
duration remain distinct. Canonical ids are validated against the injected core taxonomy and
must belong to the example's statement. Targets are looked up in the taxonomy's items, the
same items the manifest fingerprints, and a taxonomy with a duplicate id is refused. A null
target needs a nonempty abstention reason; positive targets cannot carry that reason.
Negative controls must abstain.

PDF examples require the existing core page/cell `Provenance`, with finite ordered coordinates
and a table reference. SEC examples require a tag and a source locator identifying the
retained presentation row; they cannot claim PDF provenance. SEC labels are English. The
contract carries no numeric values, invented context, siblings, parents or narrative targets.
Extra fields are refused, including numeric extraction payloads.
A provenance value that differs from its validated form, for example a copied or
constructed instance holding a string coordinate, is refused rather than normalized.

`Verification` distinguishes an `extraction_draft`, a `reviewed_crosswalk` and an
`independent_review`. Evidence has an id/hash and identifies preparer and reviewer. Reviewed
methods require different identities; crosswalk review applies only to SEC tags. Drafts
remain representable for review but are always refused as fit/validation supervision.
An extracted tag, value alignment, taxonomy lookup or self-declared review does not prove truth.

## Establish authorization before opening labels

Callers own trust establishment. They must supply:

1. An authoritative registry loaded through the existing `scripts/pool_store.py` workflow
   from independently reviewed allocation-time identity/pool records. Preserve covered SEC
   outputs, reviewed aliases, golden exclusions and recorded conflicts. Do not reconstruct it
   from label rows, present-day hashes or a row's claimed pool.
2. A complete relevant TRAIN source metadata roster, including every recorded move target.
   Select eligible source ids from metadata using `fra_core.split.development_documents`
   before opening any example content. Do not load held-out labels to discover their split.
   The roster may contain train-holdout metadata; example content from those sources is refused
   unless an existing valid recorded move puts the issuer in fit.
3. Parsed `Move` and `Look` records from the existing holdout-record reader. Supply them
   explicitly even when empty; this API validates them and never writes them. Missing move
   targets, invalid chronology and unsupported identity combinations fail in the core contract.
4. An independently established allowlist of exact approved example hashes in `PartitionConfig`.
   `example.content_sha256()` binds every field, including target, source/config hashes,
   locator, role and verification evidence. Populate this list only after independent review
   of the original source, mapping and evidence; hashing unreviewed rows is not approval.
   Preserve the review archive privately with the dataset. Hashes establish integrity relative
   to that trusted archive, not reviewer authenticity or cryptographic signatures.
5. The authoritative taxonomy, loaded with `fra_core.taxonomy.load_taxonomy`.

The API checks each roster identity/pool by registering it into a private clone of the supplied
registry and requiring the public metadata snapshot to remain unchanged. This rejects new
allocations and unrecorded aliases instead of invoking source-dependent allocation defaults.
The caller's registry stays unchanged. Example source records must exactly match the roster.
Only TRAIN is admitted: dev, model_test and blind metadata fail even with no examples.

## Current identity boundary

Partitioning requires reviewed CIK-backed identities. Numeric CIK equivalence and explicitly
recorded CIK aliases are handled by `fra_core.pools`; the salted train subdivision and moves
are handled by `fra_core.split`. No salt, percentage, normalization or override algorithm is
copied here. All years, languages and sources of a CIK share one part.

The public registry contract does not expose a stable split key for name-only alias groups.
This API refuses name-only identities, including ordinary names, rather than assign different
aliases independently. Consequently it cannot yet partition most regional corpus sources.
Reviewed ambiguous names with different CIKs can be valid in the pool registry, but the current
split contract refuses those names keyed both ways. That failure remains visible here.
A shared core boundary and coordinated metadata/split review are separate tasks; this module
does not guess CIKs, alter names, drop conflicts or patch core to admit them.

## Selection and reproducibility

Every original example is checked for roster membership, allowed part, independent verification,
approval and taxonomy validity before deduplication or capping. A forbidden duplicate or a row
beyond a cap still fails the whole call. Duplicate example ids with conflicting content fail;
exact duplicate records collapse. Different example ids retain source and verification
distinctions. This is identity deduplication, not label/parent semantic deduplication.

An optional positive `max_per_issuer` keeps examples in sorted example-id order per issuer,
across its sources/languages/years. There is no random sampling or implicit seed. This cap
does not guarantee balanced strata or long-tail coverage; select and review those policies
before building a real dataset. D18 weighting and semantic deduplication remain future work.

With the registry and configuration unchanged, permuting examples or the source roster preserves
the output and manifest when the supplied moves and looks remain in the same order. Example and
source-roster lists are fingerprinted independent of their supplied order; move and look lists
are fingerprinted in supplied order because their chronology can affect authorization. The
manifest fingerprints all original examples (including duplicate occurrences), the source
roster, registry snapshot, moves/looks, taxonomy, approvals/cap configuration and both output
parts. Output order is by example id.
Source/verification distinctions stay in the returned examples and affect their hashes.
The manifest does not certify completeness of supplied records, source bytes, annotation
truth or environment/model reproducibility. Actual runs also need software/environment,
tokenizer, model/adapter versions and seeds recorded in their run artifacts.

## Before building datasets or training

Review the SEC crosswalk, source coverage and independent bilingual annotations. Resolve the
name-only split boundary and record extraction-dependent validation uses. Define semantic
deduplication, period weighting, long-tail coverage and source/language reporting. Audit lengths
with the pinned tokenizer/chat template, compare the base model and non-model baseline, then
authorize a resource-checked training run separately.

This API creates no training files, holdout dataset or scoring command; the separate
[dataset builder](normalization-dataset-builder.md) renders its output but has run on no real data. Any later provisional
Gate C judgment must follow D16's logged train-holdout protocol and source-specific sufficiency
rules. D13's owner-run model_test checkpoint and D17's first blind run remain at the end.
Synthetic checks support none of those measured claims. Export needs the conditions in
[14-run-profiles.md](../blueprint/14-run-profiles.md) and
[01-constraints.md](../blueprint/01-constraints.md), and profile parity remains Gate D work.
