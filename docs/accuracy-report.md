# Accuracy report

Status: 24 October 2026. Every number below is copied from a dated run artifact or a committed
report, with its source. Nothing here is a gate result: `model_test` and `blind` have not been
scored (D13, D17), and the checkpoint has not run. Rows marked "not measured" have no run.

Scored scope is English documents (D21). Arabic results stay visible where they were measured.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| A. Extraction | Not met on `dev`; measurement dated 7 October, before later fixes | `var/eval/gates-dev.json`, 12 golden documents |
| B. Mapping | Not met on `dev`; measurement dated 7 October | same |
| C. Model | Not met: no adapter has been promoted | [normalization-validation.md](runbooks/normalization-validation.md) |
| D. Portability | Not measured: CI builds the image and checks API health only | [ci.md](runbooks/ci.md) |
| E. Blind | Not measured: waits for the checkpoint | None; the checkpoint has not run |

## Gate A and B, golden set (`dev`), 7 October

Annual stratum, 12 documents, all languages (the run predates D21, so the set includes Arabic
documents). No interim documents were in the set.

| Measure | Result |
| --- | --- |
| Digital cells right | 138 of 138 (1 file) |
| Scanned cells right | 218 of 254, 85.83% (2 files); bar not met |
| Bilingual pairs identical | 2 of 2 gated pairs |
| Balance identity | ok 4, skipped 4, no balance sheet 3, failed but accepted 1; not met |
| Scale and currency | 31 of 36 found and right, 5 not found; not met |
| Mapping slots | 144 total: 56 mapped, 55 explicitly flagged, 9 ambiguous, 24 statement not found, 0 unmapped; not met |

This predates the English-only rescoring of 21 October and the merges since. It needs a new
`make eval-gates` run before any claim.

## Locate (golden and `train`)

From [the locate design document](blueprint/09-ingest-locate.md) and `var/eval/locate-*.json`:
statement-page recall 100% on 12 golden documents; median share of pages sent to conversion
10.7%; corporate filings with a balance and an income range in `train` 109 of 109; banks and
insurers recognised 6 of 6. The golden set is not held out.

## Scanned value agreement, Juhayna, 12 October

97 of 124 reviewed cells agree (78.23%); balance sheet 67.11%, comprehensive income 83.33%,
income statement 100.00% (36 cells). A development diagnostic on in-sample labels, not
extraction accuracy. Details and the 27 mismatches:
[juhayna-value-agreement-2026-10-12.md](exec-summaries/juhayna-value-agreement-2026-10-12.md).

## Normalization (Gate C inputs)

Validation diagnostics on a frozen 512-row SEC English selection, not the gate:

| Metric | Lexicon | Unadapted 7B |
| --- | --- | --- |
| Accuracy | 86.33% | 89.26% |
| Macro-F1, 47 classes | 30.66% | 52.78% |
| Invalid replies | 0 | 11 |
| False mappings on null targets | 2 | 21 |

The gate needs an adapter at least 5 points above the base model and 99% on critical items on
held-out issuers. The 11 critical examples are "not shown" at this size. No adapter has been promoted.

## Not measured

- `model_test` and `blind` scores for any gate.
- Gate A and B on the train-holdout dry runs since 7 October.
- Narration with a language model; the template summary is the shipped path.
- Docker upload to summary on one digital and one scanned English document.
- Two clean end-to-end runs on documents never seen before.
