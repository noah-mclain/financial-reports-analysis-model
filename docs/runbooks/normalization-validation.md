# Normalization validation baselines

This comparison uses only the validation subdivision of `train`. It does not score
the train holdout, `model_test` or `blind`, and its scores do not establish Gate C. The
current SEC dataset has no PDF or Arabic examples.

The current runner requires builder version 2, whose shared prompt explicitly provides the
canonical catalog and JSON response schema. Original version 1 artifacts are retained, but
their underspecified prompt is not the model-comparison baseline. A 16-row native smoke on
that original prompt produced 16 prose replies rejected by the schema parser; no normalization
accuracy claim follows from that format diagnostic. Rebuild into a separate directory using
[the dataset builder](normalization-dataset-builder.md#version-2-explicit-normalization-output-contract).

## Prepare a selection and lexicon baseline

```sh
make normalization-prepare DATASET_DIR=/path/to/dataset_sec_2 \
    EXAMPLES=/path/to/sec_examples/examples.jsonl SOURCES=/path/to/sec_examples/sources.jsonl \
    APPROVED=/path/to/sec_examples/approved.txt REGISTRY=/path/to/issuer-pools.json \
    OUT_DIR=var/normalization-validation/full
```

All input paths are explicit. The registry must exist. The command validates the source roster
and registry before opening example labels. It uses the same partition, approvals, recorded
moves and looks as the dataset builder. It reproduces the
partition manifest, per-issuer deduplication, counts and exact rendered fit and validation
bytes, checking their recorded SHA-256 digests. It does not recount token lengths. Changed
approvals, taxonomy, registry or split records refuse the comparison.

A TRAIN roster may include train-holdout metadata, but holdout examples are rejected and never
enter supervision, selection, scoring or generated dataset outputs; as in the builder, example
files are parsed in memory before rejection. Strict pre-label authorization (parsing only source
identity first) is not implemented and is a possible separate hardening task.

The output directory must not exist. `selection.json` records validation examples with issuer,
period and source/language metadata alongside the original dataset manifest. `lexicon.json`
holds responses and the selection fingerprint; `report.json` holds scores. These are local
artifacts, never training data. Treat the preparation inputs and the resulting selection as
trusted caller artifacts: the fingerprints detect differences, not forged approvals.

`LIMIT=n SEED=0` freezes a bounded selection using the lowest seeded hashes of example ids,
without reading targets to choose examples. Selected rows are ordered by example id. Sampling
does not guarantee class coverage: reports retain missing classes and state available versus
selected counts. Keep one selection unchanged across lexicon, base and adapter comparisons.
For a build with an issuer cap, supply its same `MAX_PER_ISSUER`.

The lexicon baseline calls the existing `LabelIndex.match` using label and statement only.
Unmatched or ambiguous labels abstain. It does not exercise the structural mapping pipeline,
and no alias or threshold is adjusted on validation.

## Local base and adapter predictions

```sh
make normalization-predict SELECTION=var/normalization-validation/full/selection.json \
    MODEL_DIR=/path/to/local-4bit-snapshot MODEL_MANIFEST=/path/to/local-model.json \
    OUT=var/normalization-validation/base.json
make normalization-score SELECTION=var/normalization-validation/full/selection.json \
    PREDICTIONS=var/normalization-validation/base.json OUT=var/normalization-validation/base-score.json
```

MLX runs on macOS. The model snapshot must already exist locally, have the Qwen2.5-7B
architecture, be 4-bit and have exactly the dataset tokenizer identity. `MODEL_MANIFEST` is
the caller's pinned download/assembly record: `repository` must be
`mlx-community/Qwen2.5-7B-Instruct-4bit`, `revision` an immutable commit hash,
`local_directory` its recorded path, `tokenizer` a provenance description and `artifact_sha256`
the filename-to-SHA-256 map of the snapshot's weights, JSON, text and Jinja artifacts. The
record is an explicit caller trust boundary, not proof of remote authorization. Every artifact
must match; a different model size or changed weight file is refused before loading MLX.
Configuration, weights and tokenizer artifacts are hashed;
library versions, generation settings, time, throughput and peak MLX and resident memory are
recorded. MLX imports only after these checks. There is no network download or remote inference
fallback. Run with sufficient free memory and no concurrent heavy workload, following
[the memory budgets](../blueprint/01-constraints.md#13-memory-budgets-planning-estimates).

The runner uses the dataset's system instruction and label/statement user message, without
the target or issuer/period metadata. Generation is greedy with a bounded completion. Identical
prompts share one response within a run, while every original example retains its own id and
issuer for scoring. Both row count and unique prompt count are recorded.

Supply `ADAPTER_DIR=/path/to/adapter` to predict with an adapter; its configuration and weights
are hashed too. Supply `BASE=/path/to/base.json` to `normalization-score` for the paired
comparison. Every prediction set must match the selection fingerprint and contain each
selected example id exactly once. Unknown, missing, extra and duplicate ids refuse scoring.

## Metrics and limits

The primary macro-F1 averages all 47 canonical classes with zero for an unsupported class;
`macro_class_count` records that denominator. Null is excluded from this metric and included
in a separately named macro-F1. Per-class precision, recall, F1 and support, overall accuracy,
null-only accuracy, correct abstentions, invalid responses and false mappings on null targets
are reported. An invalid response is always an error, including on a null target. Duplicate
JSON keys, unknown ids, wrong-statement ids, extra fields and non-JSON replies are invalid.

Both the plan's six critical items and the taxonomy's broader critical set report errors,
counts, contributing issuer counts and one-sided 95% exact binomial upper error bounds.
With fewer than 300 critical examples or 30 contributing issuers, that critical summary is
explicitly `not shown`, retaining the point estimate and bound. Rows from one issuer are dependent;
the bounds alone do not prove 99% correctness. Scores are also shown per issuer, per
source/language stratum and per period kind. Period kinds are descriptive, never new pools.

Adapter-minus-base macro-F1 uses at least 1,000 seeded paired issuer bootstrap resamples,
separately for each source/language stratum. Under 30 issuers the comparison is marked
`not shown`. Validation remains development evidence regardless of the interval. The Gate C
thresholds, support requirements and logged train-holdout measurement are described in
[the evaluation design](../blueprint/04-execution-phases.md#24-narrative-dataset-split-and-length-audit).
SEC labels inherit the approved crosswalk's semantics; no independent PDF-label review is
claimed by these scores.

## Measured validation baseline (2026-10-19)

Local evidence lives under `var/normalization-validation/prompt-v2/`. `dataset/` contains
the separate builder-v2 dataset; `full/` contains its reconstructed selection, lexicon
predictions and report. The preparation command reported 157.99 seconds including output
writing (the report records 156.71 seconds before writing). It verified all 672,895 fit and
138,734 validation rows against their rendered bytes and SHA-256 values. The upstream
partition is identical to the original SEC build. No recorded move applies to this roster.

The unchanged lexicon on all 138,734 validation rows from 938 issuers scored:

| metric | result |
| --- | --- |
| overall accuracy | 85.6906% |
| always-null accuracy | 80.6911% |
| macro-F1, all 47 canonical ids | 43.2374% |
| invalid responses | 0 |
| false mappings on null targets | 1,351 |
| plan-critical correct / support | 2,639 / 3,421 (77.1412%) |

The frozen `bounded-512/selection.json` uses seed 0 and the lowest hashed example ids,
with 368 issuers and 33 supported canonical classes. Its 11 plan-critical examples are
insufficient for a critical-item conclusion. `smoke-16/` is the lowest-hash 16-row subset.
Both selections retain exactly the same example ids used before the prompt correction.

The v2 16-row format smoke generated 15 schema-valid responses and one invalid response
in 34.03 seconds (peak MLX allocation 4,883,624,852 bytes). The invalid response remains
an error. This is a format diagnostic; the sample contains only one non-null target and
does not estimate normalization quality.

The pinned local model is `mlx-community/Qwen2.5-7B-Instruct-4bit` at revision
`c26a38f6a37d0a51b4e9a1eb3026530fa35d9fed`, assembled with the dataset's exact tokenizer.
`local-model.json` in the parent evidence directory records all seven artifact hashes.
MLX is 0.32.2 and mlx-lm is 0.31.3; inference is greedy with a 64-token completion bound.

On the same frozen 512 rows, with invalid replies counted as errors:

| metric | lexicon | unadapted 7B |
| --- | --- | --- |
| accuracy | 86.3281% | 89.2578% |
| macro-F1, all 47 canonical ids | 30.6576% | 52.7778% |
| invalid replies | 0 | 11 |
| correct non-null mappings / 97 | 29 | 73 |
| correct abstentions / 415 | 413 | 384 |
| false mappings on null targets | 2 | 21 |

The model improves macro-F1 by 22.12 percentage points, with a paired issuer bootstrap
95% interval of +12.49 to +26.42 points (1,000 resamples, seed 0, SEC English stratum).
It also makes more false mappings on null targets. These are validation diagnostics, not
an acceptance gate or evidence for PDF/Arabic performance. The 11 critical examples remain
marked `not shown`; their point estimates cannot establish critical-item correctness.

Generation took 813.12 seconds for 512 rows and 468 unique prompts. Peak MLX allocation
was 4,887,622,548 bytes; peak resident memory was 1,872,134,144 bytes. These counters
describe this inference process, not total system memory or a training budget.
The model responses and paired score are `base-512.json` and `base-512-score.json`.
To reproduce after preparing the same selection and pinned local snapshot, use a new output
path (existing files are refused):

```sh
PYTHONPATH=scripts:eval:training uv run python training/normalization_mlx.py \
    --selection var/normalization-validation/prompt-v2/bounded-512/selection.json \
    --model-dir var/models/qwen2.5-7b-instruct-4bit \
    --model-manifest var/normalization-validation/local-model.json \
    --out var/normalization-validation/prompt-v2/base-512-repeat.json --max-tokens 64
make normalization-score \
    SELECTION=var/normalization-validation/prompt-v2/bounded-512/selection.json \
    PREDICTIONS=var/normalization-validation/prompt-v2/base-512-repeat.json \
    BASE=var/normalization-validation/prompt-v2/bounded-512/lexicon.json \
    OUT=var/normalization-validation/prompt-v2/base-512-repeat-score.json SEED=0
```

No adapter was trained in this measurement. Training and export require the separate machine
readiness preflight; they are not implied by a successful inference baseline.

After validation: a bounded training smoke, the separately guarded 7B GGUF rehearsal, QLoRA
comparison, candidate freeze and a logged train-holdout evaluation. `model_test` and `blind`
remain reserved for the owner-run checkpoint. This runner does not implement those paths.
