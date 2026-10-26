# The checkpoint

The one-time look at `model_test` and `blind` (D13, D17;
[08-revised-plan.md](../blueprint/08-revised-plan.md), [corpus rules](../../eval/corpus/README.md)
3, 5 and 6). It freezes both pools at its first `model_test` evaluation and cannot be repeated.
The owner runs it. This runbook fixes the order, the stop rules and what the report may say, so
the result is the same whoever runs it.

What the checkpoint can and cannot show, stated before it runs:

- **Gate C is not measured on `model_test`.** The SEC example pipeline accepts only `train`
  sources at every stage: payload preparation, the reviewed and approved crosswalk artifacts, and
  the partition contract (`training/sec_examples.py`, `training/dataset_contracts.py`). Scoring
  the adapter on `model_test` needs an approved-example path for that pool, which means new
  reviewed artifacts and a change to the pool barriers. Building that in a day, around a one-time
  look, risks the barriers it depends on, so it is not done. The report says Gate C is not
  measured on `model_test`, and shows the validation-panel results of the adapter, labelled as
  diagnostics ([accuracy-report.md](../accuracy-report.md)). It does not present them as the gate.
- **Gates A, B and E accuracy cannot be measured** on `model_test` or `blind`: reference
  answers (expected and reviewed gold files) exist only for the `dev` golden documents. On these
  pools the run measures what needs no reference: completion, held for review, declined, failed,
  identity checks, critical items mapped or flagged, and time and memory against budget. The
  report says "not measured" for figure accuracy and for "no confident wrong number" on these
  pools, and does not substitute another number for them.
- **Gate E** is judged on the 29 English `blind` documents that remain after the reservation
  (D22: 8 issuers, 10 documents, chosen by a fixed hash rule and kept for the rehearsal, not
  run). `model_test` has 36 English documents. The report says so.
- **The candidate** is the final update-1,000 adapter of the 25 October fit. On the validation
  panels it did not meet the Gate C margin (lower interval ends +3.2 and +1.8 points against 5;
  challenge critical errors 6 of 95). The decay re-fit in the training plan is not part of this
  checkpoint unless it has finished and passed its preregistered rule before step 1.

## Before step 1: every item must hold

1. `make test lint typecheck docs-check` pass by exit code on the commit being used, and
   `make corpus-check` passes. Record `git rev-parse HEAD`; the tree is clean.
2. Every PR that changes code under test is merged to `main`, and the checkout is that `main`.
3. The checkpoint tooling is merged: `eval/harness/checkpoint_records.py`,
   `eval/harness/checkpoint_run.py` and `eval/corpus/rehearsal_reserve.yaml`.
4. The candidate is fixed in writing before any result exists: adapter path, `shasum -a 256` of
   `adapters.safetensors` and `adapter_config.json`, and the base model manifest hash.
5. Nobody has opened, read, run or tuned against a `model_test` document, a `blind` document or
   a reserved issuer's document. If anyone has, stop: that is a rule 3 or 5 event to record, not to
   fix by running on.
6. The machine is idle and on AC power: `pgrep -fl "normalization_(fit|mlx)|mlx_lm"` is empty,
   `memory_pressure | grep 'free percentage'` is at least 25%, swap is under 1.5 GB
   (`sysctl vm.swapusage`), the browser and other heavy apps are closed.
7. The owner has confirmed this is the run: the log row in `eval/corpus/checkpoint_log.tsv` is
   the commitment.

## Step 1: `model_test` through the pipeline

```sh
cd ~/Developer/GitHub/financial-reports-analysis-model
caffeinate -i env PYTHONPATH=eval uv run python -m harness.checkpoint_run model_test --checkpoint
```

The guard refuses without `--checkpoint` and refuses a second start. It logs the row before any
document is opened, prints the document count and the projected runtime (an estimate from the
train-holdout dry runs), runs each English document in its own child process, and continues past
a failed document. An interrupted run resumes with `--continue`, which requires the same commit,
document list and configuration. Results and `summary.json` are under `var/checkpoint/model_test/`.

Stop rules: do not change a setting and rerun a document after seeing its result; a failure is
recorded with its class and reported.

## Step 2: `blind` through the pipeline

Only after step 1 is recorded as done; the guard refuses otherwise (D17).

```sh
caffeinate -i env PYTHONPATH=eval uv run python -m harness.checkpoint_run blind --checkpoint
```

The reserved issuers in `eval/corpus/rehearsal_reserve.yaml` are not run. Their documents stay
unopened until the rehearsal ([rehearsal.md](rehearsal.md)).

## Step 3: the adapter as a shippable model

Not a scoring step; it does not look at any pool. Fuse the update-1,000 adapter, convert it to
GGUF by the route in [14-run-profiles.md](../blueprint/14-run-profiles.md)
(`scripts/spikes/gguf_route.sh` shows each command, run there on a 0.5B model), and check that
the llama.cpp server returns the MLX model's reply on at least 50 rows of the validation panels
(not `model_test`), recording the agreement and the file hashes. It can run before step 1: it
touches no pool. Until it is done and recorded, the report says the adapter is a measured
candidate, not a component of the product, because no pipeline code calls a model today.

## What the report says

Write it from the artifacts only, in [accuracy-report.md](../accuracy-report.md), dated:

- The pipeline on `model_test` and `blind`: counts and rates from the summary files, with the
  statement that accuracy was not measured on these pools and why.
- Gate C: not measured on `model_test`, with the reason above, beside the validation-panel
  diagnostics of the adapter and their intervals, and a plain statement that the +5 point and 99%
  critical bars were not shown to be met.
- Gate E on the remaining `blind` issuers, with the reservation named.
- Limits: one adapter, one seed, validation panels that are not the gate, no Arabic claim (D21),
  no scored result for documents with no reference.
- No number is rounded up, pooled across panels, or replaced by a figure from another pool.

After the report is written: commit `eval/corpus/checkpoint_log.tsv` and the report, record the
checkpoint date in the plan, and keep the `var/checkpoint/` artifacts private outside Git.
