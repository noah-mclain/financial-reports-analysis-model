# Accuracy report

Status: 24 October 2026. Every number below is copied from a dated run artifact or a committed
report, with its source. Nothing here is a gate result: `model_test` and `blind` have not been
scored (D13, D17), and the checkpoint has not run. Rows marked "not measured" have no run.

Scored scope is English documents (D21). Arabic results stay visible where they were measured.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| A. Extraction | Not met on `dev` (English, 24 October): scanned cells and balance identity miss the bar | `var/eval/gates-dev.json`, 6 English golden documents |
| B. Mapping | Slot accounting met on `dev` (24 October); no mapped slot is verified against an independent reference | same |
| C. Model | Not met: no adapter has been promoted | [normalization-validation.md](runbooks/normalization-validation.md) |
| D. Portability | Not measured: CI builds the image and checks API health only | [ci.md](runbooks/ci.md) |
| E. Blind | Not measured: waits for the checkpoint | None; the checkpoint has not run |

## Gate A and B, English golden set (`dev`), 24 October

`make eval-gates` on the 6 English annual documents (D21 gated language; Arabic is reported
only). In-sample: the aliases were taken from these documents. Not measured on `model_test`.

| Measure | Result |
| --- | --- |
| Digital cells right | 138 of 138 (100.00%, 95% Wilson 97.29% to 100.00%, 1 file); met |
| Scanned cells right | 123 of 126 (97.62%, 95% Wilson 93.23% to 99.19%, 1 file); not met |
| Bilingual pairs identical | 0 of 0 gated pairs (8 ungated); not measured |
| Balance identity | 5 of 6 hold or are explained (ok 4, failed but accepted by rule 1, skipped 1); not met |
| Scale and currency | 18 of 18 statements right; met |
| Mapping slots | 72 total: 46 mapped, 8 ambiguous (flagged), 18 explicitly flagged, 0 silently absent, 0 statement not found; met |
| Mapped slots with an independent verdict | 19 of 46 consistent with the lexicon, 0 disagree, 0 verified, 27 no verdict |
| Output coverage, scanned without reference | 0 of 77 emitted numeric cells assessed, 12 of them shown as confident; safety is not established |

The scanned and mapping rows count the cells and slots the gate could check. The 4 scanned
documents without an expected file contribute no right-or-wrong cells.

## Gate A and B, all languages (`dev`), 7 October, superseded

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

This table predates the English-only rescoring of 21 October. It is kept for the Arabic and
bilingual-pair history; the 24 October table above supersedes it for scored scope.

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
