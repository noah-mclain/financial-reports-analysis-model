# 08. Revised Plan: Live Demo on Unseen Documents

Status: v1, 2026-09-26. Supersedes [07-build-plan.md](07-build-plan.md), which targeted
2026-09-30. [04-execution-phases.md](04-execution-phases.md) remains the production roadmap.

**Target:** 2026-10-26. **Restarted:** 2026-09-26.

## What changed

| Before (07) | Now |
|-------------|-----|
| Demo on the golden set, stored results allowed | The interviewer uploads any English or Arabic financial report found online, and it runs live |
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
   in both languages. `statement_pages` in the golden manifest scores the locator and is
   never an input to it. No per-issuer rules.
2. **Right, or visibly unsure.** On an unseen document the tool either produces figures that
   pass their own checks, or it shows what it extracted, marks what failed, and does not
   narrate (N4). Balance sheet identity, subtotal sums and scale sanity are the self-test.
3. **Scanned documents run live.** A fast OCR pass locates statement pages, and an accurate
   pass reads only those pages. Budget: under 1 minute for a digital report, 2 to 4 minutes
   for a scanned filing, with a stage timeline on screen.
4. **Scores come from documents nobody tuned against.** See the pools below.

## Scope

| In | Out (say so plainly when demonstrating) |
|----|------------------------------------------|
| Statement of financial position and profit or loss | Cash flow and equity statements (stretch) |
| English and Arabic, digital and scanned, detected per page | Other languages |
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
| 3: Oct 10 to 16 | SEC FSDS and bilingual-pair datasets, issuer-grouped split, length audit. Baseline vs QLoRA for normalization on the train holdout, with training data smaller by the holdout share. Grounding checker, templates, narration in both languages. GGUF export of the promoted adapter | Gate C, provisional until the checkpoint. Normalization beats baseline on held-out issuers |
| 4: Oct 17 to 23 | Upload page, background job, stage timeline, results page (statements with click-to-source, metrics with formulas, summary with citations, right-to-left for Arabic). Docker profile complete. Dry runs on the train holdout, fix the most common failure classes on non-holdout documents | Gate D. Gate E waits for the checkpoint |
| Oct 24 (planned) | Checkpoint, run by the owner: `model_test`, then `blind`, then Gates A to E confirmed | Scores frozen. The date is a plan that moves only later, never earlier than "all planned work complete and every check passing" (D13) |
| Oct 25 to 26 | Rehearsal, accuracy report, README. Which documents the rehearsal uses is an open question (note after the gates) | Two clean end-to-end runs on documents never seen before, if the open question allows it |

When a week overruns, cut inside that week. Cash flow statements are the first cut to
restore if time is left, not the first thing to add.

## Gates

- **A. Extraction.** Almarai English and Arabic figures match on every row. Balance sheet
  identity holds, or is flagged with the correct reason, on every `dev` and `model_test`
  document. Scale and currency correct on all of them.
- **B. Mapping.** Every critical item (revenue, net profit, total assets, total equity, total
  current assets and liabilities) is mapped or explicitly flagged on `dev` and `model_test`,
  in both languages. No silent wrong mapping.
- **C. Model.** Normalization on held-out issuers: critical items 99% or higher, macro-F1 at
  least 5 points above the base model. It is never pooled across sources: it is judged for SEC
  English labels, corpus English labels and corpus Arabic labels, each only if large enough, and
  a stratum that is too small is "not shown" (rules, levels and example counts:
  [04-execution-phases.md](04-execution-phases.md) 2.4). Before the checkpoint it is measured
  on the train holdout and marked provisional; at the checkpoint it is measured once on
  `model_test`, and that number is the one reported. Summaries: zero numeric claims that do not
  match a computed metric; schema-valid after at most one repair, otherwise the template.
- **D. Portability.** `docker compose up` on a clean machine runs upload to summary on one
  digital and one scanned document. Eval numbers recorded for both profiles.
- **E. Blind.** On the blind pool, every document ends in a correct result, a correct
  decline, or a visible `needs_review` with the reason. Zero confident wrong figures.

Gates A, B and E, and every dry run, are reported per period kind (annual and interim) beside
the total, and Gate C per period kind as description only (D18 in
[06-decisions-and-risks.md](06-decisions-and-risks.md); how, in
[04-execution-phases.md](04-execution-phases.md) 2.4).

The checkpoint runs once, after all planned work is complete and every check passes (D13 in
[06-decisions-and-risks.md](06-decisions-and-risks.md)). The schedule plans it for 24 October,
after week 4, run by the owner. In order: `model_test` is scored, then the `blind` pool is run
for the first time (D17), and Gates A to E are confirmed. Until then nothing is scored on
`model_test` or `blind`. Gates A and B are reported on `dev` and on dry runs of the full
pipeline on the documents of the train-holdout issuers, and marked "not yet measured on
`model_test`"; Gate C is measured on the train holdout (D16, design in
[04-execution-phases.md](04-execution-phases.md) 2.4, pool rules in
[eval/corpus/README.md](../../eval/corpus/README.md)); Gate E waits.

Run time of the checkpoint, an estimate and not measured. The budget (a target, not a limit) is
under 1 minute for a digital report and 2 to 4 minutes for a scanned one (principle 3 above). By
`eval/corpus/fetched.yaml` on 3 October, `model_test` has 65 documents (20 digital, 43 mixed, 2
scanned) and `blind` has 72 (20 digital, 44 mixed, 3 scanned, and 5 not yet fetched, taken here
as scanned). A mixed document has some pages with no text layer that need OCR, so it sits
between the two budgets. At the digital budget for mixed documents: `model_test` 63 x 1 + 2 x 4
= 71 minutes, `blind` 64 x 1 + 8 x 4 = 96 minutes, 167 minutes in all. At the scanned budget
(4 minutes) for mixed documents: `model_test` 20 x 1 + 45 x 4 = 200 minutes, `blind` 20 x 1 +
52 x 4 = 228 minutes, 428 minutes in all. So extraction on one profile takes between about 167
and about 428 minutes if every document met its budget. Not included, and not measured: model
scoring for Gate C on the SEC `model_test` rows, the Docker profile's run for Gate D, and
narration. The checkpoint is planned for one day, so the first dry run in week 2 should record
the real time per document, and the 24 October plan is revisited if that measurement says one
day is not enough.

Open question, not decided: Gate E is "every document" of the blind pool, so a checkpoint that
runs all of `blind` leaves no blind document that nobody has run, yet the rehearsal is meant to use
documents never seen before. Two options. (1) Reserve a fixed-hash subset of `blind` issuers
that the checkpoint does not run, so the rehearsal has documents nobody has run; the cost is
that Gate E is then judged on the rest, not on every document. (2) The checkpoint runs all of
`blind`, and the rehearsal uses documents found on the day from issuers outside the corpus,
which is closest to the demo itself (anyone uploads any report); the cost is that those
documents have no expected results to check against.

The price, stated once: everything measured on `model_test` and `blind` arrives at the very
end, so a bad surprise there has little time to be fixed. After the freeze a fix can only be
developed on `dev` and `train` documents (corpus rule 3), and the frozen score is then no longer
a first look. The holdout and the dry runs reduce that risk; they do not remove it.

## Risks added by this revision

| ID | Risk | Mitigation |
|----|------|------------|
| R15 | Linux OCR too weak on Arabic scans | Week 2 bake-off decides; if none passes, scanned Arabic is native-only and the demo says so. On either profile, a scanned Arabic filing that misses the scanned-page gate stays in the demo, shown as held for review with its reasons, not cut and not presented as correct (D14 in [06-decisions-and-risks.md](06-decisions-and-risks.md)); how such a filing is identified in the demo is not built yet |
| R16 | Fused adapter does not convert to GGUF cleanly | Week 1 spike, before any training run depends on it |
| R17 | Blind documents leak into development | Issuer-grouped pools enforced by `make corpus-check`; failures fixed on non-blind documents |
| R18 | Cloud environment cannot reach most filing hosts | Collection and fetch scripts run unchanged on the Mac; URL and sha256 recorded so the set reproduces |
| R19 | CPU-only narration too slow in Docker | Smaller model in the Docker profile if measured generation is under 5 tokens/s; the template summary is always available |
| R20 | Source site terms restrict automated download | Polite fetch (one request at a time, robots.txt respected), issuer and exchange sources preferred, documents never redistributed |

## Working rules

Carried from 07. Commit at the end of each working day with the gate status in the message.
