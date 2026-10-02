# 01. Constraints

Everything below was measured or checked against upstream sources on 2026-09-11. Planning
estimates are labelled as such and are replaced by Phase 0 measurements (tasks 0.3 and 0.4).

## 1.1 Development machine (measured)

| Fact | Value | Implication |
|------|-------|-------------|
| SoC | Apple M3 Pro, 6 performance + 6 efficiency cores | One heavy GPU workload at a time; CPU stages (parsing, pandas) can run alongside |
| Unified memory | 18 GB | CPU, GPU, OS and every process share one pool |
| GPU wired limit | macOS default (`iogpu.wired_limit_mb: 0`) | Read `max_recommended_working_set_size` from MLX device info in Phase 0; raise only for training if the smoke run needs it |
| Memory pressure in normal use | 7.15 GB held by the compressor, 2.45 of 3.07 GB swap used, 41% free | Operating rule (ADR 0008): demos and training run with all other apps closed, and preflight refuses to start when memory is short (section 1.3) |
| Free disk | 44 GB (volume 96% used) | Hard budget for weights, datasets, caches and swap growth (section 1.4) |
| OS | macOS 27.0 | mlx wheels exist for macOS 14+; Phase 0 smoke-tests MLX and PyTorch MPS on this release |
| Toolchain | uv 0.8.3, Python 3.12.3, Node 22.22.2, npm 10.9.7, GNU Make 3.81, git-lfs 3.7.1, tesseract 5.5.2 | pnpm through corepack; Makefile rather than another task runner |
| Apple Vision OCR languages | 33, including `ar-SA` and `ars-SA` | docling `OcrMacOptions` covers English now and Arabic in Phase 4 without another engine |

## 1.2 Memory math for the candidate models

KV cache per token = 2 (K and V) x attention layers holding a cache x KV heads x head dim x 2 bytes (fp16).

| Model (4-bit MLX) | Weights on disk | Layers with KV cache | KV per token | KV at 8,192 tokens | KV at 32,768 tokens | Vocab |
|-------------------|-----------------|----------------------|--------------|--------------------|---------------------|-------|
| Qwen2.5-7B-Instruct | 4.30 GB | 28 of 28 (4 KV heads, dim 128) | 56 KiB | 0.47 GB | 1.88 GB | 152,064 |
| Qwen3.5-9B | 5.95 GB | 8 of 32 (4 KV heads, dim 256); the 24 linear-attention layers keep a fixed-size state | 32 KiB | 0.27 GB | 1.07 GB | 248,320 |
| Qwen3.5-4B | 3.03 GB | 8 of 32 (4 KV heads, dim 256) | 32 KiB | 0.27 GB | 1.07 GB | 248,320 |

Training memory is dominated by the output logits, not the weights. The logits tensor is
`batch x sequence x vocab`; the loss path needs it plus its gradient. Float32 upper bound
for one sequence, before the gradient copy:

| Sequence length | Qwen2.5 (152k vocab) | Qwen3.5 (248k vocab) |
|-----------------|----------------------|----------------------|
| 2,048 | 1.25 GB | 2.03 GB |
| 4,096 | 2.49 GB | 4.07 GB |

Consequences:

- Batch size 1 with gradient accumulation, not batch size 4 (the mlx-lm default).
- `max_seq_length` 2,048 for both tasks. Narrative inputs are compact metric tables, never
  raw statement markdown (this also serves N1).
- Qwen3.5-4B saves weight memory but not logits memory; its training ceiling is close to the 9B's.

## 1.3 Memory budgets (planning estimates)

The Mac runs three session profiles (ADR 0008), selected with `configs/profiles/<name>.toml`.
`make demo` and `make train` run `fra-worker preflight` first and refuse to start when the
profile's thresholds are not met.

### Development session (editor, browser, Vite running)

| Consumer | Budget |
|----------|--------|
| macOS and background services | 4.0 GB |
| Editor and browser | 2.5 GB |
| `fra-api` (FastAPI, SQLite) | 0.3 GB |
| Vite dev server | 0.3 GB |
| Ingest child peak (docling Heron layout, TableFormer, ocrmac, batch size 2; measured up to 3.28 GB, spec 10) | 3.5 GB |
| Analysis worker peak (Qwen2.5-7B 4-bit, 8k KV, MLX cache capped at 1 GB) | 6.0 GB |
| **Serialized peak (N3 holds)** | **13.6 GB** |
| Unserialized peak (both heavy runtimes at once, not allowed) | 16.6 GB |

Both runtimes release memory between jobs (section 02, 2.2). `fra_worker.memory_guard` refuses
to start a heavy stage when system free memory is below `heavy_stage_min_free_gb` (5.0 in this
profile) and puts the job back in the queue.

### Demo session (all other apps closed)

| Consumer | Budget |
|----------|--------|
| macOS and background services | 4.0 GB |
| One browser window showing the app | 1.0 GB |
| `fra-api` serving the production web build (no Vite) | 0.3 GB |
| Ingest process with docling models loaded: idle / converting | 1.5 / 3.5 GB |
| Analysis worker with the model loaded: idle / generating | 4.5 / 6.0 GB |
| **Peak while converting** (model idle) | **13.3 GB** |
| **Peak while generating** (ingest idle) | **12.8 GB** |
| Headroom | 4.7 GB |

Both runtimes stay loaded, so no customer-facing step waits for a model to load. The heavy
lease still keeps their compute apart (N3). Preflight threshold: `demo_min_free_percent`
(initial 50, read from `memory_pressure`).

### Training session (all other apps closed)

API, workers, Vite and browser stopped; run under `caffeinate -i`. Preflight checks that no
service from this repository is running.

| Consumer | Budget |
|----------|--------|
| macOS and background services | 4.0 GB |
| QLoRA run: 4-bit base, adapters and optimizer state, checkpointed activations, logits and gradient at 2,048 tokens | 11.5 GB |
| Headroom | 2.5 GB |

Only if the Phase 0 smoke run fails at the default wired limit:
`sudo sysctl iogpu.wired_limit_mb=13000` (reverts at reboot). Record this in the run manifest.

## 1.4 Disk budget (44 GB free)

| Item | Budget |
|------|--------|
| Python environment (torch 2.14, transformers 5.17, docling, mlx) | 4 GB (measure) |
| `node_modules` | 0.6 GB |
| Base model kept after the bake-off | 4.3 to 6.0 GB |
| Second bake-off candidate, deleted after task 2.2 | up to 6.0 GB |
| docling layout and table models | 1 GB (measure) |
| SEC Financial Statement Data Sets, derived parquet only (quarterly zips streamed, then deleted) | 0.5 GB |
| Training splits and adapters (keep the last three) | 2 GB |
| Golden PDFs and rendered page images | 2 GB |
| `var/store` (least-recently-used pruning) | 5 GB |
| **Worst case during the bake-off** | **about 25 GB** |

Rules:

- Keep at least 15 GB free at all times (macOS swap grows on disk).
- Never download full-precision weights.
- Never run `mlx_lm.fuse --dequantize` on this machine, with one exception: the GGUF export of a
  fused adapter by the route in [14-run-profiles.md](14-run-profiles.md) ("Decision for week 3"),
  under the conditions below. Amended 2026-10-02 (decision D12 in
  [06-decisions-and-risks.md](06-decisions-and-risks.md)): the owner's words were "you can run
  it, but only very carefully". Every other use of `--dequantize` stays forbidden.
- `HF_HOME=var/hf` so every model cache is visible to `make disk-report`.
- `make disk-report` runs before any model or dataset download.

### Conditions for the GGUF export

The export dequantizes a 4-bit model to 16-bit weights, which the 18 GB machine has not been
shown to hold for the 7B. The owner's words were "only very carefully"; the conditions below are
the engineering meaning given to them, not the owner's own words. They bind the export of every
model other than the 0.5B spike model, the 7B first; rerunning the spike (its dequantized
directory is about 1 GB, measured in document 14) needs only the 15 GB floor. These conditions
are the only statement of how the export is run, and document 14 holds the sizes they refer to
and links here without repeating them. All must hold, and a run that breaks one is stopped.

1. **Rehearsal first.** Before the real adapter, the 7B route is run once with a throwaway
   adapter. The rehearsal records, in document 14: wall time; the size of each intermediate in
   bytes (from `stat`, not `du -h`); any `llama-quantize` format fallbacks; and peak memory of
   each step, including the load that verifies the f16 GGUF (condition 6). Peak memory is
   recorded as the "maximum resident set size" (bytes) that `/usr/bin/time -l` prints for the
   step. On this machine that field counted a Python child allocating 400 MB under a shell and
   under `uv run`, while "peak memory footprint" counted only the timed process itself (1.8 MB
   and 12 MB in those two cases), so the footprint line is not used for a wrapped command.
   Whether the resident set size counts the memory MLX holds through Metal is not verified, so
   the rehearsal also times the Python interpreter directly once for the dequantize step, with
   no `uv run` or shell in between, and records both fields. `sysctl vm.swapusage` is read
   before, during and after each step, and `sysctl kern.memorystatus_vm_pressure_level` once a
   minute during it. The peak memory of the 7B dequantize and convert on this machine is not
   measured yet.
2. **Disk preflight before every run.** Free space, read with `df` at the time of the run and
   saved to a log, is at least the peak size of the intermediates plus the 15 GB floor above. The
   peak is estimated in document 14 until the rehearsal measures it, and the measured peak then
   replaces the estimate there only. The reading that counts is the `df` taken before the run,
   not the "44 GB free" of this section's heading, which was a reading from earlier.
3. **Not while the bake-off's worst case is on disk.** The export does not start until the
   second bake-off candidate is deleted. Condition 2 then decides whether there is room.
4. **Nothing else heavy running** during the dequantize, convert, quantize and verification
   steps: no training run, no docling conversion, no `llama-server` other than the verification
   load itself, no Docker VM (R4 and R14 in [06-decisions-and-risks.md](06-decisions-and-risks.md)).
5. **Stop rule.** The run is stopped, the intermediates are deleted, and the result goes back to
   the owner before any retry, if the machine goes into sustained swap or free disk falls under
   the 15 GB floor. Until the rehearsal has measured it, "sustained swap" is provisional: the
   kernel's memory pressure level (`sysctl kern.memorystatus_vm_pressure_level`: 1 normal, 2
   warning, 4 critical; 1 and 2 have been read on this machine, 4 has not been seen), read once
   a minute, is 4 on two readings in a row. Swap used is not itself the test, because
   `vm.swapusage` already showed swap in use on 2 October with no export running. The
   rehearsal's record replaces this provisional criterion with thresholds it measured; no swap
   size in GB is fixed before then.
6. **Intermediates are deleted as soon as the next file is verified.** The dequantized directory
   goes once the f16 GGUF loads and agrees with MLX, and the f16 GGUF goes once the quantized
   file passes the acceptance rule in document 14. Each file's sha256 is logged before it is
   deleted.
7. **Every run is recorded in document 14:** date, free disk before and after, sizes, peak
   memory.

## 1.5 Library facts (verified 2026-09-11, pin exactly)

**Resolution.** docling 2.126.0, mlx 0.32.2, mlx-lm[train] 0.31.3, pandas 3.0.5,
matplotlib 3.11.2, FastAPI 0.141.1, Pydantic 2.13.5, pypdfium2 5.13.0 and ocrmac 1.0.1
resolve together on Python 3.12 for macOS arm64: 131 packages, including torch 2.14.0,
transformers 5.17.0, numpy 2.5.3, docling-ibm-models 4.0.2 and rapidocr 3.9.2.
One uv lockfile is viable; isolation happens at runtime (process boundaries), not in dependencies.
When resolving for another machine, set `MACOSX_DEPLOYMENT_TARGET=15.0`: uv otherwise assumes
macOS 13, and mlx publishes no wheels below 14.

**docling 2.126.0**

- `PdfPipelineOptions` defaults: `do_ocr=True` with `OcrAutoOptions`, which probes installed
  engines at runtime. Pin `OcrMacOptions(lang=["en-US"])` so runs are reproducible.
- `OcrMode` (`FULL_PAGE`, `LAYOUT_REGIONS`, `PDF_AWARE_LAYOUT_REGIONS`, `DEFAULT`) replaces
  the deprecated `force_full_page_ocr`.
- Table structure default is `TableStructureOptions` (TableFormer V1, `mode=ACCURATE`,
  `do_cell_matching=True`). `TableStructureV2Options` is available; its note warns that
  cell matching can break tables when PDF cells merge across columns. Task 1.9 compares the two.
- Layout default is Heron (`LayoutObjectDetectionOptions`); Egret presets trade speed for accuracy.
- `ocr_batch_size`, `layout_batch_size` and `table_batch_size` default to 4. Use 2 on this machine.
- `generate_page_images` defaults to `False`; enable it with `images_scale=2.0` for provenance crops.
- `DocumentConverter.convert(source, page_range=..., max_num_pages=..., max_file_size=...)`.
- The VLM pipeline ships an MLX spec, `GRANITEDOCLING_MLX` (`ibm-granite/granite-docling-258M-mlx`).
- Default PDF backend: `THREADED_DOCLING_PARSE`.

**docling-core 2.96.0**

- `TableItem.export_to_dataframe(doc=...)`.
- `TableItem.data: TableData` with `table_cells`, `num_rows`, `num_cols`, `grid`,
  `get_row_bounding_boxes()`, `get_column_bounding_boxes()`.
- `TableCell`: `text`, `bbox`, `row_span`, `col_span`, `start_row_offset_idx`,
  `end_row_offset_idx`, `start_col_offset_idx`, `end_col_offset_idx`, `column_header`,
  `row_header`, `row_section`.
- `ProvenanceItem`: `page_no`, `bbox`, `charspan`.
- `BoundingBox.coord_origin` is `TOPLEFT` or `BOTTOMLEFT`; normalize with
  `to_top_left_origin(page_height)` before storing.

**mlx 0.32.2.** `mx.set_wired_limit`, `mx.set_cache_limit`, `mx.set_memory_limit`,
`mx.get_peak_memory`, `mx.reset_peak_memory`, `mx.clear_cache`.

**mlx-lm 0.31.3**

- Training runs as QLoRA automatically when `--model` points to a quantized model.
- Defaults: `num_layers=16`, `batch_size=4`, `learning_rate=1e-5`, `max_seq_length=2048`,
  `grad_checkpoint=False`, `lora_parameters={rank: 8, dropout: 0.0, scale: 20.0}`.
  The key is `scale`; `alpha` raises an error.
- `--mask-prompt` (chat and completions formats only), `--grad-accumulation-steps`,
  `--resume-adapter-file`, optimizers `adam`, `adamw`, `muon`, `sgd`, `adafactor`.
- Data: `train.jsonl`, `valid.jsonl`, `test.jsonl` in chat, tools, completions or text format.
- **Sequences longer than `max_seq_length` are truncated with only a warning.** With prompt
  masking, a long prompt can truncate the entire completion away. Task 2.4 makes this a hard failure.
- LoRA targets every Linear, QuantizedLinear and Embedding module in the last `num_layers`
  blocks by default, so it is architecture-agnostic (Qwen3.5 hybrid layers included).
- `mlx_lm.fuse --dequantize`; GGUF export only for Llama, Mistral and Mixtral. The rule on
  running it is in 1.4.
- A `qwen3_5` model implementation is present.

**pandas 3.0.5.** Copy-on-Write and the dedicated string dtype are the defaults. Chained
assignment and object-dtype string assumptions from pandas 2 code will break.

**matplotlib 3.11.2.** Workers set `MPLBACKEND=Agg`. The macOS GUI backend must never load
in a worker process.

## 1.6 Model candidates

| Candidate | Role | Status |
|-----------|------|--------|
| `mlx-community/Qwen2.5-7B-Instruct-4bit` | Directive baseline. Smallest vocab, so the cheapest training logits; Apache-2.0 | Default |
| `mlx-community/Qwen3.5-9B-MLX-4bit` | Newer (2026-02), hybrid attention, small KV cache, larger vocab; Apache-2.0 | Bake-off |
| `mlx-community/Qwen3.5-4B-MLX-4bit` | Fallback if latency or memory gates fail | Bake-off, conditional |
| Qwen3.8-27B (16.05 GB weights), MoE models of 30B and above | None | Excluded: do not fit 18 GB |

**Decision rule (task 2.2).** Keep the baseline unless a candidate beats it by at least
2 points of normalization macro-F1 **and** its 20-iteration QLoRA smoke run at 2,048 tokens
peaks under 11.5 GB. Also record tokens per statement page for English and Arabic samples:
it sets context budgets for Phase 4.
