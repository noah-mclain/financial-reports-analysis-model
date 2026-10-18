# Normalization dataset builder

`training/dataset_builder.py` turns the fit and validation parts returned by
`partition_examples` (see [normalization-dataset-contracts.md](normalization-dataset-contracts.md))
into the `train.jsonl` and `valid.jsonl` that `mlx_lm` reads, plus a manifest. It holds pure
functions and one writer. It reads no files, calls no network and loads no model.

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
     neighbouring rows and no list of valid ids.
   - Assistant: `{"canonical_id": <id>}`, or `{"canonical_id": null}` for an abstention.
3. `build_dataset` deduplicates and renders both parts, then audits lengths. `mlx_lm` truncates a
   sequence over `max_seq_length` silently, so any row above `token_limit` (default 2048) raises
   `DatasetError` listing every offending example id and count. Rows are never truncated or
   dropped. An empty fit or validation part after deduplication is an error.
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

The owner must place the Qwen2.5-7B-Instruct tokenizer files in a local directory. None is on
disk now, so the audit has run only against a tiny synthetic tokenizer in tests, not on real
tokens.

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

- No real dataset can be built yet: no reviewed crosswalk, approvals or independent bilingual
  annotations exist, so it has run on no real data and no counts are claimed.
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
