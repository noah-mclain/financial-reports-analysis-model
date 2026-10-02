# Extraction status brief

Sep 28, 2026 · @n

The data foundation and the first extraction stage are done: statement pages are found in English and Arabic filings, digital or scanned, at 100% recall on the golden set. What remains is turning those pages into structured statements, then metrics, narration and the live demo by Oct 26, 2026.

## Completed

Four pieces are done; ingest part 1 is on the `ingest-locate` branch, pushed and awaiting merge.

| Piece | What it delivers | Where it lives |
| --- | --- | --- |
| Shared contract | Schemas for documents, statements with provenance on every value, metrics and narratives; parsers for numbers (Arabic-Indic digits, space separators), periods, units and labels; a 47-item bilingual taxonomy | `packages/core` (merged) |
| Corpus | 229 documents from 145 issuers in Saudi Arabia, Egypt, the UAE, Kuwait, Bahrain, the UK and the US, split by issuer into `dev`, `train`, `model_test` and `blind`; banks, insurers and other financial companies labelled by sector | `eval/corpus`, `scripts/corpus.py` (merged; sector labels on the branch) |
| Golden set | 12 hand-vetted Almarai, Juhayna and Edita filings in both languages, digital and scanned, with statement pages labelled for all 12 | `eval/golden` |
| Ingest part 1: locate | Per-page text or OCR (Apple Vision, Arabic first), a page cache, statement-page scoring for five statement types, the page ranges docling will convert, and a bank/insurer signal for the week 2 decline rule; `fra-ingest locate` CLI; blind labelling tool; locator eval; 322 fast tests; reviewed branch-wide with every finding fixed | `packages/ingest`, `eval/harness/locate.py`, design and results in `docs/blueprint/09-ingest-locate.md` |

## Part 1 results

Every target passes except English scan time, which runs about 10% over because each scanned page is read twice.

| Measure | Target | Result |
| --- | --- | --- |
| Statement-page recall, golden set (balance, income, comprehensive income) | 100% | 100% on all 12 documents |
| Median share of pages sent to conversion | 15% or less | 10.7% |
| Corporate filings with a balance and an income range, `train` | 95% or more | 100% (109 of 109) |
| Banks and insurers recognised, `train` | 6 of 6 | 6 of 6 |
| Ordinary companies marked bank or insurer | at most 2 | 0 |
| Digital annual report, 275 pages | under 10 s | 2.2 to 3.6 s |
| Scanned filing, 64 pages | under 30 s | Arabic 13 to 17 s; English about 31 to 33 s |

The golden set is not held out, since tuning looked at golden pages; the held-out score waits for the single `model_test` checkpoint, taken after all planned work is complete (D13).

## To be completed

The immediate work is ingest parts 2 and 3, which close week 1 by extracting Almarai's English and Arabic statements with identical figures.

| Window | Work | Done when |
| --- | --- | --- |
| Now to Oct 2 (week 1) | **Part 2, Convert:** docling on the located page ranges only, page images, one child process per document | The converter's imports and options pass a smoke test against the pinned docling version; the ranges from `locate.json` convert within the ingest memory budget |
| Now to Oct 2 (week 1) | **Part 3, Structure:** table grid with cell boxes, headers bound to periods (never to column position), hierarchy and subtotals, statements continued across pages, scale and currency, subtotal checks, review report | Almarai English and Arabic extracted with identical figures |
| Now to Oct 2 (week 1) | **Docker skeleton** (base image, compose, both run profiles start) and a **spike** fusing an MLX LoRA adapter and running it as GGUF in llama.cpp | `docker compose up` serves a health page; the spike answers risk R16 |
| Oct 3 to 9 (week 2) | OCR engines behind one interface and a bake-off (Vision, Tesseract, RapidOCR); label mapping; identity checks and about 12 metrics; declining banks and insurers from the stored industry signal; first blind run | Gate A (extraction) and Gate B (mapping) on `dev` and `model_test` |
| Oct 10 to 16 (week 3) | Training data (SEC statement data, bilingual pairs), QLoRA label normalization against the base model, grounding checker, narration in both languages, GGUF export | Gate C (model) |
| Oct 17 to 23 (week 4) | Upload page, background jobs, stage timeline, results page with click-to-source, full Docker profile, blind runs | Gate D (portability) and Gate E (blind) |
| Oct 24 to 26 | Rehearsal on fresh blind documents, accuracy report, README | Two clean end-to-end runs on unseen documents |

Notes pages stay out of scope: figures come from the primary statements, and following a line item's note reference is a later addition, only if a metric needs it.

## Files to read

Start with the revised plan for the schedule and gates, then the execution phases document for the specification of parts 2 and 3.

| File | Read it for |
| --- | --- |
| `docs/blueprint/08-revised-plan.md` | The plan being executed: week-by-week schedule, gates A to E, native and Docker run profiles, data pools, risks R15 to R20 |
| `docs/blueprint/04-execution-phases.md`, sections 1.2 to 1.9 | Parts 2 and 3 in detail: converter and OCR policy (1.2), table grid (1.3), statement classification (1.4), headers and periods (1.5), hierarchy (1.6), continuation and metadata (1.7), structure and review report (1.8), extraction eval and table-model comparison (1.9); Phase 1 exit thresholds |
| `docs/blueprint/02-architecture.md` | Pipeline stages (convert, structure and what follows), provenance on every value (ADR 0004), content-addressed stage artifacts (ADR 0005), docling and the model in separate processes (ADR 0007) |
| `docs/blueprint/03-repository-layout.md` | The `fra_ingest` modules still to write (`converter.py`, `table_grid.py`, `classify.py`, `header.py`, `hierarchy.py`, `continuation.py`, `metadata.py`, `table_checks.py`, `structure.py`) and the package dependency rules |
| `docs/blueprint/05-verification-and-test.md` | Layout verification V1 to V8 and the calculation tests the extraction output must pass |
| `docs/blueprint/01-constraints.md` | Memory and disk budgets (the ingest child's 3.0 GB peak) and pinned library versions |
| `docs/blueprint/06-decisions-and-risks.md` | Owner decisions D1 to D7 and the risk register |
| `docs/blueprint/09-ingest-locate.md` | What part 2 receives (`convert_ranges` in `locate.json`), Arabic text-order findings (R24), measured results, notes kept for later |
| `packages/core/src/fra_core/schemas/statement.py` | The `Statement`, `LineItem`, `Cell` and `Provenance` contract part 3 must fill |
| `packages/ingest/src/fra_ingest/results.py` | `LocateResult`, the input to part 2 |
| `eval/golden/manifest.yaml` and `eval/golden/README.md` | Expected scale, currency and statement pages per golden document; findings that shape part 3 (column order flips between Almarai's English and Arabic editions, currency glyphs that are not text) |
| `eval/corpus/README.md` | Pool rules: nobody develops against `blind`, and `model_test` and `blind` freeze at the first `model_test` run |
| `configs/ingest.toml` | Ingest settings; converting cash flow or equity statements is a change here |

## Open decisions

- [ ] Merge `ingest-locate`: the pull request text is ready in `var/pr/ingest-locate.md` in the worktree.
- [x] When to run the `model_test` checkpoint, the only held-out score for part 1. Running it freezes `model_test` and `blind`. Decided 2026-10-02: once, after all planned work is complete and every check passes (D13).
- [ ] English scan time: accept about 31 to 33 s per 64 pages, or decide per document which language to read first.
- [ ] Which of the about 12 ratios need a figure not on the face of the statements. Depreciation for EBITDA is the likely case, and converting the cash flow statement would cover it.
