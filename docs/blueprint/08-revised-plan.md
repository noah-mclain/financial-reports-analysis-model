# 08. Revised Plan: Live Demo on Unseen Documents

Status: v1, 2026-09-26. Supersedes [07-build-plan.md](07-build-plan.md), which targeted
2026-09-30. [04-execution-phases.md](04-execution-phases.md) remains the production roadmap.

**Target:** 2026-10-27 (moved from 2026-10-26). **Restarted:** 2026-09-26.

## What changed

| Before (07) | Now |
|-------------|-----|
| Demo on the golden set, stored results allowed | The interviewer uploads any English financial report found online, and it runs live |
| Base model only, fine-tuning out of scope | A trained model with a proper train/test split, scored on held-out issuers |
| Runs on the development Mac | Runs natively on the Mac, and from `docker compose` on any machine |
| 12 hand-vetted documents | A large corpus split by issuer into train, model test and blind pools |

## Where the code stands

Days 1 and 2 of the 07 plan are done: the contract schemas, the taxonomy with English and
Arabic aliases, and the number, period and unit parsers (127 tests). The corpus tooling from
this revision adds pool validation, download, per-page text-layer measurement and golden-set
dedupe. Nothing from extraction onward exists yet.

## Principles for unseen documents

1. **No document knowledge in code.** Statement pages come from titles and numeric density,
   in English; Arabic is reported, not gated (D21). `statement_pages` in the golden manifest scores the locator and is
   never an input to it. No per-issuer rules.
2. **Right, or visibly unsure.** On an unseen document the tool either produces figures that
   pass their own checks, or it shows what it extracted, marks what failed, and does not
   narrate unchecked facts (N4). The owner-authorized English partial summary may state
   individually arithmetic-confirmed metrics with explicit exclusions and the scanned-page
   caveat; the analysis remains held ([13](13-unit-caveats.md), Display). Balance sheet
   identity, subtotal sums and scale sanity are the self-test.
3. **Scanned documents run live.** A fast OCR pass locates statement pages, and an accurate
   pass reads only those pages. Budget: under 1 minute for a digital report, 2 to 4 minutes
   for a scanned filing, with a stage timeline on screen.
4. **Scores come from documents nobody tuned against.** See the pools below.

## Scope

| In | Out (say so plainly when demonstrating) |
|----|------------------------------------------|
| Statement of financial position and profit or loss | Cash flow and equity statements (stretch) |
| English, digital and scanned, within the limits of text-derived script detection | Arabic-majority extracted text is declined with a plain reason; other languages are outside the supported scope but are not generally detected |
| Non-financial corporates from any market | Banks and insurers: detected and declined with the reason |
| About 12 ratios with identity checks | The full 24-metric registry from 02 |
| Trained label normalization; narration on the base model, trained only if it misses its gate | Corrections UI, export, auth, multi-user |
| Upload page, live stage timeline, results with click-to-source | React client (replaced by a server-rendered page) |
| Native Mac profile and a `docker compose` profile | Kubernetes, cloud deployment |

## Two run profiles

Docker on macOS runs a Linux VM without access to the Apple GPU, so the three Mac-only
components get a Linux counterpart behind one interface each.

| Component | Native Mac (demo) | Docker (portable) | Interface |
|-----------|-------------------|-------------------|-----------|
| OCR | Apple Vision via ocrmac, accurate mode | Tesseract in Docker, chosen by the week 2 bake-off ([14](14-run-profiles.md)); Vision stays native | `fra_ingest.ocr.OcrEngine` |
| docling device | MPS | CPU | `configs/ingest.toml` |
| Language model | MLX, 4-bit Qwen with LoRA adapter | llama.cpp server, GGUF of the same fused model | `fra_model.runtime.ModelRuntime` |

`docker compose up` starts three services: `api` (FastAPI, serves the page), `worker` (ingest,
analytics, narration client), and `llm` (llama.cpp server). An optional `gpu` override runs
`llm` with CUDA. The Docker VM needs 10 to 12 GB of memory.

Parity rule: both profiles run the same eval (`make eval PROFILE=native|docker`), and the
report states both sets of numbers. Where Docker is worse (Arabic OCR is the likely case),
the report and the demo say so.

## Data

### Pools

Split by issuer, never by document (`eval/corpus/README.md`):

| Pool | Purpose |
|------|---------|
| `dev` | Golden set issuers. Development and debugging |
| `train` | Source of training examples |
| `model_test` | Held-out issuers for scoring the model and the full pipeline |
| `blind` | Never used in development. Demo documents come from here |

### Sources

| Source | Gives | Volume | Where it runs |
|--------|-------|--------|---------------|
| SEC Financial Statement Data Sets (`training/sources/sec_fsds.py`) | English line-item labels with their us-gaap tag, statement and filer | Thousands of filers per quarter; the largest source by far | Mac (sec.gov is blocked in the cloud environment) |
| Saudi filings from the Argaam archive (English and Arabic editions per issuer) | Arabic labels aligned to English ones by value vectors; digital layouts | Hundreds of issuer-years | Cloud or Mac |
| EGX filings | Scanned Arabic and English layouts, Egyptian terminology | Tens of issuers | Mac |
| UAE, Kuwait, Qatar, UK, US reports | Layout variety for the pipeline eval | Tens | Mac |
| Golden set | Development and hand-verified expectations | 12 | Repo |

SEC filers are split by CIK with the same rule, and any CIK in `blind` or `model_test` is
excluded from training rows. Filers with bank or insurance SIC codes are tagged, not dropped,
so the classifier learns to decline them.

## Schedule

| Week | Work | Done when |
|------|------|-----------|
| 1: Sep 26 to Oct 2 | Corpus expansion and fetch. `packages/ingest`: per-page text layer, locator, docling on candidate pages, grid with provenance, header and period binding, scale and currency, page continuation. Docker skeleton (base image, compose, both profiles start). Spike: fuse an MLX LoRA adapter and run it as GGUF in llama.cpp | Almarai English and Arabic extracted with identical figures. Locator recall 100% on `dev`. `docker compose up` serves a health page |
| 2: Oct 3 to 9 | OCR engines behind one interface, bake-off on the golden scans (Vision vs Tesseract; RapidOCR was cut, see 14). Label mapping: aliases, fuzzy match, structural anchors. Identity checks, 12 metrics with D1 to D6 defaults, decline for banks and insurers. First dry run on the train holdout | Gate A and Gate B on `dev` and the dry run; `model_test` at the checkpoint. OCR engine chosen for Docker with measured accuracy |
| 3: Oct 10 to 16 | SEC FSDS dataset (bilingual pairs are not built), issuer-grouped split, length audit. Baseline vs QLoRA for normalization on the train holdout, with training data smaller by the holdout share. Grounding checker, templates, English narration. GGUF export of the promoted adapter | Gate C, provisional until the checkpoint. Normalization beats baseline on held-out issuers |
| 4: Oct 17 to 23 | Upload page, background job, stage timeline, results page (statements with click-to-source, metrics with formulas, summary with citations). Docker profile work. Dry runs on the train holdout, fix the most common failure classes on non-holdout documents | Gate D. The blind pipeline observation runs at checkpoint; Gate E correctness is not judged without reviewed references. |
| Checkpoint (owner-run; D23, 2026-10-26) | Once-only `model_test` pipeline observations, then non-reserved `blind` pipeline observations; report each gate only to the extent its evidence supports | Pools freeze at the first `model_test` evaluation. D23 authorizes proceeding and reporting limits honestly; it does not waive checks or the Gate D precondition. |
| Oct 25 to 27 | Rehearsal, accuracy report, README. Use the D22 reserved blind issuers (note after the gates) | Two clean end-to-end runs on reserved documents never run before |

When a week overruns, cut inside that week. Cash flow statements are the first cut to
restore if time is left, not the first thing to add.

## Gates

- **A. Extraction.** Almarai English figures match on every row. Balance sheet identity
  holds, or is flagged with the correct reason, on every English `dev` and `model_test`
  document. Scale and currency are correct on all of them. English digital and scanned cells
  meet G1's accuracy bars ([04-execution-phases.md](04-execution-phases.md)). Arabic figures
  carry no gate: their cell accuracy, 95% interval and share of figures on statements that
  passed review remain reported, with emitted numeric coverage and confident wrong or
  unassessed output visible. Accuracy and accepted share retain their expected-cell
  denominators. Bilingual pairs are report-only. D21 supersedes D14 and D20 for gate scope
  ([06-decisions-and-risks.md](06-decisions-and-risks.md)).
- **B. Mapping.** Every critical item (revenue, net profit, total assets, total equity, total
  current assets and liabilities) is mapped or explicitly flagged on `dev` and `model_test`,
  in English. Arabic mapping is reported, not gated. No silent wrong English mapping.
- **C. Model.** Normalization on held-out issuers: critical items 99% or higher, macro-F1 at
  least 5 points above the base model. It is never pooled across sources: it is judged for SEC
  English labels and corpus English labels, each only if large enough; corpus Arabic is not
  shown. For the scored sources, a stratum that is too small is "not shown" (rules, levels and example counts:
  [04-execution-phases.md](04-execution-phases.md) 2.4). Before the checkpoint it is measured
  on the train holdout and marked provisional. A `model_test` measurement is not available for
  this checkpoint: `training/sec_examples.py` and `training/dataset_contracts.py` authorize only
  `train` sources, and no reviewed `model_test` example path or barrier change is in place.
  Report Gate C as not measured on `model_test`; validation-panel diagnostics are not the gate.
  Summaries: zero numeric claims that do not
  match a computed metric; schema-valid after at most one repair, otherwise the template.
- **D. Portability.** `docker compose up` on a clean machine runs upload to summary on one
  digital and one scanned English document. Eval numbers recorded for both profiles.
- **E. Blind.** On the blind pool excluding the D22 rehearsal reserve, every English document
  ends in a correct result, a correct decline, or a visible `needs_review` with the reason.
  Zero confident wrong figures.

D21 was declared by the owner on 21 October, before the planned 24 October checkpoint and
before any protected scoring. Apply the same manifest-language rule to `model_test`
and `blind`, independently of results; D22 excludes the fixed rehearsal reserve from the blind
run. Arabic stays in the code, corpus and training pool;
uploads identified from extracted text as mainly Arabic are declined. This script-majority
check can miss Arabic when OCR is poor and does not identify non-English Latin-script reports.
The excluded Arabic documents and Arabic-only issuers remain
visible in the accuracy report. The measured denominators and method are in
[the corpus rules](../../eval/corpus/README.md#english-only-scored-scope-d21).

Gates A, B and E, and every dry run, are reported per period kind (annual and interim) beside
the total, and Gate C per period kind as description only (D18 in
[06-decisions-and-risks.md](06-decisions-and-risks.md); how, in
[04-execution-phases.md](04-execution-phases.md) 2.4).

D23 (owner decision, 2026-10-26) authorizes the one-time checkpoint to proceed and report
observations honestly. The checkpoint runner has no reference-answer path for `model_test` or
`blind`, so Gate A and B accuracy are not measured on those pools, Gate A's accepted-cell share
is not judged there, and Gate E is not judged. The training contract restricts supervised
examples to `train`, so Gate C is not measured on `model_test`; validation panels are diagnostics
only. Never call Gates A, B, C or E met or failed based on those observations. The runbook
reports supported operational counts and explicitly names these limits.

The owner runs the checkpoint once under D23; D23 is the owner's explicit decision to proceed
and report limitations honestly. Before the run, required checks and the separate Gate D
precondition below still must hold. In order: `model_test` pipeline observations are recorded,
then the non-reserved `blind` pool is run for the first time (D17). This is not a commitment
that Gates A to E will all be confirmed. Until the run, nothing is scored on `model_test` or
`blind`. Gates A and B are reported on `dev` and on dry runs of the full
pipeline on the documents of the train-holdout issuers, and marked "not yet measured on
`model_test`"; Gate C is measured on the train holdout (D16, design in
[04-execution-phases.md](04-execution-phases.md) 2.4, pool rules in
[eval/corpus/README.md](../../eval/corpus/README.md)); the non-reserved blind pipeline runs at
the checkpoint, while Gate E correctness remains unjudged without references.

Checkpoint volume under D21 and D22, recounted from metadata on 26 October: `model_test`
scores 36 English documents from 25 issuers (13 annual, 23 interim); `blind` runs 29 English
documents from 20 issuers (14 annual, 15 interim). The D22 reserve holds 10 English documents
from 8 of the 28 English blind issuers. The 29 and 33 Arabic documents, respectively, are
excluded by manifest language; eleven Arabic-only issuers leave each scored denominator.
No protected documents were opened for these counts. Runtime for the smaller scored set has
not been measured; the per-document budgets above remain targets. Gate D evidence was recorded on 26 October (below). Therefore the Gate D precondition for step 1
is **met for the digital document and for the Docker eval numbers, and not met for the scanned
document**: no scanned English document produced a summary in the Docker profile, because every
scanned analysis was held for review (the OCR misreads digits, drops values and flips signs, so
subtotal and identity checks fail). Before step 1, for the scanned summary the owner either
records a specific Gate D exception in [the decision log](06-decisions-and-risks.md) or a scanned
English document completes with a summary recorded here.

Gate D evidence, 26 October 2026 (main `410629a`; Docker Desktop 27.3.1, 12 CPUs, 14.1 GB VM; the
Docker profile with Tesseract; `make docker-up`, upload through the API, status read until final;
documents are `dev` golden documents, not protected ones):

| Document | Pages | Type | Final status | Wall time | Summary |
| --- | --- | --- | --- | --- | --- |
| almarai-2025-en-annualreport | 275 | mixed, statements with a text layer | completed | 232 s | present (982 characters) |
| juhayna-2025-en-standalone | 61 | mixed, balance sheet scanned | needs_review | 111 s | withheld, analysis needs review |
| juhayna-2024-en-consolidated | 57 | scanned | needs_review | 162 s | withheld, analysis needs review |
| edita-2025-en-consolidated-ifrs | 85 | scanned | needs_review | 273 s | withheld, analysis needs review |
| edita-2025-en-consolidated-eas | 66 | scanned | needs_review | 268 s | withheld, analysis needs review |

Reading: the Docker profile runs upload to results end to end on all five documents. Only the
digital-text document reached a summary; the four scanned or partly scanned documents were held
for review, so the summary was withheld by design. The two Juhayna documents extracted two
statements (no balance sheet) in this profile.

Eval numbers for both profiles, the cells of the two English `dev` documents that have expected
files (`eval/golden/expected`), scored with `harness.extraction.score_statement` on the Docker
profile's analysis output against the same expected files the native evaluation uses:

| Document | Native (Vision), 24 October gate run | Docker (Tesseract), 26 October |
| --- | --- | --- |
| almarai-2025-en (digital text) | 138 of 138 | 138 of 138 (balance 80/80, comprehensive income 20/20, income 38/38) |
| edita-2025-en-consolidated-ifrs (scanned) | 123 of 126 | 114 of 126 (balance 61/70, comprehensive income 15/16, income 38/40) |

Docker is worse on the scanned document, as the parity rule anticipates. Identity, mapping and
metric numbers for the Docker profile were not computed. The run scripts are operator scripts
outside Git; the evidence is in local `var/gate-d` and `var/gate-d2`.

Decided 2026-10-26 (D22), before checkpoint results: take option 1 of the rehearsal question.
Reserve the first 8 English blind issuer keys ordered by ascending hex
`sha256("rehearsal-reserve-v1:" + issuer_key)`, computed from metadata only. The rule and keys
live in `eval/corpus/rehearsal_reserve.yaml`; each reserved issuer must have an English document.
Nobody opens, reads or runs a reserved document before rehearsal. The checkpoint reports
observations for the remaining English blind documents using that denominator; it does not judge
Gate E because no reviewed references are available. The rehearsal can therefore use documents
nobody has run. Corpus membership and issuer pool boundaries do not change. The pool runner
records outcomes, identity and mapping states and operational budgets; without reference answers
it cannot certify figure accuracy, Gate A accepted-cell share or Gate E's zero-confident-wrong
promise. Those remain unjudged by this runner.

The price, stated once: everything measured on `model_test` and `blind` arrives at the very
end, so a bad surprise there has little time to be fixed. After the freeze a fix can only be
developed on `dev` and `train` documents (corpus rule 3), and the frozen score is then no longer
a first look. The holdout and the dry runs reduce that risk; they do not remove it.

## Risks added by this revision

| ID | Risk | Mitigation |
|----|------|------------|
| R15 | Linux OCR too weak on Arabic scans | Closed for gating by D21. Retain measured Arabic OCR limits in the accuracy report; extracted text identified as mainly Arabic is declined, but poor OCR can miss it |
| R16 | Fused adapter does not convert to GGUF cleanly | Week 1 spike, before any training run depends on it |
| R17 | Blind documents leak into development | Issuer-grouped pools enforced by `make corpus-check`; failures fixed on non-blind documents |
| R18 | Cloud environment cannot reach most filing hosts | Collection and fetch scripts run unchanged on the Mac; URL and sha256 recorded so the set reproduces |
| R19 | CPU-only narration too slow in Docker | Closed 2026-10-15: the 7B Q4_K_M file generates 15 to 19 tokens/s on the CPU inside the container, above the 5 tokens/s line ([14-run-profiles.md](14-run-profiles.md)); the template summary is always available |
| R20 | Source site terms restrict automated download | Polite fetch (one request at a time, robots.txt respected), issuer and exchange sources preferred, documents never redistributed |

## Working rules

Carried from 07. Commit at the end of each working day with the gate status in the message.
