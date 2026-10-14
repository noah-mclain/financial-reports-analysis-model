# PDF review and gold labeling

## Fast assisted review

The owner selected repeated extraction plus a source map and one human PDF check as the
practical review route. Agreement alone is stability, never proof of accuracy. This method
is recorded as `assisted_review`, distinct from two independent PDF readings (`double_pdf`).
Human-reviewed extraction drafts are assisted gold, not a blinded independent benchmark.

Generate two fresh runs of a dev document with separate empty caches:

```sh
PYTHONPATH=eval uv run python -m harness.review_map capture \
  --id juhayna-2024-en-consolidated --runs 2 --out var/review/juhayna-fresh
```

Or compare saved snapshots of the same PDF (these are not automatically fresh/independent):

```sh
PYTHONPATH=eval uv run python -m harness.review_map build --id almarai-2025-en \
  --snapshot var/artifacts/source-sha/statements.raw.json \
  --snapshot var/other-run/source-sha/statements.raw.json --out var/review/almarai
```

Open `index.html`. Each cell links to its source PDF page, table, zero-based row and column;
a repeated label is told apart by its page, table and table row in the source, so a run that
omits an earlier repeat does not shift the later ones. Each record carries this as `position`
(page_no, table_ref, row), replacing the earlier `occurrence` ordinal; hand-built value
agreement evidence still uses its own `occurrence` field;
`map.json` includes exact PDF-point bounding boxes and original raw text from every snapshot.
Full inputs are copied into the packet: the viewer uses a statement/cell projection and may
not render ancillary fields from newer snapshot schemas. Source page images show highlighted
regions. Paragraph/line numbers are unavailable in the table contract; do not invent them.
Read the full PDF for omitted rows/statements, including items absent from every extraction.

Correct the editable values and taxonomy ids and check each against the PDF. Apply cell reviews
to the JSON editor, then correct labels, periods, units and omissions there. Enter your name,
confirm coverage and download the reviewed answers. No agreement automatically confirms a cell.
Checkboxes are not persisted: export before closing. Use the approve command to validate/freeze:

```sh
PYTHONPATH=eval uv run python -m harness.review_map approve \
  var/review/document-reviewed.json var/review/frozen/document-id.json
```

The capture/viewer currently accept dev golden ids only. This deliberately provides the owner
with visible review examples without creating an unlogged holdout look. Holdout review packets
and row-level predictions must stay in a separate annotation workflow; developers receive
aggregate scores, not answers for tuning. Existing dry-run reports discarded predictions and
cannot be retroactively scored. The optional dry-run gold integration below is ready to accept
frozen labels after annotation and uses the normal logged once-per-candidate holdout selection.

The holdout dry runs measured pipeline availability, not numeric or mapping accuracy. Gold
must be authored from the original PDF, including every printed row of the selected primary
statements, independently of extractor output. The development gold set remains in-sample.

## Readings and sign-off

1. Assign two distinct readers who do not view extractor predictions, draft expected files,
   each other's initial answers, or candidate score details. Read the PDF and taxonomy only.
   For holdout, readers supply answers for evaluation only and do not use those documents to
   implement fixes. Developers receive aggregate scores/failure classes only. Never annotate
   `model_test` or `blind` as part of this task; their use waits for the owner-run checkpoint.
2. Each reader writes a separate JSON reading in an ignored private directory under `var/`.
   Record the document id, SHA-256 of the original PDF, reader identity, `source: pdf_only`,
   and `complete: true` only after checking all selected primary statements and printed rows.
   This declaration is a human assertion, not a machine check of completeness or independence.
3. For each statement record type, one-based PDF pages, digital/scanned mode, scale, currency,
   full periods and every row in printed order. Each row contains label, values for every
   period (decimal strings before scale; null for a printed blank), and `canonical_id` from
   the taxonomy or null when no taxonomy item applies. Include missed statements/rows. Do not
   infer absent values, use arithmetic to invent values, or silently treat an illegible value
   as blank. Illegible/disputed answers remain unresolved and cannot be frozen.
4. Compare the two readings. Resolve disagreements against the original PDF; both readers
   approve the corrected answers. Exact agreement is required, including labels, metadata,
   periods and canonical ids. Two names alone cannot prove two independent readings.
5. Freeze before running the candidate. The output cannot overwrite an existing frozen file.
   Keep the original readings as audit evidence. Freeze files as `<document-id>.json` in a
   private gold directory. Do not commit holdout labels or PDF contents.

```sh
PYTHONPATH=eval uv run python -m harness.independent_gold   var/annotations/reader-a.json var/annotations/reader-b.json   var/holdout-gold/document-id.json
```

A reading has this shape (the example is synthetic and is not real gold):

```json
{
  "id": "synthetic",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "reader": "reader-a",
  "source": "pdf_only",
  "complete": true,
  "statements": [{
    "type": "balance", "pages": [5], "page_mode": "digital",
    "scale": 1, "currency": "EGP",
    "periods": [{"key": "2025-12-31", "end_date": "2025-12-31", "kind": "instant"}],
    "rows": [{"label": "Total assets", "values": {"2025-12-31": "5"},
              "canonical_id": "total_assets"}]
  }]
}
```

## Scoring boundary

```sh
PYTHONPATH=eval uv run python -m harness.dry_run --gold-dir var/holdout-gold
```

This remains a logged holdout look, once per frozen candidate. Missing gold, invalid readings
or a source hash mismatch refuse before consuming a look. All positive holdout documents
require gold; negative controls retain their existing sector judgement. No holdout run is
made merely to test this implementation: tests use synthetic fixtures.

Scoring happens before per-document artifacts are removed. A failed pipeline or missing
statement retains the full expected denominator and zero correct answers. Reports retain
aggregate counts, gold-file hashes, period and language breakdowns, not expected labels or
figures. Per-document gold counts are held in memory for the aggregate and are never written
to the rows file or the report, because they would show which holdout documents were read
correctly; a document row carries only its failure class.
A negative control with a gold file refuses the run before a look: controls are judged on the
sector decline and stay out of the numeric denominator.
Mapping accuracy compares independently assigned canonical ids; lexicon agreement is not gold.
A row counts as mapped when its label is read exactly or readably as the gold label, whether
or not its figures are right; a wrong figure is a numeric miss only. A blank is credited only
when the aligned row holds a cell for that period that was read empty and not flagged unsure.
A cell that failed to parse, was shifted, merged, spanned or placed without a box is wrong,
not blank, and so is a missing cell. When a statement type is extracted twice, no statement
is chosen for the numeric score, but the mapped rows and numeric rows of both count as
predictions and extra output.
Numeric thresholds are reused from extraction scoring. These counts do not establish every
component of Gate A/B, financial identities, narration accuracy, or generalization beyond
this labeled sample. A successful command exit still describes pipeline completion, not a
passing accuracy gate; inspect `gold_accuracy` in the saved report.

The next real accuracy run requires complete signed-off gold and the integrated candidate.
Previous reports cannot be retroactively scored: they intentionally discarded row-level
predictions. Use fit/dev examples for debugging failures found by the aggregate evaluation.


## Interpreting scores

Reports count `double_pdf`, `assisted_review` and unknown methods explicitly. Assisted or
mixed labels provide a human-reviewed observed accuracy, not an independent gate verdict;
`met` is null for these. An interrupted run preserves its observed counts but suppresses
full-run accuracy and gate verdicts. Extra predicted rows are reported separately. Even a
complete double-PDF numeric threshold pass is only one component of Gate A, not overall release
approval. Current code and report examples are development evidence, not a completed holdout
benchmark. No answers become gold until the human review or independent readings are complete.


## Reference coverage

Assisted approval defaults to `reviewed_packet_slots`. Such a packet may miss a row that every
extractor missed; it is suitable for the value-agreement diagnostic, and refused by the full
holdout scorer before a look. Only after checking all source primary statements, rows and periods
(including omissions) may the owner pass `--full-source` to approval. Two-reading frozen files
default to full-source scope; that remains a reader declaration, not automatic proof of recall.

The Juhayna packet report and reproducible dev evidence are in
[the value-agreement report](../exec-summaries/juhayna-value-agreement-2026-10-12.md).
