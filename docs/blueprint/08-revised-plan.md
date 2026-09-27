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
| OCR | Apple Vision via ocrmac, accurate mode | Tesseract or RapidOCR, chosen by the week 2 bake-off | `fra_ingest.ocr.OcrEngine` |
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
| 2: Oct 3 to 9 | OCR engines behind one interface, bake-off on the golden scans (Vision vs Tesseract vs RapidOCR). Label mapping: aliases, fuzzy match, structural anchors. Identity checks, 12 metrics with D1 to D6 defaults, decline for banks and insurers. First blind run | Gate A and Gate B on `dev` and `model_test`. OCR engine chosen for Docker with measured accuracy |
| 3: Oct 10 to 16 | SEC FSDS and bilingual-pair datasets, issuer-grouped split, length audit. Baseline vs QLoRA for normalization on `model_test`. Grounding checker, templates, narration in both languages. GGUF export of the promoted adapter | Gate C. Normalization beats baseline on held-out issuers |
| 4: Oct 17 to 23 | Upload page, background job, stage timeline, results page (statements with click-to-source, metrics with formulas, summary with citations, right-to-left for Arabic). Docker profile complete. Blind runs, fix the most common failure classes on non-blind documents | Gate D and Gate E |
| Oct 24 to 26 | Rehearsal on fresh blind documents, accuracy report, README | Two clean end-to-end runs on documents never seen before |

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
  least 5 points above the base model. Summaries: zero numeric claims that do not match a
  computed metric; schema-valid after at most one repair, otherwise the template.
- **D. Portability.** `docker compose up` on a clean machine runs upload to summary on one
  digital and one scanned document. Eval numbers recorded for both profiles.
- **E. Blind.** On the blind pool, every document ends in a correct result, a correct
  decline, or a visible `needs_review` with the reason. Zero confident wrong figures.

## Risks added by this revision

| ID | Risk | Mitigation |
|----|------|------------|
| R15 | Linux OCR too weak on Arabic scans | Week 2 bake-off decides; if none passes, scanned Arabic is native-only and the demo says so |
| R16 | Fused adapter does not convert to GGUF cleanly | Week 1 spike, before any training run depends on it |
| R17 | Blind documents leak into development | Issuer-grouped pools enforced by `make corpus-check`; failures fixed on non-blind documents |
| R18 | Cloud environment cannot reach most filing hosts | Collection and fetch scripts run unchanged on the Mac; URL and sha256 recorded so the set reproduces |
| R19 | CPU-only narration too slow in Docker | Smaller model in the Docker profile if measured generation is under 5 tokens/s; the template summary is always available |
| R20 | Source site terms restrict automated download | Polite fetch (one request at a time, robots.txt respected), issuer and exchange sources preferred, documents never redistributed |

## Working rules

Carried from 07. Commit at the end of each working day with the gate status in the message.
