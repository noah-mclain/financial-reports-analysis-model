# Juhayna source review: value agreement and remediation

Date: 12 October 2026. Disposition: development diagnostic; extraction quality remains below
the scanned-page target. This report does not approve release, a holdout score or a gate.

## Result and metric definition

The two fresh extraction runs agree on all 140 observed cells. Against the owner's corrected
review packet, **97 of 124 selected cells agree (78.23%)** in each run. Agreement between runs
is repeatability; agreement with the reviewed answers exposes shared errors. There are **27
mismatches**, and no improvement from repeating the same pipeline with separate empty artifact
roots. One process and OCR engine were reused; the runs are not independent reading methods.

The numerator counts exact Decimal equality at a reviewed packet row/period slot. The denominator
includes every selected reference cell, including a missing extraction. A printed dash means
zero under `fra_core.numbers`; an empty cell remains distinct. Four dashes entered by the reviewer
were normalized to zero using that rule. Signed values are compared before currency scale.

This is a **value-only slot-agreement diagnostic**, not source-aligned extraction accuracy.
The slot association comes from the review packet, and four mismatches concern row association
rather than a pure wrong numeral. No claim is made about omitted source rows, independent label
selection, correct periods, currency, scale, mapping accuracy, other filings, Arabic, interim
statements, `model_test` or `blind`. The labels were assisted by extractor drafts and checked by
one named reader. The labels remain in-sample (`dev`), explicitly scoped to `reviewed_packet_slots`; the full
holdout scorer refuses this subset before logging a look. A binomial interval would not remove
these selection, shared-recognition and source-association limitations.

| Statement | Right / selected cells | Agreement | Mismatches |
|---|---:|---:|---:|
| Balance sheet, PDF page 5 | 51/76 | 67.11% | 25 |
| Comprehensive income, PDF page 7 | 10/12 | 83.33% | 2 |
| Income statement, PDF page 6 | 36/36 | 100.00% | 0 |
| Total | 97/124 | 78.23% | 27 |

The 100% income result is limited to the 36 selected cells; it does not establish statement
selection, row recall or label/period accuracy. Of the original 140 observed cells, 12 belong
to additional candidates outside the selected first statement of each type. Another four
cells are repeated printed attribution totals and are excluded once: review cells 101–102
repeat 95–96; 137–138 repeat 131–132. These are printed duplicates, not fabricated rows.

| Reference period key | Right / selected cells | Agreement |
|---|---:|---:|
| 2024-12-31 | 21/38 | 55.26% |
| 2023-12-31 | 30/38 | 78.95% |
| FY2024 | 24/24 | 100.00% |
| FY2023 | 22/24 | 91.67% |

## Why the 27 slots disagree

Primary classes below are disjoint and sum to 27. They describe observed evidence, not a
controlled attribution of blame to an OCR library. A cell can have secondary problems too.

| Primary evidence class | Cells | Evidence | Interpretation |
|---|---:|---|---|
| Recognition-layer digit discrepancy | 16 | The captured raw text already contains the wrong digits, and those digits survive number parsing. Fifteen of these cells have no flags; one has `digit_suspect`. | Upstream recognized text disagrees with the reviewed source. The leaf evidence does not isolate the page OCR, conversion OCR, rendering or segmentation stage responsible. |
| Lost printed dash | 4 | Expected zero, extracted null; raw text is empty. All four have synthesized boxes and `numbers_missing`; two additionally say `blank_confirmed`. | The source dash was not preserved in the numeric cell. The number parser's dash-as-zero rule cannot recover a symbol it never received. Recognition versus grid assignment is not isolated. |
| Trailing nonnumeric mark | 1 | Raw `7 537 057 211/`; extracted null with `unparsed`. | A stray slash prevented parsing. Treating every slash as disposable would be unsafe for fractions or other genuine slash-bearing text. |
| Two amounts merged into one cell | 2 | Deferred-income and current-liability totals appear in a single whitespace-separated string. The parser creates a very large integer and flags `implausible_magnitude`. | Cell/row segmentation is wrong. Whitespace grouping is valid for financial numbers, so parsing cannot safely infer where one amount ends. |
| Row association / review slot repair | 4 | Current-liability slots hold total-liability values (`label_merged`). The next original slots are from the combined liabilities/equity footer, but review reassigns them to total liabilities. | This mixes a confirmed producer row merge with reference-slot reassignment. These four counts must not be presented as four isolated OCR numeral errors. |

Excluding only the four row-association/reassigned slots gives **97/120 (80.83%)** for the other
slots. This is a sensitivity calculation, not an alternative passing score or an independence
claim. The merged-amount cells remain in that denominator.

### Recognition examples and flags

PPE 2024 reads `3 856 899 018` instead of reviewed `3 886 899 018`; bank credit facilities 2024
read `1 909 495 624` instead of `2 909 495 624`. Other differences are single digits near the
end of an amount. Repeating the same pipeline reproduces these digits, so a stable value can
still be wrong. The two runs do not test a different engine or preprocessing method.

### Row topology and reference coverage

The current-liability total was absorbed into the preceding deferred-income cell. The extracted
row called `Current Viabilitica Total Gabilities` then holds the next total's values. The owner
repaired the packet's current-liability and total-liability slots. Their original prediction
boxes still refer to the original extracted rows. The corrected reference does not separately
include the final combined liabilities/equity footer. Restore the full source row sequence and
independent reference positions before using this sample to make a row-recall or source-aligned
gate claim. Do not derive or insert a gold number from an accounting identity.

## Cell-level mismatch inventory

Values are before scale. “Missing” means null, not zero. PDF pages are one-based. Prediction
bounding boxes, raw strings, source table/row/column indices and flags are retained in the
linked evidence JSON; they identify where the candidate read, not proof of reference alignment.

| # | PDF page | Reviewed label | Period | Extracted | Reviewed | Primary class |
|---|---:|---|---|---:|---:|---|
| 1 | 5 | Property, plant and equipment | 2024-12-31 | 3,856,899,018 | 3,886,899,018 | recognition_digits |
| 2 | 5 | Equity accounted investees | 2024-12-31 | 18,025,352 | 18,025,552 | recognition_digits |
| 3 | 5 | Right of use assets | 2023-12-31 | 38,965,498 | 38,966,498 | recognition_digits |
| 4 | 5 | Other long term asset | 2023-12-31 | 707,325 | 707,329 | recognition_digits |
| 5 | 5 | Non-current assets | 2024-12-31 | 6,053,405,411 | 6,053,405,413 | recognition_digits |
| 6 | 5 | PPE held for sale | 2024-12-31 | missing | 0 | lost_dash |
| 7 | 5 | Current assets | 2024-12-31 | missing | 7,537,057,211 | trailing_mark |
| 8 | 5 | Total assets | 2024-12-31 | 13,590,052,624 | 13,590,462,624 | recognition_digits |
| 9 | 5 | Legal reserve | 2024-12-31 | 742,112,953 | 742,112,963 | recognition_digits |
| 10 | 5 | General reserve - issuance premium | 2024-12-31 | 330,920,418 | 330,920,428 | recognition_digits |
| 11 | 5 | General reserve - issuance premium | 2023-12-31 | 330,920,438 | 330,920,428 | recognition_digits |
| 12 | 5 | Retained earnings | 2024-12-31 | 4,324,257,519 | 4,324,257,529 | recognition_digits |
| 13 | 5 | Non-controlling interests | 2024-12-31 | 628,458 | 628,468 | recognition_digits |
| 14 | 5 | Non-current liabilities | 2024-12-31 | 1,051,729,353 | 1,051,729,253 | recognition_digits |
| 15 | 5 | Provisions | 2024-12-31 | 238,224,163 | 228,224,163 | recognition_digits |
| 16 | 5 | Bank credit facilities | 2024-12-31 | 1,909,495,624 | 2,909,495,624 | recognition_digits |
| 17 | 5 | Bank credit facilities | 2023-12-31 | 1,096,229,655 | 1,096,229,656 | recognition_digits |
| 18 | 5 | Creditors and other credit balances | 2023-12-31 | 3,261,080,629 | 2,261,080,629 | recognition_digits |
| 19 | 5 | Due to related parties | 2024-12-31 | missing | 0 | lost_dash |
| 20 | 5 | Deferred income | 2024-12-31 | 38,234,566,199,408,901 | 3,823,456 | merged_amounts |
| 21 | 5 | Deferred income | 2023-12-31 | 76,438,563,858,564,779 | 7,643,866 | merged_amounts |
| 22 | 5 | Current liabilities | 2024-12-31 | 7,251,138,154 | 6,199,408,901 | row_association |
| 23 | 5 | Current liabilities | 2023-12-31 | 4,330,598,541 | 3,858,564,779 | row_association |
| 24 | 5 | Total liabilities | 2024-12-31 | 13,550,452,624 | 7,251,138,154 | row_association |
| 25 | 5 | Total liabilities | 2023-12-31 | 8,580,367,133 | 4,330,598,548 | row_association |
| 26 | 7 | Losses of foreign currency evaluation (EAS 13 - appendix H) | FY2023 | missing | 0 | lost_dash |
| 27 | 7 | Tax impact (EAS 13 - appendix H) | FY2023 | missing | 0 | lost_dash |

## Remediation, in order

| Priority | Work | Why | Acceptance evidence |
|---|---|---|---|
| 1 | Repair numeric cell boundaries and the sequence of current liabilities, total liabilities and combined liabilities/equity. Give every recovered value its own printed region and row. | Two merged cells and four association slots account for six failures and can silently attach plausible values to the wrong meaning. | On this dev PDF, each of the three totals has a separate row with matching page/region and the correct period; deferred income is separate. Test missing and misleading labels. Rebuild the reference's source links and account for the omitted combined footer. |
| 2 | Re-read suspect regions with enlarged crops and controlled preprocessing or another actual recognition method. Retain disagreement and quality flags. | Sixteen raw digit discrepancies remain stable across repeats; fifteen are unflagged. | Compare alternatives on permitted dev/fit material with source-reviewed values, exact configurations, candidate/source hashes and distinct cache roots. Report per-cell outcomes and end-to-end changes; hold unresolved disagreements for review. |
| 3 | Preserve printed dash cells at the grid producer and recheck crops currently declared blank. | Four printed zeros became missing values, including two misleading `blank_confirmed` outcomes. | A printed dash yields zero with source evidence; a true blank remains null; an uncertain/synthetic region remains held. Exercise both states and language digit forms. |
| 4 | Handle trailing recognition artifacts only with numeric-column and source confirmation. | One slash-contaminated amount is rejected rather than guessed. | The confirmed stray slash can be resolved; real fractions, dates and uncertain symbols remain rejected or separately interpreted. Never globally strip punctuation to improve the score. |
| 5 | Correct labels and taxonomy coverage without modifying values. | 29 labels were repaired by the owner-guided import; 36 of 62 reviewed rows have current catalog equivalents and 26 do not. Numeric agreement is independent of this subset's mapping rate. | Canonical IDs are validated within statement type. Preserve unsupported labels and explicit gaps. Do not force net finance into gross finance or unspecified EPS into basic/diluted EPS. Review semantic additions before broadening the taxonomy. |
| 6 | Evaluate the integrated candidate and extend reviewed coverage. | The captured candidate predates separate renderer, cell-splitting and statement-source work. | Re-run dev after integration; distinguish changes by class. Check scanned, digital, language and period strata separately. Use the normal logged holdout procedure and independently completed references; keep `model_test` and `blind` closed until the owner checkpoint. |

Separate changes already in progress may address parts of this list: `scan-blank-cells` targets
merged amounts; `statement-source-guard` targets statement-source selection; `week2-close`
includes an English scan renderer. They are not in this captured candidate, so this report
claims no benefit from them. Locate provenance improves reproducibility, not numeric accuracy
by itself. No numerical extraction fix is included in this reporting branch.

## Proposed follow-up commit subjects

These are planned follow-ups, not changes claimed by this reporting branch. Use the owning
worktrees and reuse the existing cell-splitting and source-guard work before creating duplicates.
Each implementation should include its focused regressions and source-reviewed acceptance checks.

1. `fix(ingest): preserve row boundaries around liability totals`
2. `feat(ingest): reread suspect numeric crops and retain disagreements`
3. `fix(ingest): preserve printed dash cells as evidenced zeroes`
4. `fix(ingest): resolve source-confirmed trailing recognition marks`
5. `feat(core): add reviewed statement-specific taxonomy items`
6. `test(eval): score integrated extraction against source-aligned references`

Splitting the 2023 deferred-income cell alone will not finish that case: its first recognized
amount is `7 643 856`, while the reviewed amount is `7 643 866`. The reassigned 2023 liability
slot also contains a last-digit discrepancy, and the original 2024 combined footer has corrupted
digits. Primary classes are kept disjoint for prioritization; recovery must check secondary
recognition problems after restoring the row topology.

## Reproduce and interpret

```sh
PYTHONPATH=eval uv run python -m harness.value_agreement
```

The command reads the checked-in public dev evidence and frozen assisted review; it makes no
OCR/model request or corpus look. It checks the reference hash, identity and full selected-cell
coverage and calculates each run's counts. Missing/duplicate evidence and unclassified mismatches
fail loudly. Raw capture hashes distinguish the original local snapshots; the compact evidence
retains the observations used here. Reproducing the report is different from rerunning extraction.

- [Reviewed answers](../../eval/golden/reviewed/juhayna-2024-en-consolidated.gold.json)
- [Observed values, raw text, source positions and cause annotations](../../eval/golden/reviewed/juhayna-2024-en-consolidated.values.json)
- [Annotation and review workflow](../runbooks/independent-gold-labeling.md)
- [Canonical taxonomy](../../packages/core/src/fra_core/taxonomy/canonical_items.yaml)

Capture base: `a4ba91d8f89672b2d8e9952c43642483658a3b6b`; structure version 19 in both runs.
Document SHA-256: `4c52b27b92e8de962ad49618aa0a7429f36e6324fd864ee2321b8c4222f1e953`.
The evidence stores both snapshot hashes and settings hashes, and the frozen-reference hash.
The canonical terminology crosswalk uses this repository's version-1 catalog. The
[IFRS Accounting Taxonomy 2024](https://www.ifrs.org/issued-standards/ifrs-taxonomy/ifrs-accounting-taxonomy-2024/)
was a terminology reference, not a claim that this EAS filing or internal IDs are IFRS tags.

The separate stricter scorer reported **46/124 (37.10%)** because it also requires row/label
alignment and the value match. That is a different estimand; it is not a second estimate of
value-only agreement. No complete Gate A/B verdict follows from either score. The review workflow
explicitly labels assisted results and suppresses independent `met` verdicts for assisted or
incomplete runs. Passing software tests confirms harness behavior, not this filing's accuracy.
