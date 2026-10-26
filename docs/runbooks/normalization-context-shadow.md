# PDF normalization context shadow inspection

`fra_ingest.normalization_context.build_normalization_request(statement, target_id)` builds
a typed, amount-free request from an existing PDF `Statement`. The optional production mapping
stage now uses its literal label and statement fields, while keeping context and audit out of
the trained model input. The historical shadow measurements below remain diagnostics, not
measured production gains. A fresh request demonstrates context construction, not improved
normalization.

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
only when it matches that supported baseline. These began as shadow callables without production routing. The optional integration below
now invokes the same unchanged evidence gate; its diagnostic measurements remain historical. Prefix-only matches remain unresolved; no aliases
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


## Optional production mapping

`FRA_NORMALIZATION_ENABLED` defaults to `0`; only `0` and `1` are accepted. The worker records
mapping as skipped when disabled or declined. Enabled jobs validate the server lazily inside
the timed mapping stage, after baseline structure and before metrics. A connection error,
timeout, redirect, wrong model, prompt-pin mismatch, or invalid response envelope/identity
fails the job at `mapping`; there is no lexicon fallback for an enabled failure. An
out-of-catalog or otherwise invalid answer is audited as a rejected proposal and does not fail
the document.

`fra_core.normalization_prompt` now owns the existing training/scoring prompt and label-only
renderer. The system SHA-256 remains
`bba5f9cdf6ea3e553c9b899a0d4419128e84f6255f73870dba2090ded1bc0606`.
The user message is compact, sorted Unicode JSON with only `label` and `statement`. Literal
whitespace and printed digits in labels remain unchanged. Context, audit, amounts, raw value
text, coordinates, check operands and proposed/expected answers are never model messages.
Existing training rows and dataset tests are unchanged.

Only unmapped balance/income rows with value cells and a nonblank caption are proposed.
Headings and blank-caption totals have no usable label-only model input and retain their
baseline mapping state. Existing lexicon and anchor mappings are never replaced or tagged as
model output. A semantic null object answer records `model_abstained`; unsupported proposals
retain `model_rejected:<reason-code>` as mapping evidence. Invalid answers are audited as
rejected proposals with `model_rejected:invalid_model_response`; they are neither mapped nor
counted as abstentions.
Accepted output carries `mapping_source: model`, `model_proposed` evidence and a visible
"Model proposed; evidence checked" origin on the results page. This is categorical support,
not a claim that the number, entity, units or denominator has been verified.

The existing `assess_proposal` gate is unchanged: support must match the recomputed
independent deterministic mapper. Truly unmapped rows under the same checks may therefore
receive **zero accepted fills**. The source-positive synthetic acceptance test proves the
reachable integration path, not a coverage improvement. Subtotal, identity, EPS and tie
policies, critical findings and review thresholds are unchanged. After any acceptance the
same numerical checkers run again. Structure version 54 retains EPS source notes and statement
headers for that purpose, including after a cache round trip. Earlier failed/skipped checks
and review holds survive; checks are merged by ID without duplicate-count inflation.

Every enabled invocation still runs the model when baseline structure was cached. It writes
separate files under `<artifact-root>/<document-sha256>/normalization/`: `statements.json`,
`checks.json` and `audit.json`. None is a deterministic structure-cache entry. The audit holds
model configuration, prompt hash, per-row response, prior mapping state, original request audit
and evidence decision. Join rows by document SHA-256, statement ID and line-item ID, retaining
statement source order and page/table/row lineage. The existing mapping evaluator matches
expected rows by their reported figures and order among repeated figures; that comparison
runs after inference. Expected values/labels are never supplied to the runtime. A missing or
ambiguous match remains unverified; it is not a zero-error score.

### Serve and enable

The single model pin is `configs/normalization.toml`: the owner-provided fused 1000-update,
47-ID LoRA file `var/models/qwen2.5-7b-normalization-q4_k_m.gguf`, 4,683,073,408 bytes, SHA-256
`1b87968d3116a9fc228d52373e7783f784008a44d214c10479dab291fb2404b3`.
This records the owner's artifact identity, not a new export or accuracy measurement.
The worker hashes local bytes and requires `/props.model_path` to equal the complete absolute
realpath (resolving model-directory symlinks). Both profiles set that same absolute path as the
server alias; response model identity must match. The HTTP client ignores proxies and refuses
redirects. Port configuration comes
only from `FRA_LLM_PORT` in `.env`/the launch environment. The explicit timeout is in the pin
configuration; requests use temperature zero, at most 64 output tokens and `cache_prompt:false`.

From this task checkout, in separate terminals:

```sh
make llm
FRA_NORMALIZATION_ENABLED=1 make worker
make dev
```

`make llm` uses the installed `llama-server`, loopback only and `-ngl 99`. Both servers use
an explicit 2048-token context, one slot and zero prompt-cache RAM (`-c 2048 -np 1 --cache-ram 0`).
These bounds are configuration, not a measured peak-memory guarantee. The optional Docker
CPU service uses `-ngl 0`, the previously recorded R19 full-image digest, readonly model mounts
and a health check. It publishes no host port. The worker connects to `llm` on `FRA_LLM_PORT`;
both containers see the model at `/app/var/models/...`, so basename matching is unnecessary.
Start the sidecar and wait for health before enabling a worker with queued jobs; the runtime
does not retry startup failures. The CPU image's server entrypoint and health command still
require a live coordinator check. Native and Compose share the stdlib configuration/command
helper; the CPU image needs Python 3.11 or later for TOML parsing, without installing the
application or Pydantic. Relative pins resolve from the profile root; absolute pins remain
absolute and must be mounted at the same path in worker and server.

```sh
docker compose --profile llm up -d --wait llm
FRA_NORMALIZATION_ENABLED=1 docker compose up -d --build --wait api worker
```

Ordinary `make docker-up` and `make docker-health` keep the disabled default and do not need a
model server. No weights or additional packages are fetched by the application.

Native `make smoke PDF=/absolute/path/to/dev.pdf` copies all required configurations into
isolated state, including the normalization pin, and links its model directory to this
checkout's directory. No weights are copied. OFF requires no model file. For ON, first restart
`make llm` from this checkout with the realpath command above, then run
`FRA_NORMALIZATION_ENABLED=1 make smoke PDF=/absolute/path/to/dev.pdf`. Make forwards the
checkout's `.env` port. Direct `bash scripts/smoke_job.sh` calls must supply `FRA_LLM_PORT`
in the environment because the script does not load `.env`. Both paths resolve to the same file; size,
SHA-256, `/props` and response identities remain mandatory. Exit 2 means `needs_review`.

### Paired English dev evaluation

`eval-extraction`, `eval-structure`, `eval-mapping` and `eval-gates` use the worker's same
post-structure mapping path. Enabled runs read the separate normalization checks; disabled runs read baseline
checks. This goes beyond a runtime smoke call. `ENGLISH_ONLY=1` selects the six English entries
in the golden manifest, before opening PDFs or expected examples. It has no protected-pool
argument. Keep baseline reports before the enabled commands overwrite the normal `var/eval`
report names.

```sh
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=0 make eval-structure ENGLISH_ONLY=1
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=0 make eval-extraction ENGLISH_ONLY=1
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=0 make eval-mapping ENGLISH_ONLY=1
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=0 make eval-gates ENGLISH_ONLY=1
# Preserve the four baseline reports, then start make llm in another terminal.
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=1 make eval-structure ENGLISH_ONLY=1
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=1 make eval-extraction ENGLISH_ONLY=1
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=1 make eval-mapping ENGLISH_ONLY=1
FRA_PROFILE=native FRA_NORMALIZATION_ENABLED=1 make eval-gates ENGLISH_ONLY=1
```

An enabled runtime transport, envelope, or identity rejection fails that document with reason
`model_runtime` and the original detail, then evaluation continues to the next document.
Invalid model answers are retained as rejected proposals in the mapping audit and do not fail
the document. Reports retain failed document IDs
and exit nonzero; failed mappings contribute no baseline statements or checks. Extraction
reports separate failed and scored document counts per stratum. OCR engine failures retain
their existing abort rule; repeated model failures do not trigger the OCR engine guard.
Declined documents produce no mapping/check artifacts, and gate evaluation does not read
stale checks for them. No retry, schema coercion or prompt/gate relaxation hides a failure.

The coordinator owns the live six-document comparison, full `make test` and independent
whole-diff review. Unit tests do not establish model precision or Gate A/B improvement. No
training, export, protected-pool scoring or new checkpoint is part of this integration.
