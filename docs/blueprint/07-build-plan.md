# 07. Build Plan: 18 Days

> Superseded on 2026-09-26 by [08-revised-plan.md](08-revised-plan.md). Kept for the record.

This was the plan being executed. [04-execution-phases.md](04-execution-phases.md) remains the
production roadmap and is not being built in full.

**Target:** 2026-09-30. **Started:** 2026-09-12. Full days available.
**Deliverable:** a working tool that reads a published financial statement in English or Arabic
and produces a grounded summary with metrics, plus this plan set as the engineering record.

## Scope

| In | Out (say so plainly when demonstrating) |
|----|------------------------------------------|
| Statement of financial position and profit or loss | Cash flow and equity statements (stretch, day 17 if free) |
| English and Arabic, both as first-class | Other languages |
| Digital and scanned PDFs, detected per page | Fine-tuning. The data pipeline design is in 04 |
| The golden set in `eval/golden/` (Almarai, Juhayna, Edita) | Documents outside that set, until they are vetted the same way |
| ~12 ratios, deterministic, with identity checks | Full 24-metric registry from 02 |
| Grounded summary on the base model | Corrections UI, export, auth, multi-user |
| Cross-language verification (same figures from both editions) | Banks and insurers: detected and declined |

## Why OCR is in scope

Eight of the ten Egyptian filings collected have no text layer, and two are mixed. The tool
would fail on its own target market without OCR. See `eval/golden/README.md` for the
measurements behind this.

## Days

| Day | Work | Done when |
|-----|------|-----------|
| 1 | Repo, workspace, contract schemas, taxonomy seed with English and Arabic aliases | `make test` green on the contract package |
| 2 | `numbers.py`, `periods.py`, `units.py` with both scripts and both separator styles | Parser table tests pass, including Arabic-Indic cases |
| 3 | Per-page text-layer detection, OCR policy, docling converter, page locator | Statement pages found in Almarai (both editions) and Juhayna |
| 4 | Table grid from docling cells: geometry, header rows, note column | Almarai English financial position extracted with provenance |
| 5 | Periods, scale, currency, hierarchy, right-to-left column binding | Almarai Arabic extracted, columns bound by header not position |
| 6 | Scanned path end to end through OCR | Juhayna and Edita balance sheets extracted |
| 7 | Cross-language check, identity checks, bounding-box review report | Gate A |
| 8 | Tidy frame, identity rules, 12 metrics with policy flags | Metrics match hand-computed values on Almarai |
| 9 | Label matcher: lexicon, then embeddings, then escalation | Critical items mapped on all golden documents |
| 10 | Charts and the summary payload builder | Payload contains only computed values |
| 11 | MLX runtime, summary prompts in both languages, grounding checker | Gate C |
| 12 | API: upload, jobs, artifacts, progress stream | Upload to result over HTTP |
| 13 | UI: upload, stage timeline, statements table, source viewer | Click a number, see it highlighted in the page |
| 14 | UI: metrics, summary with citations, charts, right-to-left layout | Arabic document renders correctly |
| 15 | Demo profile: preflight, production build, warm-up, preprocessed documents | Gate D |
| 16 | Accuracy report, README, architecture summary for the panel | Numbers in the report reproduce from `make eval` |
| 17 | Rehearsal, fixes, cash flow statement if free | Two clean end-to-end runs |
| 18 | Buffer | — |

## Gates

**Gate A, day 7. Extraction.**
- Almarai English and Arabic produce identical figures for every line item on the financial
  position and profit or loss statements. Target: 100% of matched rows, with any mismatch
  explained in writing.
- Balance sheet identity holds within tolerance on every golden document, or is flagged with
  the correct reason.
- Scale and currency correct on every document.

**Gate B, day 9. Mapping.** Every critical canonical item (revenue, net income, total assets,
total equity, total current assets and liabilities) is mapped on all golden documents, in both
languages, or explicitly flagged as unmapped. No silent wrong mapping.

**Gate C, day 11. Summary.** Zero numeric claims in generated text that do not match a computed
metric, measured over all golden documents in both languages. Schema-valid output after at most
one repair, otherwise the deterministic template.

**Gate D, day 15. Demo.** With all other apps closed: upload, extract, compute, summarize, all
visible in the UI, on a digital document live and a scanned document from stored results. Peak
memory within the demo budget, no swap growth.

## Working rules

1. Tests first for anything that parses or computes. Parsers and metrics are where silent
   wrongness hides.
2. Every number displayed carries provenance: a page and bounding box, or a formula over values
   that have one.
3. The model never performs arithmetic, and never sees a number that pandas did not compute.
4. Commit at the end of each day with the day's gate status in the message.
5. When a day overruns, cut scope inside that day rather than borrowing from tomorrow. The
   buffer is one day, and it is already allocated to day 18.

## Demo sequence (rehearse on day 17)

1. Upload the Almarai Arabic edition live. It is digital, so extraction is quick.
2. Show the statements table with a value clicked through to its place on the page.
3. Show the metrics with their formulas and inputs.
4. Show the Arabic summary, then the English one, and point out that both cite computed metrics.
5. Show the cross-language check: the same figures extracted from two language editions.
6. Show a stored result from a scanned Egyptian filing, and explain the OCR cost honestly.
7. Upload a bank's statements and show the tool declining them, with the reason.
