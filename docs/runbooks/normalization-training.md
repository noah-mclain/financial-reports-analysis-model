# Bounded normalization training smoke

This entry point checks a pinned, filtered builder dataset and a local 4-bit Qwen2.5-7B
snapshot before calling the installed mlx-lm 0.31.3 public `trainer.train` API with bounded
LoRA/AdamW setup and an explicit loss hook. It is a wiring smoke,
not a capacity test, full training run, adapter promotion or Gate C result. It selects the
first 50 fit rows (or all if fewer) and the first validation row, without selecting on targets.
Short real examples do not establish capacity at 2,048 tokens. No synthetic padding is added.

Prepare the dataset with [the existing builder](normalization-dataset-builder.md). Use the
filtered dataset carrying the frozen exclusion audit, and supply the matching frozen exclusions.
For the current SEC lineage, this is `dataset_sec_4_v3`, with exclusions frozen from
`dataset_sec_4_v2`; the older `dataset_sec_2` plan is not a current training input.
Approval remains the caller's responsibility: the explicit manifest pins detect changed inputs,
not forged approvals. This command does not reconstruct issuer partitions or infer new approvals.

## Explicit configuration

Save a private JSON file outside Git, substituting approved absolute paths and SHA-256 pins.
Compute each pin with `shasum -a 256` on its named manifest; inspect the manifest and its lineage
before using the result. Do not silently repin changed inputs.

```json
{
  "dataset_dir": "/private/tmp/approved-dataset",
  "dataset_manifest_sha256": "<sha256 of dataset manifest.json>",
  "exclusions": "/private/tmp/approved-exclusions.json",
  "model_dir": "/private/tmp/local-qwen2.5-7b-4bit",
  "model_manifest": "/private/tmp/local-model.json",
  "model_manifest_sha256": "<sha256 of local-model.json>",
  "run_dir": "/private/tmp/normalization-smoke-preflight",
  "iters": 20,
  "batch_size": 1,
  "grad_checkpoint": true,
  "num_layers": 16,
  "max_seq_length": 2048,
  "seed": 17,
  "timeout_seconds": 900
}
```

The output directory must be new and outside Git. Every attempt consumes its directory,
including failed preflights; use another private path for the next attempt. The tokenizer must
match the dataset exactly, all model artifacts must match the pinned model manifest, and the
manifest's recorded model directory must match the supplied local directory. Train and valid
bytes, prompt, taxonomy, builder version and frozen exclusion audit must match. `test.jsonl`
beside the dataset is refused without opening it. No download or remote inference is supported.

## Preflight and execution

From the assigned worktree, with its provisioned `.venv`:

```sh
make train-smoke CONFIG=/private/tmp/normalization-smoke.json DRY_RUN=1
```

This performs non-training checks and token counts only; it never imports MLX or creates an
adapter. The resource probe requires Apple Silicon macOS, at least 15 GB free disk and
12 GB available memory floor. Completed 60-iteration runs measured an 8.15 GB peak MLX
allocation and a peak process RSS of 2.42 to 3.20 GB (4.03 GB in the historical arm D). These
counters overlap in unified memory, so their sum is a ceiling, not a measurement: 11.35 GB for
the matched-pair runs, 12.18 GB for arm D. The 12 GB floor covers the pair runs with 0.65 GB
spare and is below the summed ceiling for arm D. The unchanged 11.5 GB MLX allocator cap is not
the measured need, and the 1 GB cache limit is unchanged. Available memory is
estimated from `memory_pressure -Q` free percentage times `hw.memsize`; on this
19,327,352,832-byte machine, each percentage point represents about 193 MB. API, worker and
repository Vite processes must be stopped. A failed system probe refuses execution. Close other
apps manually; the command never kills them, uses sudo, changes wired limits or changes settings.

After independent review and separate authorization to train, change `run_dir` to a new private
path in the config, then run exactly:

```sh
caffeinate -i make train-smoke CONFIG=/private/tmp/normalization-smoke.json
```

The child process repeats preflight and identities before loading weights. It runs at most
20 iterations, batch 1, at most 16 trainable layers, checkpointed activations, fixed seed 17,
AdamW and rank-16 LoRA. There is one optimizer update per iteration. The model is already
4-bit; no full-precision weights are downloaded. The child has the explicit configured timeout
(up to 1,800 seconds), a 1 GB MLX cache limit and an 11.5 GB MLX allocator limit. These allocator
limits do not cap total system memory. A timeout kills only the child and reports failure;
partial artifacts remain diagnostic and must not be promoted or reused.

There is no padding-paired diagnostic entry point in this checkout. The diagnostic allocation
constants alone do not implement probe/reference children; the training thresholds above remain
in force. The optimization diagnostic below uses the same training resource policy.

Both manifest maximum sequence lengths and the selected rows are checked against the configured
cap. The child also checks exact upstream chat tokenization and masked target lengths before
training. Overflow fails instead of reaching upstream silent truncation. The upstream automatic
dataset loader is never called: only fit rows enter optimization, with one validation row for
loss diagnostics. No test-file scoring, holdout, `model_test` or `blind` access is implemented.

## Artifacts and next gates

`config.json` preserves exact inputs and bounds. `report.json` records success or failure,
identity hashes, exclusion evidence, selected-row hashes, environment versions, times and parent
RSS. Training adds `trainer.log`, `resources.json` with child peak MLX/RSS counters, and an
`adapter/` containing the upstream adapter configuration and weights. On a killed child, peak
counters may be unavailable; absence is not a zero measurement. Failed runs return nonzero.
All artifacts and any labels or weights remain private and outside Git.

Preparation establishes builder contracts; fit trains the adapter; validation guides development.
The train holdout is a separately logged, frozen-candidate measurement under D16. `model_test`
and `blind` wait for the owner-run checkpoint (D13/D17). A successful smoke proves none of those
quality gates. Full training still needs a reviewed data/stratum plan, baseline comparisons,
resource measurements at representative sequence lengths and the relevant Gate C evidence.
GGUF export requires the separately guarded rehearsal and D12 disk/weight conditions; this
entry point implements neither export nor a full-training scheduler.

## Preregistered optimization stability diagnostic

Question: does the earlier smoke spike and validation rise depend on optimization setup, and do
smaller learning rates or larger batches remove it on this sample? This is an optimization
experiment, with no accuracy, capacity, quality or Gate C claim. No diagnostic measurements have
been recorded here. This phase supplies commands for later authorized execution only.

| Arm | Learning rate | Batch | Optimizer updates |
| --- | --- | --- | --- |
| A | 0.0001 | 1 | 60 |
| B | 0.00002 | 1 | 60 |
| C | 0.00002 | 4 | 60 |
| D | 0.00001 | 4 | 60 |

Every arm uses one fresh base-model process, seed 17, rank 16, 16 trainable layers, AdamW,
activation checkpointing, a 2,048-token cap and the padding-excluding loss. No learning-rate
schedule or adapter resume is allowed. Each process has a 1,500-second maximum. The 12 GB
available-memory preflight floor, 11.5 GB allocation limit and 1 GB cache policy are unchanged.

The authorized input is `dataset_sec_5/train.jsonl` and `valid.jsonl`, plus manifests,
exclusions and model metadata. The dataset manifest SHA-256 is
`9cae5c2febd7e568cb1996150b99bfad7170f5c565dea090010e67b9953843f5` and exclusions SHA-256 is
`9d5388129dbdba74dff6bd3fa5724f738b7e0d4f963208cfd4de34941b6c8a7f`.
Model pins come from the existing `smoke-config.json`; do not repin or download. No holdout,
`model_test`, `blind`, `test.jsonl` or `dev` content is read.

Select 240 fit and 64 validation rows. For a file with N rows and requested count K, zero-based
candidates are `floor(k*(N-1)/(K-1))` for k from 0 through K-1. Scan forward from each candidate,
skipping over-cap rows and already selected indices. Count unique over-cap indices and
collisions, never wrap or search backward, and fail on exhaustion or invalid supervision.
Targets never select rows. Class counts and null share are descriptive statistics computed
after selection. A fresh `random.Random(17)` permutes each split, identically for every arm.
Contiguous batches preserve that order without sorting or another permutation. C and D consume
all 240 selected fit rows in 60 updates; A and B consume the first 60 rows of the same permutation.
Ordinary batch padding is allowed; no synthetic examples or length inflation are added.

Validation is the arithmetic mean of singleton losses over all 64 selected rows with dropout
disabled: true step 0 before optimization, then after updates 15, 30, 45 and 60. The installed
trainer's usual pre-update validation callback is disabled for these arms. Reports retain each
training loss, validation losses and ratios, nonfinite events, elapsed time, and peak MLX/RSS
bytes. Missing counters are unmeasured, never zero.

The evaluator emits a separate verdict for each criterion:

1. No nonfinite values.
2. Final validation loss is at most 1.10 times initial validation loss.
3. Every validation loss is at most 1.25 times initial validation loss.
4. Maximum training loss is at most 0.5 times A's maximum in the same experiment.

All four must pass with complete run, resource and identity evidence for the outcome
`stable on this sample`. `not stable on this sample` means a complete, usable measurement
that failed at least one criterion. Missing arms and failed or incomplete reports are labeled
`NOT MEASURED` when their evidence cannot support a verdict. A is compared literally with itself
for criterion 4. If A passes both validation rules, validity is
`INCONCLUSIVE about LR/batch` and there is no winner. Missing or unusable criteria read
`NOT MEASURED`; failed and partial reports retain evidence and cannot silently pass. Unknown
baseline validation rules also prevent a winner. A table from partial runs is still valid output;
four successful runs are not required to report failure honestly. One seed and 64 validation
rows cannot establish generalization or compare near ties reliably.

### Offline preparation and execution

Finish source changes, native checks and independent full-diff review before preparing the
immutable preregistration. From the assigned worktree, the following defines an offline helper;
all artifacts remain under the existing private training root, outside Git:

```sh
export UV_CACHE_DIR="$PWD/.venv/.uv-cache"
training_root="$HOME/Library/Application Support/financial-reports-analysis-model/normalization-training"
stability_dir="$training_root/stability-diagnostic"
stability() {
  PYTHONPATH=scripts:eval:training uv run --no-sync --offline python training/normalization_stability.py "$@"
}

stability --prepare-configs "$training_root/smoke-config.json" --out "$stability_dir"
stability --preregister "$stability_dir/plan.json" --out "$stability_dir/preregistration.json"
```

Preparation reads existing model pins; preregistration verifies identities and tokenizes selected
rows without a resource probe, MLX model load or training. It exclusively writes the hypothesis,
all four configs, selection semantics, indices, permutations, sample statistics/hashes, source
hashes, pins, metrics and rules. The preregistration is read-only and its SHA-256 is bound into
every generated config and report. Changed inputs or implementation refuse execution. Never
rewrite the preregistration after the first attempt.

After separate authorization, preflight and run each arm once, using the generated distinct dry
and training directories. For arm A (repeat with B, C and D only as authorized):

```sh
make train-stability CONFIG="$stability_dir/A-dry.json" DRY_RUN=1
caffeinate -i make train-stability CONFIG="$stability_dir/A.json"
```

Every attempt consumes its run directory; an exclusive per-arm attempt marker also prevents
retrying with another directory. Stop on authentication/quota, identity/hash/pin mismatch,
preflight refusal, resource/runtime excess or scope expansion. A nonfinite or other numerical
failure ends that arm and preserves evidence; the owner may continue the remaining arms after
a non-environment failure, without rerunning the failed arm. Do not change the rules, probes,
thresholds or inputs in response to results.

Generate the table using completed or failed reports from this experiment:

```sh
stability --evaluate "$stability_dir/runs/A/report.json" "$stability_dir/runs/B/report.json" \
  "$stability_dir/runs/C/report.json" "$stability_dir/runs/D/report.json" \
  --out "$stability_dir/evaluation.json"
```

If an arm was never attempted, omit its report argument; it appears as `NOT MEASURED` in the
four-arm table. Identity, sample, config, source and preregistration mismatches fail loudly.
`events.jsonl` preserves step identity and JSON-safe nonfinite markers even when `metrics.json`
cannot be completed. Failed adapters and partial artifacts must not be reused or promoted.

## Measured result: matched bias-correction pair, 24 October 2026

One preregistered matched pair on the stability sample (240 fit examples, 64 validation rows,
2,161 supervised tokens, 60 updates, `dataset_sec_5`), run natively on the Mac at commit
`f51bda8` (registration `df77cbce…6853`, dataset manifest `9cae5c2f…43f5`). Both arms
exited 0 with `diagnostic_completed` and no artifact errors. This is a teacher-forced
optimization-stability check; it says nothing about generated answers.

| Measure | Reference (no correction) | Treatment (bias-corrected) |
| --- | --- | --- |
| Verdict | not stable on this sample | stable on this sample |
| Validation loss, initial to final | 0.0572 to 0.0337 | 0.0572 to 0.0196 |
| Worst validation loss, ratio to initial (limit 1.25) | 1.50, fails | 1.00, passes |
| Final validation, ratio to initial (limit 1.10) | 0.59, passes | 0.34, passes |
| Maximum training loss | 0.633 | 0.460 |
| Maximum training loss, ratio to the historical arm A ceiling | 0.062 | 0.045 |
| Nonfinite values | none | none |
| Peak MLX / peak RSS | 8.15 GB / 2.42 GB | 8.15 GB / 3.20 GB |
| Elapsed | 1,206 s | 1,205 s |

The treatment arm reproduced the earlier treatment-only run exactly (final validation 0.0196,
maximum training loss 0.4596), which used the sources before the 12 GB launch floor.

Limits: one pair, one sample, one seed. The reference arm failed on a single validation spike.
`generated_quality` is `NOT MEASURED` and promotion is not authorized by teacher-forced
stability. Two earlier attempts at this pair were refused by the memory preflight at 72% free
before any training (see the launch floor above); their registrations were discarded and no
result from them is used.

## Measured result: generated quality of the matched pair, 24 October 2026

Final update-60 adapters of the pair above, generated at temperature 0 with at most 64 tokens
on the two frozen panels (`representative`, 512 rows; `challenge`, 168 rows), then scored with
`normalization_validation.py score`, seed 20261023, paired issuer bootstrap against the base
model's predictions on the same panel. The unadapted base model and the label-lexicon match are
the comparators. Peak MLX was 4.89 GB for the base and 5.46 to 5.49 GB with an adapter; every
process stayed under the 6 GB limit. These are validation diagnostics, not Gate C: panels are
reported separately and never pooled.

| Panel | Predictor | Accuracy | Macro-F1 (47 classes) | Invalid | False mappings on null | Taxonomy-critical errors | Macro-F1 change vs base, 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Representative | Base | 0.875 | 0.520 | 15 | 20 | 5 of 23 | n/a |
| Representative | Reference | 0.830 | 0.222 | 4 | 5 | 10 of 23 | -0.318 to -0.183 |
| Representative | Treatment | 0.896 | 0.485 | 6 | 19 | 4 of 23 | -0.073 to +0.026 |
| Representative | Lexicon | 0.850 | 0.289 | 0 | 2 | 14 of 23 | -0.278 to -0.138 |
| Challenge | Base | 0.804 | 0.457 | 1 | 6 | 15 of 95 | n/a |
| Challenge | Reference | 0.613 | 0.219 | 1 | 1 | 33 of 95 | -0.246 to -0.130 |
| Challenge | Treatment | 0.804 | 0.462 | 0 | 7 | 13 of 95 | -0.030 to +0.038 |
| Challenge | Lexicon | 0.482 | 0.239 | 0 | 0 | 62 of 95 | -0.258 to -0.115 |

Reading: the uncorrected reference adapter is clearly worse than the base model on both panels.
The bias-corrected adapter is not distinguishable from the base model on either panel (both
macro-F1 intervals include zero), with fewer invalid outputs on the representative panel (6
against 15). It does not reach the Gate C bar of 5 macro-F1 points above the base model, and the
critical-item rate is far from 99%. Sixty updates on 240 examples show that the corrected
optimizer is stable and avoids the damage the uncorrected one does; they do not show an
improvement from fine-tuning. Nothing is promoted, and `model_test` and `blind` were not scored.
