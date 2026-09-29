# Financial Statement Analysis Platform: Blueprint

Status: Draft v1, 2026-09-11. Revised 2026-09-26: see [08-revised-plan.md](08-revised-plan.md).
Scope: English and Arabic financial statements, live on documents the tool has never seen.

## Goal

Ingest financial reports (PDF, scanned or digital), extract the primary statements with
layout-preserving structure, compute a deterministic metric set, and produce a grounded
written analysis, all on a single Apple Silicon machine (M3 Pro, 18 GB unified memory).

## Architecture in brief

1. **Extraction.** docling converts only the pages that hold statements into structured
   tables, keeping cell bounding boxes. Our structuring layer turns those tables into
   canonical `Statement` objects: periods, scale, currency, hierarchy and provenance.
2. **Analytics.** pandas computes every number the product shows. matplotlib renders
   charts from the same frames.
3. **Model.** A 4-bit Qwen model with a QLoRA adapter, running on MLX, does two narrow jobs:
   mapping unfamiliar line-item labels to the canonical taxonomy, and writing prose that
   cites computed metrics by id. A grounding checker rejects any sentence that contains
   a number it cannot trace back to a computed metric.
4. **Delivery.** FastAPI serves jobs and artifacts. A React + TypeScript client handles
   upload, review, correction and analysis.

## Non-negotiables

| # | Rule | Enforced by |
|---|------|-------------|
| N1 | The language model never produces a number that the analytics engine did not compute. | `fra_model.grounding`, gate G-N1 |
| N2 | Every displayed value traces to a page region (page, bbox) or to a formula over traced values. | `Provenance` on every `Cell`; UI source viewer; gate G3 provenance audit and V12 |
| N3 | docling (PyTorch MPS) and the MLX model never compute at the same time. In the development profile, only one of them holds memory at a time. | SQLite heavy lease; session profiles (02, 2.2) |
| N4 | A statement with unresolved critical flags is never narrated. | Job status `needs_review` |

## Documents

| File | Contents |
|------|----------|
| [01-constraints.md](01-constraints.md) | Measured hardware, memory and disk budgets, verified library facts, model candidates |
| [02-architecture.md](02-architecture.md) | Decisions, process topology, pipeline stages, data contract, taxonomy, metrics, grounding |
| [03-repository-layout.md](03-repository-layout.md) | Complete monorepo tree with file responsibilities |
| [04-execution-phases.md](04-execution-phases.md) | Production roadmap: phases 0 to 4 with tasks, interfaces and exit gates. Reference, not the current build |
| [07-build-plan.md](07-build-plan.md) | Superseded 18-day plan to 2026-09-30. Kept for the record |
| [08-revised-plan.md](08-revised-plan.md) | **The plan being executed**: live demo on unseen documents by 2026-10-26, run profiles, data pools, gates |
| [09-ingest-locate.md](09-ingest-locate.md) | Ingest part 1: per-page text and OCR, statement page locator, industry signal, and how they are scored |
| [10-ingest-convert.md](10-ingest-convert.md) | Ingest part 2: docling on the located ranges, OCR mode and language per range, a child process per document, peak memory |
| [05-verification-and-test.md](05-verification-and-test.md) | Verification work packages (layout) and Test work packages (calculation accuracy) |
| [06-decisions-and-risks.md](06-decisions-and-risks.md) | Owner decisions, risk register, repository hygiene |

## Phase order

| Phase | Focus | Depends on |
|-------|-------|------------|
| 0 | Foundations: repo, contract, measured budgets, golden set v0 | none |
| 1 | Extraction (docling) and, in parallel after schema freeze, the deterministic analytics engine | 0 |
| 2 | MLX pipeline: baseline eval, datasets, QLoRA, grounding | 1 |
| 3 | API and UI integration, correction loop | 1, 2 |
| 4 | Arabic | 3 |
