# Normalization dataset builder

`training/dataset_builder.py` turns the fit and validation parts returned by
`partition_examples` (see [normalization-dataset-contracts.md](normalization-dataset-contracts.md))
into the `train.jsonl` and `valid.jsonl` that `mlx_lm` reads, plus a manifest. It renders and
audits rows and writes the output files. It loads the canonical taxonomy resource for the
shared prompt; the command owns reading the input records. It calls no network and loads no model.

## What it does

1. `dedupe_per_issuer` applies the D18 rule per part: examples with the same issuer (the split's
   CIK key), statement, normalized label and target collapse to the lowest `example_id`. The
   label is normalized with NFKC, casefold and collapsed whitespace, so case, spacing and Arabic
   presentation forms do not make a distinct example. Deduplication never crosses statements or
   issuers. One issuer label that maps to two different targets (or to a target and an abstention)
   raises `DatasetError` naming the example ids.
2. `render_row` writes one chat row per example: a system message (`SYSTEM_PROMPT`, the single
   source of the instruction), a user message and an assistant message.
   - User: `{"label": ..., "statement": ...}` as JSON with sorted keys and no escaping of
     non-ASCII text. Nothing else is in the prompt: no period or months, no numbers, no
     neighbouring rows. The shared system message supplies the allowed ids by statement
     and the response schema; the per-example target never changes that catalog.
   - Assistant: `{"canonical_id": <id>}`, or `{"canonical_id": null}` for an abstention.
3. `build_dataset` deduplicates and renders both parts, then audits lengths. `mlx_lm` truncates a
   sequence over `max_seq_length` silently, so any row above `token_limit` (default 2048) raises
   `DatasetError` listing every offending example id and count. Rows are never truncated or
   dropped by the length audit. An empty fit or validation part after deduplication is an error.
4. `write_dataset` writes `train.jsonl` (fit), `valid.jsonl` (validation) and `manifest.json`,
   all UTF-8 and ordered by `example_id`. It refuses to write if any of the three files, or a
   `test.jsonl`, already exists in the directory.

## Length audit

`count_tokens` is an injected callable: it receives the message list and returns the token count
of the chat-templated sequence. `training/chat_tokenizer.py` provides it:
`load_chat_counter(tokenizer_dir)` loads a tokenizer from a local directory with no network,
refuses a directory without `tokenizer.json`, `tokenizer_config.json` or a chat template, and
counts `apply_chat_template(messages, tokenize=True)` with no generation prompt, because the
training sequence ends with the assistant turn. It also returns the tokenizer identity recorded
as `tokenizer_id`: a SHA-256 over the contents of the tokenizer files present
(`tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`, `special_tokens_map.json`,
`added_tokens.json`, `vocab.json`, `merges.txt`), never a path.

The Qwen2.5-7B-Instruct tokenizer files must be in a local directory. The measured real SEC
build below records its tokenizer identity; tests also use a tiny synthetic tokenizer.

## Command

    make dataset-build EXAMPLES=examples.jsonl SOURCES=sources.jsonl APPROVED=approved.txt \
        TOKENIZER_DIR=path/to/tokenizer OUT_DIR=path/to/out [MAX_PER_ISSUER=n]

- `EXAMPLES`: JSONL, one `NormalizationExample` (`model_dump(mode="json")`) per line.
- `SOURCES`: JSONL, one `SourceRecord` per line, the authoritative train roster.
- `APPROVED`: text file of example `content_sha256` values, one per line; blank lines and `#`
  comments are ignored. This file is the caller's trust boundary: the command only checks that
  each example's hash is listed and cannot tell whether anyone reviewed it.
- The pool registry, holdout moves and scoring log default to the repository's files
  (`var/issuer-pools.json`, `eval/corpus/holdout_moves.yaml`, `eval/corpus/scoring_log.tsv`);
  `--registry`, `--moves` and `--looks` override them when running the script directly.

It runs `partition_examples`, `build_dataset` (token limit 2048) and `write_dataset`, prints the
row counts and the manifest path, and exits nonzero with the error message on a refusal. A
malformed input line is reported with its file and line number.

## Manifest

`DatasetBuildManifest` records the upstream `DatasetManifest`, builder version, SHA-256 of the
system prompt, tokenizer id, token limit, row counts before and after deduplication per part,
length statistics per part (count, max, mean as a decimal string, nearest-rank p95), counts per
`source|language|period` stratum per part, and the SHA-256 of the exact `train.jsonl` and
`valid.jsonl` bytes.

## Holdout

The builder accepts only `PartitionedExamples`, built by `partition_examples`, which refuses
holdout sources. It checks both parts against the digests in that result's manifest and refuses
a mismatch, and it has no code path that writes `test.jsonl`. The check is not authenticated: a
result constructed by hand with a self-consistent manifest would pass it.
Writes are not atomic: a failure part-way leaves earlier files behind, to be removed by hand.

## What it does not do

- The measured real dataset is SEC English only. It supplies no PDF or Arabic evidence.
- No bag-of-words or bilingual-pair example generation.
- No semantic deduplication, period weighting or long-tail coverage policy.

## Monitoring a run

`dataset-build` reports input reading, contract validation and partitioning, token counting,
and train/valid writing on stderr. A terminal shows an in-place progress bar; redirected stderr
and CI logs show timestamped stage and progress lines.

Each stage writes a start line and a finish line with its elapsed time; stages with a countable
total (reading, line validation, contract validation and partition, rendering, token counting)
also log count, rate and ETA at most every 30 s. Timestamps are UTC (`Z`).

## Measured: first real SEC run (2026-10-18)

These measurements describe builder version 1 and its original prompt. Version 2 below changes
the prompt, so its token statistics and file hashes must be measured separately.

Inputs: the 811,629 examples of `sec-examples` over eight quarterly archives (2024q3 to 2026q2),
the `TOKENIZER_DIR` for Qwen2.5-7B-Instruct fetched from `main` (not a pinned revision), no
`--max-per-issuer`. Measured with `dataset-build` on the author's Mac:

| | fit | validation |
| --- | --- | --- |
| rows | 672,895 | 138,734 |
| `sec\|en\|annual` | 217,185 | 45,056 |
| `sec\|en\|interim` | 455,710 | 93,678 |
| mean / p95 / max tokens | 63.1 / 99 / 325 | 63.0 / 98 / 262 |
| abstain rows | 80.6% | 80.7% |
| canonical ids present | 44 | 44 |
| fewest rows for one id | 363 (`investments_in_associates`) | 76 (`investments_in_associates`) |

No row came near the 2048-token limit and none was dropped. None of the 4 recorded moves
applies to this roster (`moves applied: 0 of 4 recorded`), so the split is the plain issuer
hash. The thinnest ids in fit after `investments_in_associates` are `short_term_borrowings`
(457) and `short_term_investments` (484). These are counts, not accuracy: no model has been
trained or scored on this dataset, and it has no PDF or Arabic rows.

Run time: 1h44m before the roster check was fixed to use one registry clone for the whole
roster (it rebuilt the registry once per source); 5m30s after, with byte-identical
`train.jsonl`, `valid.jsonl` and `manifest.json`.

## Version 2: explicit normalization output contract

The first local unadapted-model smoke returned prose on all 16 selected validation prompts;
the scorer correctly rejected all 16. Version 1's instruction did not specify the expected
JSON format or list the canonical ids, so this demonstrated an output-contract failure rather
than an estimate of normalization ability.

Version 2 generates one shared system prompt from the taxonomy and `NormalizationAnswer`
schema. It supplies allowed ids by statement, instructs null for unsupported statements or
unmatched labels, and requires JSON without explanation or markdown. Dataset rendering and
MLX inference import the same prompt and response schema. A partition with a different
taxonomy fingerprint is refused. No example's target, issuer or period changes the prompt.

Build version 2 into a new directory and rerun the length audit. Do not overwrite the original
dataset. Earlier prompt/file hashes remain evidence of the original build; validation
selections must be reconstructed against the new manifest before comparing base and adapter.

Measured on 2026-10-19 with the same SEC inputs and tokenizer identity as version 1:

| | fit | validation |
| --- | --- | --- |
| rows retained | 672,895 | 138,734 |
| mean / p95 / max tokens | 380.099 / 416 / 642 | 380.044 / 415 / 579 |

The upstream partition manifest is identical to version 1. No row was truncated or dropped.
The system prompt SHA-256 is
`bba5f9cdf6ea3e553c9b899a0d4419128e84f6255f73870dba2090ded1bc0606`;
train and validation SHA-256 values are
`87d2254a8e52ae2b2073257302c354a20c18c1b1fdc684e04f64f5e221e29957` and
`0e3e8c479c586b15bb78ec38b291a1c583ddb1bb4a598c44d29485ff4a98d2e7`.
The separate build took 539.31 seconds. Its timing wrapper exited 1 after writing all three
files because sandboxed `sysctl kern.clockrate` was refused; no peak-memory measurement is
claimed from that wrapper. The validation preparation separately checks file hashes and
reconstructs every rendered row before accepting the dataset.

## Frozen train equity exclusions

`training/dataset_exclusions.py` freezes exact `messages[1].content` strings that have both
`equity_attributable_parent` and `total_equity` targets in train. Validation targets never
select keys. `build_dataset.py --exclusions` applies that stored list to train, including
matching null and other targets; it never reselects keys. Builder version, system prompt,
upstream dataset hashes, tokenizer identity and both source file hashes must match. Removal
counts, target breakdown and retained row count must reconcile or raise `DatasetError`.
Validation bytes stay unchanged. The optional manifest `exclusion` audit records the key-list
hash, source train hash, before/after counts and removed targets; `train_sha256` describes
the filtered bytes. Without exclusions, manifest serialization is unchanged.

A read-only dry run on 2026-10-19 against `dataset_sec_4_v2` measured 35 selected keys,
997 matching train rows (216 parent equity, 649 total equity, 130 null and 2 total liabilities
and equity), leaving 674,930 of 675,927 train rows. The canonical sorted key-list SHA-256 is
`439bc67ab2d09af7a475d809923690b47719b6646a210cfbf3dbe14e51406109`.
Source train/validation and example hashes matched the task's pinned artifacts; the tokenizer
identity matched `9e3c11a4a87d85a72b6f2d1e4b29ab88532f72d03189b6fa2793da735a2d98aa`.
These examples are consumed as hash-bound decoder artifacts; this change does not independently
reproduce `sec_examples_4`. Dropping conflicting supervision does not teach abstention or
establish a clean dataset. Version 1 leaves broader label-versus-null discordance (scope B)
unresolved; it applies no global discordance filter.

The coordinator runs the commands below outside the worker sandbox. They use the existing
local environment, create new outputs and refuse overwrites. No real exclusion file, rebuilt
dataset or population report was produced by the dry run. Run from the assigned checkout:

```sh
cd /Users/nadamohamed/.herdr/worktrees/financial-reports-analysis-model/equity-train-exclusion
V=/Users/nadamohamed/Developer/GitHub/financial-reports-analysis-model/var
PYTHON_BIN=/Users/nadamohamed/Developer/GitHub/financial-reports-analysis-model/.venv/bin/python
export PYTHONPATH=training:scripts:eval:packages/core/src:packages/ingest/src

"$PYTHON_BIN" training/dataset_exclusions.py \
    --dataset-dir "$V/dataset_sec_4_v2" \
    --out "$V/dataset_sec_4_v2_exclusions/exclusions.json" \
    --provenance 'Consumes hash-bound sec_examples_4 decoder artifacts; does not independently reproduce them.'

"$PYTHON_BIN" training/build_dataset.py \
    --examples "$V/sec_examples_4/examples.jsonl" \
    --sources "$V/sec_examples_4/sources.jsonl" --approved "$V/sec_examples_4/approved.txt" \
    --registry "$V/issuer-pools.json" --moves eval/corpus/holdout_moves.yaml \
    --looks eval/corpus/scoring_log.tsv \
    --tokenizer-dir /private/tmp/normalization-validation/var/models/qwen2.5-7b-instruct-4bit \
    --exclusions "$V/dataset_sec_4_v2_exclusions/exclusions.json" \
    --out-dir "$V/dataset_sec_4_v3"

"$PYTHON_BIN" training/normalization_validation.py prepare \
    --dataset-dir "$V/dataset_sec_4_v2" \
    --examples "$V/sec_examples_4/examples.jsonl" \
    --sources "$V/sec_examples_4/sources.jsonl" --approved "$V/sec_examples_4/approved.txt" \
    --registry "$V/issuer-pools.json" --moves eval/corpus/holdout_moves.yaml \
    --looks eval/corpus/scoring_log.tsv \
    --exclusions "$V/dataset_sec_4_v2_exclusions/exclusions.json" \
    --out-dir "$V/normalization-validation-sec4-equity-populations"
```

The last command runs only the lexicon. Its `report.json` preserves the full validation score
and adds three labelled `populations`: full validation for continuity, validation excluding
ambiguity (a different, non-comparable population), and ambiguity-only diagnostics with
per-input targets and predictions. Fixture tests verify membership and full-score continuity.
The coordinator must check the real 173 ambiguity rows / 21 inputs and compare the full
lexicon result with the existing `normalization-validation-sec4/RECORD.txt` baseline
(accuracy 85.35%, macro-F1 42.08%); these real population results remain unverified here.

## Frozen training-input consistency exclusions (artifact version 2)

The optional `training_input_consistency` criterion keeps the existing builder and manifest
contracts. It freezes `(input_key, target-or-null)` removals, where `input_key` is the entire
exact `messages[1].content` string. Parsing validates the label, statement and target without
re-rendering the key. It never relabels, deduplicates or creates rows.

All three policy parameters are explicit; there are no defaults. First carry the supplied
version 1 equity removals, then group the remaining train rows. Remove rows outside the selected
statement types. For each selected group:

- Retain every row if all targets are null.
- Excluding nulls from the denominator, quarantine the whole group (including nulls) if the
  largest mapped share is below `dominance` or there is a tie for the largest mapped count.
- Otherwise retain only null rows when their share of all group rows is at least
  `null-dominance`; retain only the top mapped target otherwise. Remove all other rows.

Threshold comparisons are exact, including equality. The artifact records policy parameters,
the source identities, the supplied equity artifact's byte hash and embedded v1 selection,
and disjoint pair counts/reasons (`equity`, `statement`, `ambiguous`, `outlier`). Equity takes
precedence so a row is counted once. For v2, `keys_sha256` binds the complete artifact content
except that hash field, including policy and pair removals; for v1 it remains the original
sorted key-list hash. These integrity hashes are not evidence of annotation truth.

V2 freeze reads only train rows, the build manifest and the supplied equity artifact. The
validation hash is carried from the bound manifests; freeze does not open validation or any
example/provenance records. Application still checks source identity, pair coverage, per-pair
and target counts, and before/after accounting. Version 1 selection, validation and whole-key
application remain available with the original CLI. Both versions produce one unchanged
`ExclusionAudit`; the existing `build_dataset(..., exclusions=...)` path recomputes retained
lengths, strata, row count and train hash from the original examples and injected tokenizer.
Validation bytes remain unchanged.

After independent diff review and all four required make checks, the coordinator may freeze
from the **unexcluded** source with the owner-selected parameters below. Reuse the environment
variables from the commands above, from the `fit-consistency-projection` checkout:

```sh
"$PYTHON_BIN" training/dataset_exclusions.py \
    --dataset-dir "$V/dataset_sec_4_v2" \
    --equity-exclusions "$V/dataset_sec_4_v2_exclusions/exclusions.json" \
    --criterion training_input_consistency \
    --statements income,balance --dominance 0.95 --null-dominance 0.90 \
    --out "$V/dataset_sec_4_consistency_exclusions/exclusions.json" \
    --provenance 'Training-input consistency policy; not independently verified financial truth.'
```

Then rerun the existing `build_dataset.py` command with the original example/approval/source
inputs, this new artifact as `--exclusions`, and a new output directory. Do not freeze against
the already equity-filtered v3 dataset: the combined artifact binds the unexcluded source.
Combined retained rows must equal applying the policy after v1 equity removal.

No real v2 policy counts or build results have been measured yet. Synthetic tests establish
policy/application behavior only; the immutable real v1 byte regression and real v2 freeze,
build and reconciliation remain coordinator checks. This is a training-input consistency
choice. For example, dominant training mappings may retain `total_assets` despite custom-tag
null drafts; the policy does not integrate or certify the separate source-evidence gate.
