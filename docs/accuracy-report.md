# Accuracy report

Status: 27 October 2026. Gate A and B results below are from `dev`; nothing here is a held-out
gate result. The one-time checkpoint pipeline observations ran on 27 October; they do not
measure gate accuracy or correctness. The cited Gate A/B and locate results are the dated reports committed under
[`eval/results/`](../eval/results/README.md), with their SHA-256 and the command that produced
each. The live reports under `var/` are not committed and a later run overwrites them; regenerate
with `make eval-gates` and `make eval-locate TARGET=golden`. Rows marked "not measured" have no
measurement; checkpoint pipeline observations are not gate measurements.

Scored scope is English documents (D21). Arabic results stay visible where they were measured.
The 29 Arabic documents in `model_test` and 33 in `blind` are excluded; 11 Arabic-only issuers
leave each scored denominator ([D21 counts](blueprint/08-revised-plan.md)).

## Checkpoint pipeline observations, 27 October 2026

The owner-run checkpoint used the pipeline-only runner: ingest pipeline, no model call, and no
reference-answer path. These observations establish operational completion only. A
`held_for_review` outcome is not a correctness verdict and is not evidence for Gate E. The run
commit was `72607d1d7e0a324faa44e83604505b5a5f1e93db`. Each summary's `candidate` string is the commit, document-list hash and configuration hash; the configuration SHA-256 is
`42138cf1572892d8f7241b168977ce699ab48f362ce6fa141e399d08d175857e`. The exact `candidate`
strings are `72607d1d7e0a324faa44e83604505b5a5f1e93db:documents=33c5f7f80a34497a5de57751ce49061b50011b7d5033272b36514b0f8fd797b3:config=42138cf1572892d8f7241b168977ce699ab48f362ce6fa141e399d08d175857e`
for `model_test` and
`72607d1d7e0a324faa44e83604505b5a5f1e93db:documents=243fe4b4169b69decd80e242ab8cf8abd3e59e7f1ab03d066f2bd1602b8f4a05:config=42138cf1572892d8f7241b168977ce699ab48f362ce6fa141e399d08d175857e`
for `blind`. The recorded candidate identity is one adapter, one seed,
`adapters.safetensors` SHA-256
`2f7234d7a96bdf02b9c4384f0a4b17389ade5a05ad4fa13c70513ba5fee28a1e`, `adapter_config.json`
SHA-256 `5cdfcfca249bfb025076eb71931f43907fe25ac1e142c1a26cc8855281a312eb`, and base-model
manifest SHA-256 `d5d0d55fa3528eb37274c0b50b16a1c599ed046f9c5400d753657ef880e96687`. The runner
did not exercise this candidate.

D22 scope: `model_test` was 36 English documents from 25 issuers (13 annual, 23 interim);
`blind` was 29 English documents from 20 issuers (14 annual, 15 interim). The 8 reserved
issuers and their 10 English documents were excluded from `blind` and remained unopened. The
summary document and period fields and row language fields agree with the D22 counts: no count
differed. Arabic gate claims are not made (D21).

The tables below report each total beside its annual and interim strata. Outcome, identity,
critical-item, failure-class, time-budget and peak-memory values come from the corresponding
pool summary's root fields for total and `by_period.annual` / `by_period.interim` fields for
strata. Industry outcome counts are grouped from `rows[].industry.outcome` and
`rows[].industry.code`; language counts are grouped from `rows[].language`. The row fields are
used only for these operational counts.

### `model_test`

| Outcome | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Completed | 0 | 0 | 0 |
| Declined | 0 | 0 | 0 |
| Failed | 0 | 0 | 0 |
| Held for review | 36 | 13 | 23 |

| Identity outcome | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Passed | 24 | 6 | 18 |
| Not met | 12 | 7 | 5 |
| Held with explanation | 0 | 0 | 0 |

| Critical-item status | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Mapped | 257 | 85 | 172 |
| Explicitly flagged | 130 | 45 | 85 |
| Ambiguous | 29 | 22 | 7 |
| Statement not found | 16 | 4 | 12 |
| Unmapped | 0 | 0 | 0 |

| Industry outcome | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Pass | 35 | 12 | 23 |
| Needs review, near threshold | 1 | 1 | 0 |
| Declined: bank | 0 | 0 | 0 |
| Declined: insurer | 0 | 0 | 0 |

No failure classes were recorded (0 total, annual, or interim; `failure_classes` is empty).
Every document was held for review. Text-layer documents within budget and peak memory were:

| Operational measure | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Digital within 60 s | 9/9 | 2/2 | 7/7 |
| Mixed within 240 s | 25/25 | 10/10 | 15/15 |
| Scanned within 240 s | 2/2 | 1/1 | 1/1 |
| Peak memory (GB) | 3.170261412858963 | 3.170261412858963 | 3.0983012318611145 |

All 36 rows were English (`rows[].language = en`; annual 13, interim 23). The summary SHA-256
is `471e2e2b5ad37b8fe7cd9f1472dcc3ffc98503c562b0ba912d3c25749fa61842`; the per-document
manifest SHA-256 is `a3c78cd02880be3e59b3889044b43e9cc99c88ca5932074db026cedc51bcd107`.

### `blind`

| Outcome | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Completed | 0 | 0 | 0 |
| Declined | 2 | 2 | 0 |
| Failed | 0 | 0 | 0 |
| Held for review | 27 | 12 | 15 |

| Identity outcome | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Passed | 19 | 8 | 11 |
| Not met | 10 | 6 | 4 |
| Held with explanation | 0 | 0 | 0 |

| Critical-item status | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Mapped | 207 | 96 | 111 |
| Explicitly flagged | 98 | 37 | 61 |
| Ambiguous | 19 | 11 | 8 |
| Statement not found | 0 | 0 | 0 |
| Unmapped | 0 | 0 | 0 |

| Industry outcome | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Pass | 26 | 11 | 15 |
| Needs review, near threshold | 1 | 1 | 0 |
| Declined: bank | 1 | 1 | 0 |
| Declined: insurer | 1 | 1 | 0 |

No failure classes were recorded (0 total, annual, or interim; `failure_classes` is empty).
The two declined documents are reported as counts only. Text-layer documents within budget
and peak memory were:

| Operational measure | Total | Annual | Interim |
| --- | ---: | ---: | ---: |
| Digital within 60 s | 8/8 | 3/3 | 5/5 |
| Mixed within 240 s | 18/18 | 9/9 | 9/9 |
| Scanned within 240 s | 3/3 | 2/2 | 1/1 |
| Peak memory (GB) | 2.4050476402044296 | 2.372805617749691 | 2.4050476402044296 |

All 29 rows were English (`rows[].language = en`; annual 14, interim 15). The summary SHA-256
is `396f1a270cc336d22a192a769030b23489eebff48c90abc785bd1301f3a71b33`; the per-document
manifest SHA-256 is `020f48b0ad500218d8c5b7962b9f4421932b37a9024e2492c491b298e1f8043d`.

Gate A and B accuracy are **not measured on `model_test` or `blind`**; Gate A's accepted-cell
share is not judged. Gate C is **not measured on `model_test`**; validation panels are
diagnostics only and are not pooled or substituted for Gate C. Gate E is **not judged** because
there are no reviewed references. Never interpret these observations as any of Gates A, B, C,
or E passing or failing. Gate D is reported only from its separate recorded evidence: its
digital Docker result met; its scanned half did **not** meet and is covered by the owner's D24
exception. Partial summaries existed for 2 of 4 scanned or partly scanned `dev` documents,
`net_margin` only, and all remained held for review.

The summaries and per-document checksum manifests are archived privately outside Git.

## Gate status

| Gate | Status | Evidence |
| --- | --- | --- |
| A. Extraction | Not met on `dev` (English, 24 October): scanned cells and balance identity miss the bar | [`eval/results/gates-dev-2026-10-24.json`](../eval/results/gates-dev-2026-10-24.json), 6 English golden documents |
| B. Mapping | Slot accounting met on `dev` (24 October); no mapped slot is verified against an independent reference | same |
| C. Model | Not met: no adapter has been promoted | [normalization-validation.md](runbooks/normalization-validation.md) |
| D. Portability | Partly established (26 October): the Docker profile ran upload to results on five English `dev` documents; the Almarai annual report completed with a summary (the digital half is met). Docker cell accuracy: Almarai 138 of 138 (native 138 of 138), Edita IFRS 114 of 126 (native Vision 123 of 126). The scanned half is **not met** and is covered by the owner's exception D24: with the confirmed-only partial summary, 2 of 4 scanned or partly scanned `dev` documents reach a partial summary (`net_margin` only, each included figure equal to its reviewed reference) and all four stay `needs_review` | [08-revised-plan.md](blueprint/08-revised-plan.md) |
| E. Blind | Not judged: no reviewed references exist for the checkpoint pools | [Checkpoint observations](#checkpoint-pipeline-observations-27-october-2026) |

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

## Rehearsal, 27 October 2026

Three end-to-end runs on reserved issuers' English annual documents (two planned, a third added after neither was clean) ([procedure](runbooks/rehearsal.md)), commit `72607d1d7e0a324faa44e83604505b5a5f1e93db`, normalization flag off (the shipped default). Selection rule, written before any output was seen: the first two of the four reserved English annual documents ordered by fetched SHA-256; native first, Docker second. Neither run is clean under the runbook's reading (completes without `needs_review`, no contradiction in the owner's spot check); that reading awaits the owner's confirmation.

| Run | Profile | Document | Final status | Wall time | Statements | Summary |
| --- | --- | --- | --- | --- | --- | --- |
| A | Native | Tesco 2025 annual report (digital, 248 pages) | `needs_review`, declined as an insurer | 5 s | 0 | none |
| B | Docker | Aseer 2021 annual financial statements (mixed, 73 pages) | `needs_review` | 273 s | 2 (balance, income), both held | withheld, no partial summary |
| C | Native | Agthia 2025 annual financial statements (digital, 64 pages) | `needs_review` | 31 s | 3 (balance held, comprehensive income passed, income `needs_review`) | partial: `net_margin` 2.1% only |

Run A: the decline reason is `declined:insurer`, score 9.0 from three cues ("insurance revenue" on page 137, "insurance contract liabilities" and "reinsurance contract assets" on page 139). The issuer is a retailer with a bank arm, so this may be a false decline; it is recorded as found, not corrected, and the document was not replaced. Run B: the balance sheet is held for unmapped critical items (cash and equivalents, equity attributable to the parent, total liabilities) and the income statement for unmapped critical items (net income, net income attributable to the parent, operating income) and two unchecked subtotals; balance-dependent metrics are excluded and no figure was confirmed. Run C was added at the owner's instruction after A and B were not clean: the next reserved English annual document under the same ordering rule (third by fetched SHA-256), run natively as a replacement for A. It was chosen after A's outcome was seen and is reported as a replacement, not as a pre-planned run; A and B stay as recorded. C: the balance sheet is held (59 numeric cells, none checked by an arithmetic check, 21 flagged; 17 cells unparsed, 4 rows misaligned, unchecked subtotals, 7 critical items unmapped), comprehensive income passed (16 of 16 checked) and income needs review (34 of 36 checked, 2 ambiguous separators). The confirmed-only partial summary states `net_margin` 2.1% and excludes current ratio, total debt, net debt to EBITDA and days sales outstanding because the balance sheet could not be confirmed, and EBITDA as undefined. Nothing was tuned against these documents. Three documents do not support a generalization. The owner's spot check of a figure and a formula has not been made.

## Not measured

- Gate A and B accuracy on `model_test` or `blind`, Gate A accepted-cell share there, Gate C on `model_test`, and Gate E correctness: the checkpoint records pipeline operations only, without reviewed references. See [checkpoint pipeline observations](#checkpoint-pipeline-observations-27-october-2026).
- Gate A and B on the train-holdout dry runs since 7 October.
- Narration with a language model; the template summary is the shipped path.
- Gate D scanned portability: not met; D24 records the exception. Partial summaries on 2 of 4 scanned or partly scanned documents contained `net_margin` only and remained held for review. Docker identity, mapping and metric numbers (only cell accuracy on two documents is recorded).
- Two clean end-to-end runs on documents never seen before ([procedure](runbooks/rehearsal.md)): three runs were made on 27 October on reserved documents, and **none was clean** (see the rehearsal section below); the owner's spot check has not been made.
