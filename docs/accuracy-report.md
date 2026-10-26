# Accuracy report

Status: 26 October 2026. Gate A and B results below are from `dev`; nothing here is a held-out
gate result. `model_test` and `blind` have not been scored (D13, D17), and the checkpoint has
not run. The cited Gate A/B and locate results are the dated reports committed under
[`eval/results/`](../eval/results/README.md), with their SHA-256 and the command that produced
each. The live reports under `var/` are not committed and a later run overwrites them; regenerate
with `make eval-gates` and `make eval-locate TARGET=golden`. Rows marked "not
measured" have no run.

Scored scope is English documents (D21). Arabic results stay visible where they were measured.
The 29 Arabic documents in `model_test` and 33 in `blind` are excluded; 11 Arabic-only issuers
leave each scored denominator ([D21 counts](blueprint/08-revised-plan.md)).

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| A. Extraction | Not met on `dev` (English, 24 October): scanned cells and balance identity miss the bar | [`eval/results/gates-dev-2026-10-24.json`](../eval/results/gates-dev-2026-10-24.json), 6 English golden documents |
| B. Mapping | Slot accounting met on `dev` (24 October); no mapped slot is verified against an independent reference | same |
| C. Model | Not met: no adapter has been promoted | [normalization-validation.md](runbooks/normalization-validation.md) |
| D. Portability | Partly measured (26 October): the Docker profile ran upload to results on five English `dev` documents. The Almarai annual report completed with a summary in 232 s; four scanned or partly scanned Juhayna and Edita documents ended `needs_review` with the summary withheld (111 to 273 s). No scanned document reached a summary, and Docker eval numbers were not recorded, so Gate D is not established | [08-revised-plan.md](blueprint/08-revised-plan.md) |
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
| Mapped slots with a verdict | 19 of 46 consistent with the lexicon, 0 disagree, 0 verified, 27 no verdict |
| Output coverage, no expected file | Digital (2 files): 0 of 77 emitted numeric cells assessed, 12 shown as confident. Scanned (4 files): 0 of 316 assessed, 64 shown as confident. Safety is not established |

The scanned and mapping rows count the cells and slots the gate could check. The 4 scanned
documents without an expected file contribute no right-or-wrong cells.

The lexicon verdicts are not independent of the lexicon that mapped the rows.

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

From [the locate design document](blueprint/09-ingest-locate.md) and the committed 7 October
re-runs, [`locate-golden-2026-10-07.json`](../eval/results/locate-golden-2026-10-07.json) and
[`locate-train-2026-10-07.json`](../eval/results/locate-train-2026-10-07.json): statement-page
recall 100% on 12 golden documents (no misses), with a median 14.1% of pages sent to conversion.
On the 178 `train` documents (fit and validation parts, never the holdout) the locator covered
all 156 corporate filings, judging 154 right and 2 held (annual 45 of 47, interim 109 of 109,
none wrong). Of 22 negative controls, none of the 4 banks was wrongly passed (3
declined, 1 held for review) and neither of the 2 insurers was (both held); of 16 other financial
companies, 10 were held and 6 wrongly passed. The golden set is not held out. The design
document's table (10.7%, 109 of 109 corporate filings, 6 of 6 banks and insurers) records an
earlier run; the committed files are the later re-runs and do not reproduce those figures.

## Scanned value agreement, Juhayna, 12 October

97 of 124 reviewed cells agree (78.23%); balance sheet 67.11%, comprehensive income 83.33%,
income statement 100.00% (36 cells). A development diagnostic on in-sample labels, not
extraction accuracy. Details and the 27 mismatches:
[juhayna-value-agreement-2026-10-12.md](exec-summaries/juhayna-value-agreement-2026-10-12.md).

## Scanned Arabic OCR, Edita 2024

The run-profile bakeoff records Vision (`ocrmac`), accurate, at 98 of 128 Arabic cells right
(76.6%; 95% interval 68.5% to 83.1%) ([run profiles](blueprint/14-run-profiles.md)). The dated
extraction review also records 98 of 128 (76.56%) ([review report](blueprint/12-ingest-review.md)).
This is a development diagnostic, not a gated Arabic result under D21.

## Normalization (Gate C inputs)

Validation diagnostics on a frozen 512-row SEC English selection, not the gate. These values
are from the 19 October 2026 run described in the
[normalization validation runbook](runbooks/normalization-validation.md); its cited
`var/normalization-validation/prompt-v2/` evidence is local and uncommitted. The runbook names
the report/artifacts, but those files are not preserved in this checkout, so these values cannot
be independently checked from a clean checkout:

| Metric | Lexicon | Unadapted 7B |
| --- | --- | --- |
| Accuracy | 86.33% | 89.26% |
| Macro-F1, 47 classes | 30.66% | 52.78% |
| Invalid replies | 0 | 11 |
| False mappings on null targets | 2 | 21 |

The gate needs an adapter at least 5 points above the base model and 99% on critical items on
held-out issuers. The 11 critical examples are "not shown" at this size. No adapter has been promoted.

### Bounded fit, 25 October (validation panels)

One 1,000-update adapter, generated at temperature 0 on two frozen validation panels
(`representative`, 512 rows; `challenge`, 168 rows) and scored against the unadapted base model
with a paired issuer bootstrap. Not Gate C: one seed, validation diagnostics, no `model_test` or
`blind` score. The panels differ from the 512-row selection above, so the base figures differ.
Source and commands: [normalization-training.md](runbooks/normalization-training.md#measured-result-bounded-fit-25-october-2026).

| Panel | Predictor | Accuracy | Macro-F1 (47 classes) | Invalid | False mappings on null | Taxonomy-critical errors | Macro-F1 change vs base, 95% CI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Representative | Base | 0.875 | 0.520 | 15 | 20 | 5 of 23 | n/a |
| Representative | Fit (1,000 updates) | 0.916 | 0.592 | 12 | 24 | 0 of 23 | +0.032 to +0.120 |
| Challenge | Base | 0.804 | 0.457 | 1 | 6 | 15 of 95 | n/a |
| Challenge | Fit (1,000 updates) | 0.881 | 0.538 | 1 | 5 | 6 of 95 | +0.018 to +0.105 |

Both intervals exclude zero, and the point estimates (+7.2 and +8.1 points) exceed the 5-point
margin, but the lower ends do not. Critical items are not at 99% on the challenge panel, and
false mappings on null rows rose on the representative panel. A 3,000-update fit aborted at update 1,500
after 21,874 seconds because validation loss reached 0.0778, 1.36 times the step-0 value of 0.0572
(limit 1.25); it produced no candidate. Its retained update-1,400 checkpoint, scored as a diagnostic only, was
not better than the 1,000-update adapter: about the same representative macro-F1 (0.603 against
0.592) with lower accuracy (0.895) and 23 invalid replies, challenge macro-F1 0.493 with a change over base of -0.014
to +0.074 (the interval includes zero). See
[normalization-training.md](runbooks/normalization-training.md#the-3000-update-fit-aborted-25-october-2026).

## Not measured

- `model_test` and `blind` scores for any gate.
- Gate A and B on the train-holdout dry runs since 7 October.
- Narration with a language model; the template summary is the shipped path.
- Docker upload to summary on a scanned English document, and eval numbers for the Docker profile. The digital half was measured on 26 October (the Almarai annual report completed with a summary); the four scanned or partly scanned documents were held with the summary withheld.
- Two clean end-to-end runs on documents never seen before ([procedure](runbooks/rehearsal.md)).
