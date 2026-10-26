# The checkpoint

The owner-run, one-time pipeline look at English `model_test` and the non-reserved English
`blind` documents (D13, D17, D22 and D23). The first `model_test` evaluation freezes both
pools; it cannot be repeated. D23 authorizes proceeding and reporting the evidence honestly,
including gates that cannot be judged. It does not waive the checks below or the separate Gate D
precondition. No checkpoint is run as part of this runbook edit.

## What the checkpoint can establish

- **Gate C is not measured on `model_test`.** `training/sec_examples.py` resolves examples only
  from approved artifacts and `training/dataset_contracts.py` rejects any source whose pool is
  not `train` (`partition_examples`, pool check). There is no approved `model_test` example path.
  Adding one requires new reviewed artifacts and changes to the pool barriers; neither is done
  before this one-time look. Report Gate C as **not measured on `model_test`**. The fixed
  validation panels are diagnostics, not Gate C, and do not establish the gate margin or critical
  item bar.
- **Gates A, B and E are not judged on these pools.** Reviewed expected answers exist only for
  the `dev` golden documents. Report pipeline observations available without references:
  completion, held for review, declined, failed, identity outcomes, critical items mapped or
  flagged, runtime and memory. State that these observations do not establish Gate A/B accuracy,
  Gate A accepted-cell share, or Gate E correctness / zero confidently wrong figures. Never say
  A, B or E passed or failed based on these observations. Gate D is reported only from its own
  recorded portability evidence.
- **D22's scope is fixed.** Run 36 English `model_test` documents (25 issuers; 13 annual, 23
  interim). Run 29 English `blind` documents (20 issuers; 14 annual, 15 interim), excluding 8
  reserved issuers and their 10 English documents. The reservation is for the rehearsal and is
  not run at checkpoint. The counts and rule are in [the corpus policy](../../eval/corpus/README.md)
  and [`rehearsal_reserve.yaml`](../../eval/corpus/rehearsal_reserve.yaml).
- **The checkpoint candidate is fixed:** the final update-1,000 adapter from the 25 October fit.
  The decay re-fit is outside this checkpoint and is not a precondition. The validation-panel
  diagnostic for this candidate did not meet the Gate C margin (lower interval ends +3.2 and
  +1.8 points against 5; challenge critical errors 6 of 95); do not present that as a
  `model_test` score or a Gate C result.

## Before step 1: every item must hold

1. `make test`, `make lint`, `make typecheck`, `make docs-check` and `make corpus-check` pass on
   the commit to be used. Record `git rev-parse HEAD`; the tracked tree is clean.
2. Every PR changing code under test is merged, and the checkout contains the checkpoint tools:
   `eval/harness/checkpoint_records.py`, `eval/harness/checkpoint_run.py`, and
   `eval/corpus/rehearsal_reserve.yaml`.
3. **Gate D precondition:** before step 1, the plan records (a) successful Docker
   upload-to-summary results for both a digital and a scanned English document, and (b) eval
   numbers for both profiles. Each part not met needs a specific owner exception in the decision
   log, one per part. The plan's 26 October Gate D evidence shows a summary for the digital
   document (Almarai) and none for the four scanned or partly scanned documents, which were held
   for review, and records no Docker eval numbers. Part (a) is therefore met for the digital
   document only and part (b) is unmet. The scanned half of (a) is satisfied when a scanned
   English document completes with a summary and that result is recorded in the plan, with no
   exception needed for it; otherwise it needs an exception. D23 supplies neither exception. Do
   not start step 1 until every part is either recorded as met in the plan or covered by its
   exception.
4. Before any checkpoint result exists, fix the candidate in writing: the update-1,000 adapter
   path, `shasum -a 256` for `adapters.safetensors` and `adapter_config.json`, and the base-model
   manifest hash. Use this exact candidate for the checkpoint record. If a different candidate
   is later adopted, amend this precondition and the separate export instructions below before
   starting the checkpoint; do not change candidates after seeing checkpoint results.
5. Nobody has opened, read, run or tuned against a `model_test` document, a `blind` document, or
   a reserved issuer's document. If that boundary was crossed, stop and record the event; do not
   try to repair it with another run.
6. The machine is idle and on AC power: `pgrep -fl "normalization_(fit|mlx)|mlx_lm"` returns no
   process; available memory is at least **6 GB** and `memory_pressure` free percentage is at
   least **25%**; the kernel memory pressure level (`sysctl kern.memorystatus_vm_pressure_level`) reads 1 or 2, the levels the export stop rule in `01-constraints.md` 1.4 treats as safe; the browser and other
   heavy apps are closed. Keep both memory checks: the 6 GB absolute minimum is an explicit
   checkpoint safety margin, while the percentage check protects against lower-memory states.
   The inspected checkpoint dependencies do not expose a 5 GB native heavy-stage floor, so this
   runbook does not attribute that figure to code.
7. The owner confirms this is the one-time run. The row in
   `eval/corpus/checkpoint_log.tsv` is the commitment; do not start without it.

## Step 1: `model_test` pipeline observations

From the assigned clean repository checkout, run:

```sh
caffeinate -i env PYTHONPATH=eval uv run python -m harness.checkpoint_run model_test --checkpoint
```

The guard requires `--checkpoint`, refuses a second start, logs the row before opening a
document, prints the selected count and holdout-based runtime projection, runs each English
document in its own child process, and records failures by class. Continue past a failed
document; do not change settings and rerun after seeing a result. An interrupted run may use
`--continue` only for the same commit, document list and configuration; it does not retry saved
failures. Outputs are under `var/checkpoint/model_test/`.

## Step 2: non-reserved `blind` pipeline observations

Start only after step 1 has its single `model_test_pipeline` done record and unchanged result
file. The guard enforces that prerequisite.

```sh
caffeinate -i env PYTHONPATH=eval uv run python -m harness.checkpoint_run blind --checkpoint
```

The runner excludes all D22 reserved issuers. Their documents remain unopened until the
separate rehearsal ([rehearsal.md](rehearsal.md)). Outputs are under `var/checkpoint/blind/`.

## Report contract

Write [the accuracy report](../accuracy-report.md) from the recorded artifacts only. For each
pipeline pool and every printed results table, report the total and annual/interim counts side by
side. The runner summary provides `by_period` summaries for every stratum; use these alongside
the total summary. Do not derive alternate counts from protected PDFs, labels or extraction
content.

Report operational observations without converting them into correctness verdicts. State that
Gate A and B accuracy are **not measured on `model_test` or `blind`**, Gate C is **not measured
on `model_test`**, and Gate E is **not judged** because these pools have no reviewed references.
Explain their evidence limits above; do not say these gates passed or failed. Report Gate D only
with its recorded digital and scanned English results or the owner's recorded exception. Include
D22 counts, candidate identity, one adapter/one seed, the absence of Arabic gate claims, and the
fact that the validation panels are diagnostics only.
Do not pool panels, round a result up, or substitute a measure from another pool.

Record SHA-256 for **each** pool's `summary.json` and for the complete per-document result set
(a deterministic manifest of each result path and its SHA-256). Copy the exact
`var/checkpoint/` artifacts and checksum manifest to a private archive outside Git, and retain
them until the owner releases them. Keep source PDFs, extracted content, model weights and
private reports outside Git.

## Separate GGUF export (not a checkpoint step)

Export does not evaluate or touch either checkpoint pool and may happen before or after the
checkpoint. It is not a checkpoint precondition or a product claim. If attempting the real
candidate export, use only the candidate fixed in precondition 4. If adopting a different
candidate, amend both precondition 4 and this export instruction before the checkpoint run.
First complete the mandatory 7B throwaway-adapter rehearsal. Follow every condition in
[constraints 1.4](../blueprint/01-constraints.md#conditions-for-the-gguf-export),
including a fresh `df` preflight before **each** rehearsal or real export run with at least the
then-applicable estimate (currently about 45 GB free), the no-other-heavy-workload rule, staged
verification/deletion and stop rule. Record every run in [run profiles](../blueprint/14-run-profiles.md);
its 7B rehearsal has not yet been recorded there. Do not substitute
`scripts/spikes/gguf_route.sh`: it trains a toy 0.5B adapter, not this candidate.

Acceptance for the real export requires both f16 and quantized GGUF outputs to agree with MLX on
**every row of both fixed validation panels (680 rows total, no sampling)**. In addition, the
quantized output must pass document 14's preregistered task-accuracy rule against gold. Agreement
alone is not accuracy and never makes the adapter a product claim. Until the export is separately
completed and reviewed, describe the adapter only as a measured candidate; the pipeline does
not call a model today.
