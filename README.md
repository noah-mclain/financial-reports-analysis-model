# Financial reports analysis

Upload an annual or interim financial report (PDF, digital or scanned). The pipeline finds the
statement pages, extracts the primary statements with a page region behind every figure,
computes a fixed set of ratios, and writes a short summary whose financial metric values are
computed by the analytics engine. Source-period dates and duration in months are copied from
the statement periods; they are metadata, not computed financial metrics.

Scored scope is English documents (D21). Uploads identified as mainly Arabic are declined;
the Arabic code, corpus and training pool are kept. Bank and insurer uploads are declined
when the industry verdict is sufficiently certain. Uncertain or unsettled classifications are
held for review and continue through conversion and structuring.

## How it works

1. **Locate.** Per-page text or OCR, then statement-page scoring (`packages/ingest`).
2. **Convert.** docling converts only the located pages, in a child process.
3. **Structure.** Table grid, periods, scale, currency, hierarchy, subtotal checks, and label
   mapping to a 47-item canonical taxonomy. Every cell keeps its page and bounding box.
4. **Metrics.** Deterministic `Decimal` calculations (`packages/analytics`).
5. **Summary.** Template text checked by a grounding checker that rejects any financial metric
   value not traced to a computed metric (`packages/modeling`); source-period dates and duration
   metadata come from statement periods. A statement with unresolved critical flags is
   held as `needs_review` and is not narrated.

The design is in [docs/blueprint](docs/blueprint/README.md); the plan being executed is
[08-revised-plan.md](docs/blueprint/08-revised-plan.md).

## Layout

| Path | Contents |
| --- | --- |
| `packages/core` | Shared schemas, parsers, taxonomy, pool split rules. Depends on nothing else |
| `packages/ingest` | Locate, convert, structure |
| `packages/analytics` | Identity checks and metrics |
| `packages/modeling` | Summary templates and the grounding checker |
| `apps/api`, `apps/worker` | Upload page, job API, background worker |
| `eval` | Corpus pools, golden set, gate harness |
| `training` | Normalization dataset and training tooling |

## Run it

Requires macOS on Apple Silicon for the native profile, and `uv`.

```sh
make setup            # workspace into .venv
make dev              # API and upload page on FRA_API_PORT (set in .env)
make worker           # in a second terminal: runs queued jobs
make smoke PDF=report.pdf   # one PDF through an isolated API and worker, with live stage events
```

Docker profile (API and worker, Tesseract OCR):

```sh
make docker-up        # needs Docker running
make docker-health
```

Both profiles read their ports from `.env`. The optional `llm` compose service needs a model
file at `var/models/model.gguf` and starts only with `--profile llm`
([14-run-profiles.md](docs/blueprint/14-run-profiles.md)).

## Checks

```sh
make test lint typecheck docs-check   # all four must pass before every commit
make corpus-check                     # corpus pool rules, no network
```

## Evaluation

Gates A to E and the pool rules are defined in
[08-revised-plan.md](docs/blueprint/08-revised-plan.md) and
[eval/corpus/README.md](eval/corpus/README.md). Nobody develops against the `blind` pool.
Measured results, with their dates and limits, are in
[docs/accuracy-report.md](docs/accuracy-report.md).
