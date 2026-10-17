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
of the chat-templated sequence. No tokenizer adapter exists yet, because the pinned tokenizer is
not on disk; tests use a synthetic counter. A real run needs an adapter around the pinned
tokenizer and its chat template, and its `tokenizer_id` is recorded in the manifest.

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
- No tokenizer adapter and no command line entry point.
- No bag-of-words or bilingual-pair example generation.
- No semantic deduplication, period weighting or long-tail coverage policy.
