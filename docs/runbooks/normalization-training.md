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
14 GB available memory: the blueprint's 11.5 GB training allocation plus 2.5 GB headroom.
Available memory is estimated from `memory_pressure -Q` free percentage times `hw.memsize`.
These are conservative planning thresholds, not measured training capacity. API, worker and
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

The separate padding-paired diagnostic keeps these training thresholds for its `train` child.
Its bounded `probe` and `reference` children use an explicitly reviewed 8.5 GB available-memory
preflight and a 6 GB MLX allocation cap (with the same 1 GB cache). That diagnostic-only policy
uses the blueprint's 6 GB analysis budget plus 2.5 GB headroom; it does not change the training
profile. Every stage repeats its own preflight before loading weights and fails if its measured
MLX peak exceeds its declared allocation.

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
