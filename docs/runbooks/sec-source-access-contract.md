# SEC source access contract

`training/sec_source_access.py` is a synthetic, metadata-first boundary for future
normalization readers. It certifies access eligibility against injected authority. It does
not certify annotation truth, historical coverage, dataset quality or model readiness. It
does not allocate pools, write source files, change moves or spend scoring looks.

The governing rules are Week 3 in [the revised plan](../blueprint/08-revised-plan.md),
sections 2.3 and 2.4 of [the execution phases](../blueprint/04-execution-phases.md),
[D13, D16, D17 and D18](../blueprint/06-decisions-and-risks.md), and the
[corpus model-data rules](../../eval/corpus/README.md#model-data). This boundary returns
only train fit and validation payloads. It does not provide holdout scoring access or
checkpoint access to `model_test` or `blind`; `dev` is also ineligible here. Period kind
remains source metadata, not a separate pool or an inferred column duration.

## Typed boundary

All models are strict, frozen and reject extra fields. Nested instances are revalidated at
entry, including instances made through unchecked model-copy methods. Index and manifest
schema versions are the exact integer `1`. Hashes reuse `dataset_contracts.Sha256`.

| Type | Meaning |
| --- | --- |
| `SecFiling` | A reused `SourceRecord` and an opaque, stable `raw_locator`. `source_id` is the filing id; different accessions/years are separate filings even when they share an issuer and period. Only SEC sources with a reviewed CIK are admitted. |
| `SecIndex` | Archive id, archive source hash, preparation configuration hash and the complete metadata roster. It contains no labels. Each filing's source/configuration hashes must match the index. |
| `PayloadArtifact` | Unique artifact id, filing id, canonical relative POSIX payload path, payload hash and declared row count. One artifact describes exactly one filing. There is no row-declared pool or cached part field. |
| `SourceManifest` | Complete-artifact state, writer version, archive/configuration/index/registry/split hashes and the exact artifact roster. Only `state="complete"` is admitted. |
| `AccessContext` | Independently established expected source/configuration/index/manifest/registry/split hashes. This is injected authority, not evidence inferred from the input being checked. |
| `AuthorizedPayload` | Filing metadata, artifact description, recomputed fit/validation part and the bound access context. It is call-specific evidence, not a bearer capability. |
| `VerifiedPayload` | An authorized descriptor and hash-checked opaque bytes. They have not been decoded or approved as supervision. |

`SourceRecord.source_sha256` denotes the raw archive containing the filing in this contract;
`PayloadArtifact.payload_sha256` separately identifies the isolated filing payload. The raw
locator is unique within that archive. Its interpretation, including any member/ordinal
syntax, belongs to a future reviewed source-preparation contract. No hierarchy, namespace,
taxonomy version, label meaning or column duration is inferred from it.

`authorize_payloads(artifact_ids, *, index, manifest, registry, context, moves, looks)`
returns descriptors only. `iter_payloads` accepts the same arguments plus an injected
`opener: Callable[[AuthorizedPayload], bytes]` and returns an iterator of verified payloads.
`read_payloads` takes the same arguments and returns the whole iterator as a tuple. Both
perform fresh metadata authorization before invoking the opener. Neither accepts
caller-created descriptors as authority to open a file.

## Trust prerequisites

The caller must establish the reviewed registry, complete index roster, exact configuration,
current ordered Move/Look records and complete-artifact commit evidence independently.
The expected manifest digest must refer to an immutable, committed manifest whose artifacts
were completed successfully. A `complete` string or a digest computed from an untrusted
manifest does not establish that fact. The registry's historical output coverage list alone
also does not establish successful artifact completion.

`content_sha256()`, `registry_sha256()` and `split_records_sha256()` provide reproducible
fingerprints, not review approval. Canonical JSON uses sorted keys, compact separators and
UTF-8. Filing and artifact tuples are sorted by their respective ids for fingerprints and
duplicate occurrences are retained until validation rejects them. Move/Look order is
preserved because the exact recorded chronology matters. Returned descriptors are sorted
by artifact id; selection order does not change the result.

A caller cannot use this API to prove roster completeness from an incomplete index itself.
That completeness is part of the independent review bound by `AccessContext.index_sha256`.
The implementation supports one archive per index. All relevant train move targets must be
present in its metadata roster even when they are not selected. Missing targets fail through
the shared core rule. Cross-archive or cross-source move-target metadata needs a separately
reviewed integration boundary; it must not be represented by fabricated archive hashes.

## Checks before any payload opens

1. Revalidate typed inputs and the injected Move/Look records. Apply the core chronology
   checks before relying on a split fingerprint. Besides the checks on each move, the core
   rejects a move sequence whose dates or recorded look counts fall from one move to the
   next, and a move dated later than the look that followed it. A move whose key is not the
   normalized name of its issuer is refused, so it cannot release another issuer.
2. Match actual index, manifest, registry and split fingerprints to the expected context;
   match the source/configuration hashes across the index, manifest and filing records.
3. Validate every filing identity and pool, including unselected nontrain and holdout
   metadata. Public registry validation runs on disposable probes reconstructed from the
   authoritative snapshot. One probe is built per call and checked against every filing;
   a filing that would add an assignment or alias is rejected, so the probe never differs
   from the snapshot. The injected registry is never changed. Conflicts fail through the core registry contract.
4. Recompute eligible ids with `fra_core.split.development_documents` over the entire
   train metadata roster. Validate all recorded move targets with the shared rules.
   Splitting uses the CIK contract; a name hash or cached part cannot replace it.
5. Require unique filing ids and raw locators, unique artifact ids/filing ids/paths, and
   exact index-to-manifest filing coverage. Missing or unexpected artifacts fail even for
   an empty selection.
6. Reject unknown or duplicate selections and every selected nontrain or unmoved holdout
   filing. Validate all selections before returning descriptors or opening any bytes.
   There is no deduplication, filtering or cap that can hide a forbidden selection.

Known aliases and filing years sharing a CIK receive the same core train part. An unrecorded
alias cannot be inferred from a known CIK. Name-only migration is excluded. The public split
boundary also rejects one normalized train name attached to different CIK keys, even when
the registry declares that name ambiguous. This contract preserves that refusal; resolving
it requires a separate shared-boundary review, not a local replacement split algorithm.

## Retrieval and deferred checks

Paths consist of slash-separated ASCII components beginning with a letter or digit and
continuing with letters, digits, dots, underscores or hyphens. Absolute paths, dot/dot-dot
components, empty components, backslashes, drive/URI syntax, percent escapes and control
characters are rejected without normalization. This is a POSIX syntax check only.

After all metadata and selections pass, the opener receives only eligible descriptors.
The returned value must be bytes with exactly the declared payload SHA-256. Missing files,
permission errors and other `OSError` failures raise `SourceAccessError` with the artifact id
and original cause. Other opener exceptions propagate loudly. A hash mismatch also raises.
`read_payloads` returns only after every selected retrieval succeeds; it never returns a
trusted partial tuple after a later failure. It holds every selected payload in memory until
it returns. The manifest records no payload size, so no total byte limit can be checked
before opening. `iter_payloads` instead opens and hashes one payload per step and holds no
earlier one, so memory is bounded by the largest single payload. Its authorization still
happens before it returns, but a retrieval or hash failure surfaces at its own step, after
earlier payloads were yielded; a caller that needs all-or-nothing treats any failure as
invalidating what it consumed. Neither can undo an opener's external side effects.

There is no decoding here. The future decoder must validate the payload schema, every row's
filing/source identity and locator, declared row count, and absence of mixed-source content
before using any row as supervision. Hash agreement alone establishes none of those semantic
properties. Exact example approval and reviewed target/abstention evidence remain the
responsibility of the existing dataset contract and future normalization adapter.

Production openers must enforce a trusted root, regular-file and symlink policy, immutable
artifact ownership, and resistance to path substitution and symlink races. Pure path syntax
cannot enforce confinement or prevent a time-of-check/time-of-use race. Production runtime
must also pin the authority/artifact snapshot and enforce revocation or changes during
retrieval. This module implements no filesystem opener, locking, decompression or network
transport, and is not a sandbox for an arbitrary injected callable.

## Archive bootstrap and historical outputs

A quarterly archive's presentation member contains mixed issuers. Reading and filtering
those labels, or opening an existing mixed quarterly gzip, is not authorized by this
contract. Metadata indexing does not solve the bootstrap problem: a future source-preparation
task needs explicit archive-access authorization and a reviewed routing boundary. If no
forbidden label bytes may be read, a safely selective upstream source is required.

Historical mixed outputs remain quarantined until separately authorized reconciliation
establishes their allocation-time identities and coverage from independently retained
records. Today's names, CIK hashes, row pool claims or newly computed fingerprints cannot
recover that history. This implementation supplies no migration, downloader, source writer,
approved examples or real-data processing.

## Synthetic verification

The focused tests are `tests/training/test_sec_source_access.py`. They use only in-memory
synthetic metadata and payload bytes. Opener spies verify zero calls for forbidden mixed
selections and metadata failures, including unrecorded identities, missing move targets,
stale bindings, invalid paths and incomplete state. Retrieval regressions cover missing
bytes, hash conflicts and failure after an earlier successful retrieval, and that the
iterator opens one payload per step. Further cases cover a move that carries another
issuer's key, moves recorded out of chronological order, and a single registry probe per call.

Run from the assigned checkout with the installed offline environment:

```sh
uv run pytest tests/training/test_sec_source_access.py
uv run mypy --strict training/sec_source_access.py
make test
make lint
make typecheck
make docs-check
git diff --check
```

`make typecheck` permanently checks `training/sec_source_access.py` with strict mypy
settings. No accuracy, historical-coverage or readiness claim follows from these synthetic
checks.
