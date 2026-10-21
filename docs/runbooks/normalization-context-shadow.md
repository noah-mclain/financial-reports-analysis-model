# PDF normalization context shadow inspection

`fra_ingest.normalization_context.build_normalization_request(statement, target_id)` builds
a typed, amount-free request from an existing PDF `Statement`. This callable is for synthetic
tests and shadow inspection only. Production mapping, prompts, training rows and model calls
are unchanged. A fresh request demonstrates context construction, not improved normalization.

## Contract and evidence policy

The core contract lives in `fra_core.schemas.normalization`. Use
`request.model_payload_json()` to serialize the model-visible `statement`, literal `label`
and `context`. Its strict frozen models reject extra fields. Printed strings are preserved,
including whitespace; blank labels and labels longer than 512 characters fail validation
rather than being rewritten or truncated. Context contains at most one heading, one printed
subtotal parent and six related labels.

`context.status` is `inferred`, `unavailable` or `conflicting`. There is no confirmed state:
existing parent links are inferred structure, even when a heading's text is printed.
Unavailable and conflicting states withhold every contextual label.

- A heading must be an existing ancestor and the nearest preceding nonempty row without
  value cells. Proximity alone never establishes that relationship.
- A nonempty subtotal ancestor may supply a parent label only within the target's heading
  run. An ancestor crossing a heading boundary makes context conflicting. Outer ancestors
  above an applicable heading supply no additional labels.
- Related rows share the target's immediate parent, stay between the surrounding headings,
  carry cells and are not subtotals. The nearest six eligible rows are selected, with ties
  resolved by source order, then serialized in source order. Distance assigns no category.
- A blank closing subtotal is never a printed parent caption. Without a retained heading
  ancestor it yields unavailable context. The adapter does not reconstruct a heading from
  the subtotal's canonical mapping, checked sums, or preceding text.

This policy deliberately abstains when checked subtotals have replaced a heading parent link,
as in the existing blank-total lease fixture. Run-in headings and higher-level heading chains
are not reconstructed. There is no new hierarchy, alias engine or mapper call.

`request.audit` holds document SHA-256, statement and row IDs, statement source-order offsets,
and deduplicated page/table/row references retained by value cells. Audit is excluded even
from ordinary request serialization; inspect it explicitly with `request.audit.model_dump()`.
These references identify value rows, not label regions. Heading rows often have only a
statement-order reference. Every audit row records `label_region: "unavailable"`; no label
bbox, value bbox substituted for a label bbox, or invented table reference is emitted.

The adapter never reads canonical mappings, mapping evidence, note references, roles or check
operands. Amount cells are used only for structural presence and audit row identities; their
values and raw text are excluded. It does not take SEC tags, gold answers, hidden filer roles
or page text as inputs. Printed digits inside a literal label remain printed text.
Duplicate IDs, missing targets/parents and cycles in any graph component fail loudly.
Traversal is iterative and bounded by the supplied statement; the statement is not mutated.

## Measured synthetic example

Measured locally using the new tests' `section()` fixture, which runs existing
`infer_hierarchy` on a printed heading, a lease row and a loan row. No private filing or
held-out example was used. The legacy renderer is `training.dataset_builder.render_input`.

Legacy input, identical under both section fixtures (51 UTF-8 bytes):

```json
{"label":"Lease liabilities","statement":"balance"}
```

New model-visible input under the current-liabilities fixture (149 UTF-8 bytes):

```json
{"statement":"balance","label":"Lease liabilities","context":{"status":"inferred","heading":"Current liabilities","parent":null,"related":["Loans"]}}
```

Changing only the printed fixture heading to `Non-current liabilities` changes the new
heading context while leaving the target label and legacy payload identical. These byte
counts describe serialized inputs, not tokenizer lengths or measured model performance.

Negative controls exercised by the focused tests:

| Synthetic input | Result |
| --- | --- |
| Generic EPS under `Earnings per share`, with `Basic` and `Diluted` peers | Both peer labels in source order; status inferred; no share-kind field or confirmed kind |
| Nearby heading but no target parent link | Unavailable; all context labels withheld |
| Existing lease fixture with a blank closing subtotal parent | Unavailable; no inferred printed parent or invented heading |
| Printed nonempty closing subtotal in the same run | Inferred parent and same-parent peers; opposing section excluded |
| Parent heading belongs to an earlier section | Conflicting; all context labels withheld |
| Duplicate IDs, missing references/target, self-cycle or disconnected cycle | Named `ValueError`; no recursive traversal |
| Values, mapping answers, tags and role sentinels on the source fixture | Excluded from payload; audit excluded; original Statement unchanged |

## Reproduce and interpret checks

Run from the assigned checkout, with the installed offline environment:

```sh
export UV_OFFLINE=1 UV_NO_SYNC=1
export UV_CACHE_DIR=/private/tmp/normalization-padding-uv-cache
uv run pytest packages/core/tests/test_normalization_schema.py packages/ingest/tests/test_normalization_context.py
uv run pytest packages/ingest/tests/test_label_mapping.py packages/ingest/tests/test_hierarchy.py packages/ingest/tests/test_sum_hierarchy.py packages/ingest/tests/test_eps_checks.py packages/ingest/tests/test_structure.py
make test
make lint
make typecheck
make docs-check
```

Private execution evidence is retained in ignored `var/context-shadow-proof/`: the initial
regressions failed collection with exit 2 because the new contract did not exist
(`regressions-before.log`), then the first focused run passed 19 tests with exit 0
(`focused.log`); the final focused run passed 21 tests with exit 0 (`focused-final.log`).
Existing ingest regressions passed 211 tests with exit 0
(`existing-ingest.log`). `synthetic-comparison.json` records the measured payload comparison
and negative controls. `make lint`, `make typecheck` and `make docs-check` exited 0
(`make-lint.log`, `make-typecheck.log`, `make-docs-check.log`).
`make test` exited 2: pytest reported 5,173 passed, 11 failed, 27 skipped and 17 deselected
(`make-test.log`). Every failure was an existing API healthcheck socket test: binding
`127.0.0.1` raised `PermissionError: [Errno 1] Operation not permitted` at
`apps/api/tests/test_healthcheck.py:32` or `:43`. The coordinator then reran the full gate
outside the native sandbox: `make test` exited 0 with 5,186 passed, 27 skipped and 17 deselected
(`coordinator/make-test.log`). The coordinator also verified `make lint`, `make typecheck`
and `make docs-check`, each with exit 0; commands' exit codes are retained in
`coordinator/exits.tsv`. `make test-sandboxed` is supplemental and did not replace the full
gate; it exited 0 with 5,175 passed, 27 skipped and 28 deselected
(`make-test-sandboxed.log`), including the final focused additions.

No dataset was rebuilt, downloaded, trained or evaluated against real documents. SEC
presentation order does not provide equivalent PDF parent/heading evidence; target tags must
not manufacture that context. Training/inference parity and real-data quality gains remain
unmeasured. D13/D16/D17 checkpoint, issuer-split and holdout rules remain unchanged; neither
`model_test` nor `blind` was opened.

## Paired synthetic inference checkpoint

A subsequent inference-only diagnostic used the pinned local Qwen2.5-7B 4-bit base model,
without an adapter. Twelve synthetic cases produced 24 paired rows and 16 unique requests;
identical requests reused completed responses, not a conversation or KV cache. Both arms used
the same experimental ambiguity policy, temperature zero, seed 17 and a 64-token output limit.
Only the context field changed. This was not a production prompt change or real-data evaluation.

The six positive cases paired current/non-current leases, basic/diluted EPS and
current/non-current other assets. The six controls covered absent/conflicting context, a blank
subtotal parent, mixed EPS cues and two uniquely named items. Expected targets and thresholds
were frozen before inference. Missing evidence was not a license to guess: ambiguous label-only
inputs and four controls required abstention.

| Context-arm check | Required | Observed |
| --- | --- | --- |
| Correct context-resolvable targets | 6/6 | 5/6 |
| Correct controls | 6/6 | 2/6 |
| Valid response schema | 12/12 | 11/12 |

The strict acceptance criterion failed. Context corrected the non-current lease and diluted-EPS
outputs relative to the label-only defaults. Current other assets returned bare JSON `null`,
which is invalid for the required object schema and was not counted as abstention. All four
required-abstention controls still returned non-null IDs; only revenue and net PPE controls
passed in both arms. The label-only arm returned valid schemas for all 12 rows but guessed IDs
on all ten ambiguous rows. This label-only model arm is not the deterministic production mapper.

The child completed in 36.73 seconds with a peak MLX allocation of 4,830,573,416 bytes, below the
6,000,000,000-byte cap. Fresh available memory was 12,562,779,340 bytes and no repository services
were running. No inference retry or prompt tuning followed the results. All 24 responses were
reparsed and recounted, and all 46 frozen source pins remained unchanged.

The frozen plan SHA-256 is
`6bcfec429f45e28a3dbd29039f9498fe77e45cb9f4e60638d9e9f91463a49550`.
Private run artifacts retain the prompts, outputs, model identity and audit. They are not
committed. This checkpoint supports further abstention/output-contract work, not a SEC rebuild,
model promotion or a claim of reliable document-grounded normalization.

## Evidence-gated decisions

`fra_ingest.normalization_decision.baseline_decision(statement, target_id, index, checks)`
recomputes the existing deterministic mapper after discarding incoming mapping conclusions.
`assess_proposal(statement, target_id, taxonomy, checks, response)` accepts a non-null proposal
only when it matches that supported baseline. These are shadow callables with no production
routing, prompt change or model invocation. Prefix-only matches remain unresolved; no aliases
were added to improve coverage.

Decisions distinguish `supported`, `unresolved`, `rejected`, `model_abstention` and `invalid`.
Only `supported` carries an accepted canonical ID. A valid object containing a null ID records
model abstention, not proof that no taxonomy concept exists. Bare null, duplicate keys, missing
or extra fields, wrong-statement IDs and unknown IDs remain invalid. Training re-exports the
shared core answer contract and delegates its existing parser API; the shipped schema and
prompt retain their prechange SHA-256 digests in regression tests. No dataset rebuild follows.

The boundary validates every row-graph component before mapper traversal. Only subtotal and
balance-identity checks can anchor mappings. Their statement, period and source-row references
must belong to this statement; EPS and cross-statement note references are ignored for mapping.
Results must come from trusted deterministic upstream checks. The gate checks their scope and
does not recalculate arithmetic or authenticate their producer.

The target must have a reported value and a valid printed label. Statement flags, unresolved
period conflicts, critical cell flags and unrecognized non-provenance cell flags conservatively
hold the shadow decision. The exact `per_share` cell annotation is treated as unit metadata and
does not hold categorical eligibility; similarly named or unknown flags remain held. A supplied
skipped anchor conservatively holds the decision until its scope is settled, even when another
period passed. Categorical support is neither numerical verification nor primary-statement
eligibility. For EPS, it does not verify the amount, denominator, entity or unit.
Entity/consolidation and EPS denominator verification remain the existing upstream checker's
responsibility; a categorical EPS alias cannot settle those questions.

The decision audit records every inspected row's observed label, inferred parent ID, statement
order and actual retained value-row references. Each label region remains `unavailable`.
Supplied anchor check IDs name inputs, not proof that every check caused resolution. Decisions
and audit observations are separate from `NormalizationPayload`; no source references, cell
values or proposed/gold answers are added to model input. Stable unresolved mapper reasons and
needed-evidence descriptions support collecting real source facts next.

Tests use construction fixtures, including the four intentionally underdetermined controls.
They establish boundary behavior, not real-document accuracy, PDF label layout, EPS arithmetic
or a model-quality score. Explicit source-positive captions are separate evidence and do not
repair those negative controls. Independent full-diff review remains a separate gate.
