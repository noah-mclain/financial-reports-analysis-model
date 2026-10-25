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
the comparators. Peak MLX was 4.88 to 4.89 GB for the base and 5.46 to 5.49 GB with an adapter; every
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

## Bounded fit

The [matched pair](#measured-result-matched-bias-correction-pair-24-october-2026)
found the bias-corrected optimizer stable on its fixed sample. The
[generated-quality comparison](#measured-result-generated-quality-of-the-matched-pair-24-october-2026)
found no statistically distinguishable improvement over the base model. A longer fit tests
whether additional updates help; it produces one candidate and does not establish Gate C.

`training/normalization_fit.py` fixes constant LR 1e-5, AdamW bias correction, batch 4,
16 LoRA layers, gradient checkpointing, sequence cap 2048 and seed 17. Validation is the
same diagnostic selection of 64 rows from `valid.jsonl`, evaluated individually with row
provenance at step 0, every 50 updates and the final update. The fit selects exactly
`iters * 4` rows from the already exclusion-filtered `train.jsonl`, using the diagnostic
stride/cap policy and retaining selected index order. Each row is consumed once, without
shuffling, repetition or truncation. Preregistration freezes indices, hashes, counts and the
supervised-token budget, as well as the input pins, tokenizer/model identity, dependency
source hashes, package versions and abort rules.

The declared iteration range is 1 to 3000 (default 1000). Timeout defaults to 25000 seconds,
is capped at 75000 seconds, and must be at least `iters * 25` seconds, so the default iteration
count fits its default timeout and 3000 updates fit the cap exactly. A longer fit supplies both
`iters` and `timeout_seconds` explicitly. No limit is silently relaxed. Each arm of the pair
above took about 1,206 seconds for 60 updates, which includes model load and five validation
passes: roughly 20 seconds per update. Extrapolating gives approximately 5.5 hours at 1000 updates. This is an estimate, not a measured longer-fit runtime.

Finish source changes, checks and independent review before registration. Keep every artifact
outside Git under the private training root. Create `fit-plan.json` from the existing smoke
config's dataset/model path and manifest pins, adding the frozen exclusions file SHA-256.
Supply `iters: 1000`, `timeout_seconds: 25000`, `batch_size: 4`, `grad_checkpoint: true`,
`num_layers: 16`, `max_seq_length: 2048` and `seed: 17`; omit preregistration fields. The
`run_dir` field is required in the plan; registration generates distinct dry/run paths beside
its output. No model download, resource probe or training occurs during preregistration.

```sh
training_root="$HOME/Library/Application Support/financial-reports-analysis-model/normalization-training"
fit_dir="$training_root/bounded-fit"
fit() {
  PYTHONPATH=scripts:eval:training uv run --no-sync --offline python training/normalization_fit.py "$@"
}
fit --preregister "$training_root/fit-plan.json" --out "$fit_dir/preregistration.json"
make train-fit CONFIG="$fit_dir/fit-dry.json" DRY_RUN=1
caffeinate -i make train-fit CONFIG="$fit_dir/fit.json"
fit --evaluate "$fit_dir/runs/fit/report.json" --out "$fit_dir/evaluation.json"
```

The registration and generated configs are read-only. Each dry/train mode has an exclusive
attempt marker, and every run directory is exclusive. A failed attempt consumes its marker;
changing the output directory does not permit a retry. There is no resume. Training verification,
tokenization and model loading take place in one supervised heavy worker; the training controller
stays free of torch, transformers and MLX. A dry run verifies with the local tokenizer and does
not import MLX or load weights.

Abort on any nonfinite loss, any validation loss above 1.25 times step 0, final validation loss
above 1.10 times step 0, timeout, peak MLX above 11.5 GB, available memory below the 12 GB floor,
or source/identity/dataset/exclusions/model drift. The 1 GB MLX cache cap remains unchanged.
The final consumed supervised-token count must equal the preregistered budget. Errors preserve
`config.json`, `events.jsonl` (including nonfinite flags), `metrics.json` when available,
`resource-samples.jsonl`, `resources.json`, `trainer.log`, `report.json` and partial adapters.
The report lists individual criteria, elapsed time and per-validation ratios; failed or missing
evidence cannot authorize a candidate.

Intermediate `adapter/NNNNNNN_adapters.safetensors` checkpoints are retained every 100 updates.
The final update-N checkpoint and `adapter/adapters.safetensors` must agree. The selection rule
is fixed before training: **the final update-N adapter is the candidate**. No best-checkpoint
selection is performed. Evaluation always states `generated_quality: NOT MEASURED` and
`promotion: not authorized`.

After separately authorized training, reuse the matched pair's generation script shape from
the follow-up protocol and the existing
[base/adapter prediction and paired scoring commands](normalization-validation.md#local-base-and-adapter-predictions)
on the unchanged representative and challenge panels. Use the final adapter, greedy generation,
the same bounded completion and paired issuer bootstrap seed 20261023, with base predictions
on each identical panel. Report panels separately; never pool them or regenerate their
selection. This module adds no generation code. Gate C still requires at least 5 macro-F1
points above the base model and 99% critical-item correctness under the prescribed evaluation
protocol. Neither a successful fit nor these validation panels authorize promotion or early
`model_test`/`blind` scoring.

## Measured result: bounded fit, 25 October 2026

One preregistered fit (`/private/tmp/normalization-fit-20261025`; 1,000 updates, batch 4, 4,000
fit examples, 36,467 supervised tokens, bias-corrected AdamW, constant learning rate 1e-5, seed 17)
ran natively on the Mac in 14,537 seconds, about 14.5 seconds per update. It passed all ten
evaluation criteria with no abort rule triggered: peak MLX 9.12 GB (limit 11.5 GB), peak RSS
3.23 GB, token budget matched. Validation loss on the 64 diagnostic rows went from 0.0572 at update
0 to 0.0303 at update 1,000, a ratio of 0.53; the series is noisy (lowest 0.0164 at update 950,
highest 0.0671 at update 400, which is 1.17 times the initial value, under the 1.25 limit). The
candidate is the final update-1,000 adapter; no checkpoint was chosen by its scores.

The final adapter was generated at temperature 0 (at most 64 tokens) on the two frozen panels and
scored with the same command, seed and paired issuer bootstrap against the unadapted base
model's predictions as the matched pair above. Panels are reported separately.

| Panel | Predictor | Accuracy | Macro-F1 (47 classes) | Invalid | False mappings on null | Taxonomy-critical errors | Macro-F1 change vs base, 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Representative | Base | 0.875 | 0.520 | 15 | 20 | 5 of 23 | n/a |
| Representative | Pair treatment (60 updates) | 0.896 | 0.485 | 6 | 19 | 4 of 23 | -0.073 to +0.026 |
| Representative | Fit (1,000 updates) | 0.916 | 0.592 | 12 | 24 | 0 of 23 | +0.032 to +0.120 |
| Challenge | Base | 0.804 | 0.457 | 1 | 6 | 15 of 95 | n/a |
| Challenge | Pair treatment (60 updates) | 0.804 | 0.462 | 0 | 7 | 13 of 95 | -0.030 to +0.038 |
| Challenge | Fit (1,000 updates) | 0.881 | 0.538 | 1 | 5 | 6 of 95 | +0.018 to +0.105 |

Reading: unlike the 60-update adapter, the 1,000-update adapter is above the base model on both
panels, and both macro-F1 intervals exclude zero. The point estimates (+7.2 and +8.1 points)
exceed the Gate C margin of 5 points, but the lower ends of the intervals (+3.2 and +1.8 points)
do not, and the panels are validation diagnostics, not Gate C. Critical items are not at the 99%
bar on the challenge panel (6 errors of 95); the representative panel has no taxonomy-critical
error in 23 items, too few to bound a 99% rate. False mappings on null rows rose on the
representative panel (24 against 20). This is one adapter, one seed, one pass over 4,000 of the
fit rows. Nothing is promoted, and `model_test` and `blind` were not scored.

### Diagnostic: update-500 checkpoint against the final adapter

The retained update-500 checkpoint (`0000500_adapters.safetensors`, copied with the run's
`adapter_config.json` to its own adapter directory) was generated and scored with the identical
commands, panels, seed and base predictions. This is a diagnostic of how the gain develops during
training. It is not a selection: the fit's rule fixes the final update-1,000 adapter as the
candidate, and no checkpoint is chosen by these scores.

| Panel | Adapter | Accuracy | Macro-F1 (47 classes) | Invalid | False mappings on null | Taxonomy-critical errors | Macro-F1 change vs base, 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Representative | Update 500 | 0.930 | 0.575 | 6 | 20 | 2 of 23 | +0.014 to +0.110 |
| Representative | Update 1,000 | 0.916 | 0.592 | 12 | 24 | 0 of 23 | +0.032 to +0.120 |
| Challenge | Update 500 | 0.756 | 0.472 | 0 | 6 | 22 of 95 | -0.038 to +0.053 |
| Challenge | Update 1,000 | 0.881 | 0.538 | 1 | 5 | 6 of 95 | +0.018 to +0.105 |

Reading: on the representative panel the gain over the base model was already present at update
500 and changed little by update 1,000 (macro-F1 0.575 to 0.592). On the challenge panel it
appears only between update 500 and 1,000: at update 500 the interval includes zero and accuracy
is below the base model's 0.804. The harder rows were still improving when the run ended, which
is consistent with a longer fit helping, but a single seed and two diagnostic panels do not establish a trend.

## The 3,000-update fit aborted, 25 October 2026

The longer fit (`/private/tmp/normalization-fit-3000-20261025`, 3,000 updates, seed 17, timeout
75,000 seconds) triggered the preregistered abort rule at update 1,500 after 21,874 seconds:
validation loss on the 64 diagnostic rows was 0.0778 against 0.0572 at update 0, a ratio of 1.36
(limit 1.25). Earlier validations ranged 0.0147 to 0.0395 from update 1,150 to 1,450. The report
status is `failed`; per the rules above a failed run cannot authorize a candidate, there is no
resume, and the attempt marker is consumed. No panel was generated or scored. Retained
checkpoints are 100 to 1,400 in steps of 100 (no update-1,500 checkpoint). The 1,000-update fit
above remains the only candidate. Whether to change the abort rule for a new preregistration is
an owner decision; the rule was not changed after seeing this result.

### Generating and scoring a checkpoint as a diagnostic

Only as a diagnostic, as with update 500 above: no checkpoint is chosen by its scores. Copy
`adapter/00NNNNN_adapters.safetensors` as `adapters.safetensors` beside the run's
`adapter_config.json` in `$FIT/ckpt-NNNN/adapter`, then generate and score with the commands below. `NNNN` is
the update count without padding (`1400`); `00NNNNN` is the seven-digit file prefix (`0001400`);
`ckptNNNN` is the output file prefix. Do not
run them while another heavy job holds memory.

   ```sh
   cd ~/.herdr/worktrees/financial-reports-analysis-model/normalization-fit
   export PYTHONPATH=scripts:eval:training
   PY=~/Developer/GitHub/financial-reports-analysis-model/.venv/bin/python
   FIT=/private/tmp/normalization-fit-3000-20261025
   ```

1. **Generate on both panels** with the checkpoint adapter, 64 tokens at most, the MLX allocation
   limited to 6 GB in the child, and each panel under its own deadline (about 13 minutes for
   the 512 representative rows and 4 for the 168 challenge rows at the 0.7 rows per second the
   1,000-update run measured; those are estimates for this run). `mkdir` refuses an existing
   output directory, and `normalization_mlx.py` refuses an existing output file.

   ```sh
   PANELS=/private/tmp/normalization-quality-protocol/panels-v1
   BASE=/private/tmp/normalization-stability-reference-pair-20261024T174500Z/generated-v1
   ROOT="$HOME/Library/Application Support/financial-reports-analysis-model/normalization-training"
   mkdir -m 700 "$FIT/generated-v1"
   child='import mlx.core as mx,runpy; mx.set_memory_limit(6_000_000_000); runpy.run_path("training/normalization_mlx.py",run_name="__main__")'
   for panel in representative challenge; do
     limit=1500; [ "$panel" = challenge ] && limit=600
     HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 /usr/bin/caffeinate -i timeout "$limit" \
       "$PY" -c "$child" --selection "$PANELS/$panel-selection.json" \
       --model-dir "$ROOT/qwen2.5-7b-instruct-4bit" --model-manifest "$ROOT/local-model.json" \
       --adapter-dir "$FIT/ckpt-NNNN/adapter" --max-tokens 64 \
       --out "$FIT/generated-v1/ckptNNNN-$panel.json" || break
     jq -e '.metadata.peak_mlx_bytes <= 6000000000' "$FIT/generated-v1/ckptNNNN-$panel.json" >/dev/null \
       || { echo "allocation exceeded: preserve $panel output as failed"; break; }
   done
   ```

   The base predictions on the same panels already exist (`$BASE/base-<panel>.json`); do not
   regenerate them or the panels.

2. **Score each panel separately**, paired with its base predictions and the same bootstrap seed
   as every earlier comparison. Never pool the panels.

   ```sh
   for panel in representative challenge; do
     "$PY" training/normalization_validation.py score \
       --selection "$PANELS/$panel-selection.json" \
       --predictions "$FIT/generated-v1/ckptNNNN-$panel.json" \
       --base "$BASE/base-$panel.json" --seed 20261023 \
       --out "$FIT/generated-v1/ckptNNNN-$panel-score.json"
   done
   ```

3. **Record the result** in a new "Measured result" section here and in the Normalization section
   of [accuracy-report.md](../accuracy-report.md), only from these score files. Report, per panel
   and beside the base model and the 1,000-update adapter: accuracy, macro-F1 over 47 classes,
   invalid replies, false mappings on null rows, taxonomy-critical errors and the paired
   macro-F1 change with its 95% interval. State what the numbers cannot show: one seed, two
   validation panels, no `model_test` or `blind` score. Compare against the Gate C bar (5
   macro-F1 points above base with the interval's lower end considered, 99% on critical items)
   without declaring it met.

### Measured diagnostic: update-1,400 checkpoint

Generated and scored with exactly the commands above (`ckpt-1400`; temperature 0, at most 64
tokens, paired issuer bootstrap seed 20261023, peak MLX 5.46 and 5.49 GB under the 6 GB limit)
against the same base predictions. A diagnostic of whether the longer run was still improving, not
a selection: the checkpoint belongs to a failed run and cannot be a candidate.

| Panel | Adapter | Accuracy | Macro-F1 (47 classes) | Invalid | False mappings on null | Taxonomy-critical errors | Macro-F1 change vs base, 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Representative | Update 1,000 | 0.916 | 0.592 | 12 | 24 | 0 of 23 | +0.032 to +0.120 |
| Representative | Update 1,400 (aborted run) | 0.895 | 0.603 | 23 | 26 | 0 of 23 | +0.043 to +0.142 |
| Challenge | Update 1,000 | 0.881 | 0.538 | 1 | 5 | 6 of 95 | +0.018 to +0.105 |
| Challenge | Update 1,400 (aborted run) | 0.857 | 0.493 | 1 | 7 | 5 of 95 | -0.014 to +0.074 |

Reading: more updates did not improve generated quality. On the representative panel macro-F1 is
about the same (0.603 against 0.592) while accuracy fell and invalid replies doubled (23 against
12). On the challenge panel accuracy and macro-F1 are lower than at update 1,000 and the interval
includes zero, while taxonomy-critical errors are 5 of 95 against 6. The two runs trained on different fit selections (the selection depends on the
iteration count), so this is a single-seed comparison, not a trend. It gives no reason to prefer a
longer fit over the update-1,000 candidate.
