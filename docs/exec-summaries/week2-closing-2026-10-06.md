# Week 2 closing note — 6 October 2026

## Disposition

The planned second holdout dry run completed once on candidate `7618503` (35 of 35 documents, exit 0, source revision `7618503c3ec54ec287264367126bccbedf03e4e5`, clean source tree). Completion means the pipeline produced all rows; it does not mean Gates A or B passed. Gate A and Gate B remain **NOT MET** on `dev`. On both holdout runs their accuracy dispositions are **NOT MEASURED**: the holdout reports contain no independently verified gold cells, scales, currencies or mapping labels. The reported holdout counts below are pipeline availability and review signals, not correctness scores.

No `model_test` score or `blind` run was made (D13/D17). This note does not approve the checkpoint or publication. The checkpoint remains after all planned work and checks pass.

## Gates A and B by period

`dev` is measured against the golden expectations. Holdout A/B dispositions are not measured, even where proxies are available. `unknown` is an empty stratum in all supplied reports.

| Evidence | Period | Documents | Gate A | Gate B |
|---|---:|---:|---|---|
| dev | annual | 12 | **NOT MET** | **NOT MET** |
| dev | interim | 0 | NOT MEASURED (empty) | NOT MEASURED (empty) |
| dev | unknown | 0 | NOT MEASURED (empty) | NOT MEASURED (empty) |
| dev | total | 12 | **NOT MET** | **NOT MET** |
| 4 Oct holdout | annual / interim / unknown / total | 5 / 30 / 0 / 35 | NOT MEASURED in all strata | NOT MEASURED in all strata |
| 6 Oct holdout | annual / interim / unknown / total | 5 / 30 / 0 / 35 | NOT MEASURED in all strata | NOT MEASURED in all strata |

The `dev` Gate A evidence is 138/138 digital numeric cells, 218/254 scanned cells (85.83%), 4 identity checks `ok`, 1 failed-but-explained, 4 skipped and 3 documents without a balance sheet; scale/currency was found correctly for 30/36 expected statements. Gate A remains not met: scanned accuracy is below 98%, identity checks are not available for all documents, and scale/currency is incomplete. The 2/2 gated bilingual pairs matched; this does not offset the failures.

The `dev` Gate B denominator is 144 taxonomy-critical slots: 12 slots per document, separate from the six planned items in the dry-run report. There are 53 mapped, 9 ambiguous, 54 unmapped and 28 in missing statements; unmapped slots are silently absent, while ambiguous slots are explicitly flagged. Of 53 mapped slots, 1 was verified and 19 were lexicon-consistent: 20/53 have verdicts, with zero disagreements among those 20. The remaining 33/53 have no verdict, so no correctness conclusion applies to them. These missing/unverified slots leave Gate B **NOT MET**.

These fresh `dev` counts are kept distinct from the historical candidate report:

| Measure | Historical report | Fresh result, 6 Oct |
|---|---:|---:|
| Digital numeric cells | 138/138 | 138/138 |
| Scanned numeric cells | 218/254 (85.83%) | 218/254 (85.83%) |
| Scale/currency | 29/36 | 30/36 |
| Critical slots: mapped / ambiguous / absent / statement missing | 52 / 6 / 50 / 36 of 144 | 53 / 9 / 54 / 28 of 144 |
| Identity status counts | no comparable historical count retained | 4 ok, 1 failed-but-explained, 4 skipped, 3 no balance |

The dry-run statement and identity counts are annual/interim/total. Balance sheets were found in 4/4, 20/29, 24/33 documents in the historical run and 4/4, 28/29, 32/33 in the current result. Income statements were found in 4/4, 17/29, 21/33 and 4/4, 20/29, 24/33 respectively. Identity status (`ok / failed / skipped / no balance`) was 4/0/0/0 annually in both reports; interim was 12/2/6/9 historically and 12/2/14/1 currently. Period-kind denominators are corporate documents (4 annual, 29 interim); the two negative controls are separate.

The regenerated structure output includes a balance sheet missing from the historical cached result, so its identity counts are reported fresh only, not as a like-for-like change.

## Holdout dry-run evidence

Each run covered 5 annual and 30 interim documents. The pipeline completed all rows without process, critical-item-stage or metrics-stage exceptions. The corporate denominator was 4 annual + 29 interim = 33; two negative controls are separate. Availability counts are not gate accuracy.

| Signal | 4 Oct candidate `3abdc22` | 6 Oct candidate `7618503` |
|---|---:|---:|
| Balance found, annual / interim / total | 4/4 / 20/29 / 24/33 | 4/4 / 28/29 / 32/33 |
| Income found, annual / interim / total | 4/4 / 17/29 / 21/33 | 4/4 / 20/29 / 24/33 |
| Identity `ok / failed / skipped / no balance`, annual | 4 / 0 / 0 / 0 | 4 / 0 / 0 / 0 |
| Identity `ok / failed / skipped / no balance`, interim | 12 / 2 / 6 / 9 | 12 / 2 / 14 / 1 |
| Identity `ok / failed / skipped / no balance`, total | 16 / 2 / 6 / 9 | 16 / 2 / 14 / 1 |
| Six planned critical items all mapped, annual / interim / total | NOT RECORDED | 3/4 / 3/29 / 6/33 |
| 12 taxonomy-critical slots per corporate document, mapped / unmapped / ambiguous / statement not found | NOT RECORDED | annual 37/10/1/0 (48 slots); interim 134/137/33/44 (348 slots); total 171/147/34/44 (396 slots) |
| Metric availability by metric | NOT RECORDED | See table below |
| Failure classes / failed documents | none / 0 | none / 0 |

The six planned items and the 12 taxonomy-critical slots are distinct measures: the former counts documents where all six named items map; the latter is a 12-item taxonomy denominator for every corporate document (33 × 12 = 396). Each period's slot states sum to its denominator. Unmapped slots are silent absences; ambiguous slots are explicitly flagged. The six-item mapping evidence is incomplete for most corporate documents; no holdout truth labels establish whether mapped items are correct. Identity has 16 `ok`, 2 failed and 14 skipped checks, plus 1 document with no balance sheet (33 corporate documents total). These are not independent identity accuracy measurements. Of the two negative controls, both were classified as corporate (one annual and one interim) against their corpus sector labels; neither was declined or held. This is a control failure, not a process exception.

### Metric output availability on the 6 October holdout

Entries are documents with at least one computed period / documents whose metrics stage ran, split annual / interim / total. Denominators are 4 annual, 29 interim and 33 total corporate documents. A zero numerator means no document in that stratum had a computed period for that metric; it is not a zero-valued metric. Flags are recorded alongside metric outputs and can overlap; they are signals, not proven exclusive causes.

| Metric | Annual | Interim | Total |
|---|---:|---:|---:|
| asset_turnover | 4/4 | 9/29 | 13/33 |
| cash_conversion_cycle | 0/4 | 5/29 | 5/33 |
| cash_ratio | 2/4 | 9/29 | 11/33 |
| current_ratio | 4/4 | 16/29 | 20/33 |
| debt_to_equity | 0/4 | 0/29 | 0/33 |
| dio | 2/4 | 9/29 | 11/33 |
| dpo | 1/4 | 6/29 | 7/33 |
| dso | 2/4 | 7/29 | 9/33 |
| ebitda | 1/4 | 0/29 | 1/33 |
| gross_margin | 3/4 | 11/29 | 14/33 |
| interest_coverage | 1/4 | 4/29 | 5/33 |
| net_debt | 0/4 | 0/29 | 0/33 |
| net_debt_to_ebitda | 0/4 | 0/29 | 0/33 |
| net_income_growth | 4/4 | 3/29 | 7/33 |
| net_margin | 4/4 | 3/29 | 7/33 |
| operating_margin | 3/4 | 9/29 | 12/33 |
| quick_ratio | 0/4 | 0/29 | 0/33 |
| revenue_growth | 4/4 | 14/29 | 18/33 |
| roa | 4/4 | 3/29 | 7/33 |
| roe | 0/4 | 0/29 | 0/33 |
| total_debt | 0/4 | 0/29 | 0/33 |

Summing each metric's total once, recorded metric/document flag incidences include missing input (450), input may be unmapped (411), needs review (252), average-fallback-to-closing (81), parentheses-negative (57), reversed words (50), duplicate period (48), unresolved row alignment (45), missing scale (45), subtotal failed (44), uncertain word order (39), identity totals not found (35), Arabic-Indic digits (34), and interim not annualized (20). These are metric-specific incidences and may overlap across metrics and flags; they do not assign a single cause to each unavailable result. Other recorded flags include period binding, ambiguous statements, currency/scale and parsing uncertainty, ties, zero denominators, and digit/separator concerns. No independently validated scale or currency correctness rate exists for the holdout.

## Timing and comparison limits

Document times below are the run's per-document medians, p90s and sums. The comparable second-run series excludes the new critical-item and metrics stages; its own full series includes both. Wall time from the captured run timestamps was about 808.5 seconds; full per-document time summed to 805.232 seconds.

| Period | 4 Oct median / p90 / sum (s) | 6 Oct comparable median / p90 / sum (s) | 6 Oct full median / p90 / sum (s) |
|---|---:|---:|---:|
| annual (n=5) | 30.275 / 42.550 / 156.451 | 25.769 / 29.325 / 120.536 | 25.770 / 29.326 / 120.543 |
| interim (n=30) | 26.065 / 45.257 / 893.703 | 21.896 / 27.210 / 684.654 | 21.898 / 27.211 / 684.689 |
| total (n=35) | 26.083 / 45.257 / 1050.154 | 22.104 / 28.630 / 805.190 | 22.106 / 28.630 / 805.232 |

The current run used native / MPS / `ocrmac`, Docling 2.126.0, and pages/locate/convert/structure/formula/taxonomy versions 5/5/2/17/1/1, with fresh per-document artifact roots. All 35 current documents were within the harness's digital/mixed time budgets. Timing differences cannot be attributed to one change: the candidates use different structure, locate and mapping versions, and differ in industry hold behavior. The first run's effective configuration and version provenance were **NOT RETAINED**, so it is not a fully comparable provenance record. The first run has no critical-item or metrics stages. Golden conversion regeneration also resumed cached conversion timings; it is not a fresh end-to-end comparison. The current golden set has only annual documents, so it does not establish interim or unknown-period performance.

## Remaining work and evidence boundary

- Correct the `dev` scanned-cell, identity, scale/currency and critical-slot failures; use fit and `dev` documents for fixes. Keep holdout documents closed to debugging. Validation is now an untuned check only, not a place to choose rules or thresholds.
- Investigate holdout failure classes on permitted fit/`dev` material: missing or unmapped metric inputs, critical-item omissions, identity/statement availability, scale and period/row alignment flags, and the two negative-control classifications. Dry-run exceptions being zero does not clear these quality failures.
- The Docker OCR bake-off was not run as a real Tesseract/Docker comparison; no Docker OCR choice is substantiated here. The `config.py` profile-parser duplication/default issue remains a deferred Minor.
- Provisional issuer-held-out Gate C measurement remains week-3 work before the checkpoint (D16). Final `model_test` confirmation waits for the owner-run checkpoint after all planned work and required checks pass (D13); the blind run waits for that checkpoint (D17). Interim remains its own reported stratum (D18), not a separate pool.
- Publication awaits owner approval. No issue, PR, merge or remote update is part of this note.

Aggregate evidence is reproducible with `/private/tmp/week2-closure-evidence/aggregate.py` and `/private/tmp/week2-closure-evidence/recount.py`; results are in `aggregate-results.json` and `recount-results.json`. Source artifacts are `var/eval/gates-dev.json`, `var/eval/structure-golden.json`, `var/eval/convert-golden.json`, `var/eval/dry_run/2026-10-06-7618503.json`, and the historical 4 October report in the `worktree-phase2-extraction` checkout. Their hashes are recorded in the private aggregate result. No row-level holdout data, labels, values or document identifiers are included here.
