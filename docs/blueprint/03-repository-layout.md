# 03. Repository Layout

Python packages use the `fra_` prefix (financial reports analysis). Python is managed as one
uv workspace with one lockfile (resolution verified, section 01, 1.5). TypeScript is managed
as a pnpm workspace.

## 3.1 Tree

```text
financial-reports-analysis-model/
├── Makefile                          # setup, dev, test, eval, train-smoke, train, disk-report, clean-store
├── README.md
├── pyproject.toml                    # uv workspace root; ruff, mypy, pytest config
├── uv.lock
├── .python-version                   # 3.12
├── package.json                      # pnpm root scripts
├── pnpm-workspace.yaml               # apps/web, packages/api-client
├── pnpm-lock.yaml
├── .editorconfig
├── .gitattributes                    # eval/golden/documents/** filter=lfs
├── .gitignore                        # project paths only: var/, .venv/, node_modules/, training data and weights
├── .pre-commit-config.yaml           # ruff, ruff-format, mypy, eslint, prettier, size guard
│
├── configs/
│   ├── runtime.toml                  # memory thresholds, lease timings, model and adapter ids, paths
│   ├── ingest.toml                   # OCR engine and languages, table model, batch sizes, images_scale
│   ├── analytics.toml                # owner decisions that are settings: D1 margin, D3, D4; D2, D5, D6 are rules in code
│   ├── profiles/
│   │   ├── dev.toml                  # per-document ingest child, model unloads when idle
│   │   ├── demo.toml                 # persistent ingest process, model kept loaded, preflight thresholds
│   │   └── training.toml             # services must be stopped, preflight thresholds
│   └── training/
│       ├── smoke.yaml                # 20 iterations, peak-memory probe
│       ├── normalize.yaml
│       └── narrate.yaml
│
├── docs/
│   ├── blueprint/                    # this document set
│   ├── adr/                          # 0001 to 0008 from section 02, 2.1
│   ├── metrics.md                    # generated from the registry
│   └── runbooks/
│       ├── memory-and-disk.md        # Phase 0 measurement tables
│       ├── demo.md                   # pre-demo checklist, preprocessed vs live documents
│       ├── training.md
│       └── golden-set-annotation.md
│
├── packages/
│   ├── core/                         # fra-core: contract, no heavy dependencies
│   │   ├── pyproject.toml
│   │   ├── src/fra_core/
│   │   │   ├── schemas/{document,statement,metric,narrative,job}.py
│   │   │   ├── taxonomy/{canonical_items.yaml,loader.py}
│   │   │   ├── numbers.py            # printed number -> Decimal: parentheses, dashes, U+2212, thin spaces, locale digits
│   │   │   ├── units.py              # scale phrases, ISO currency detection
│   │   │   ├── periods.py            # header text -> Period
│   │   │   ├── split.py              # issuer split of train: fit, validation, holdout, pure rules (04, 2.4)
│   │   │   ├── tolerance.py          # the D6 identity tolerance, n x 0.5 reported units, used by ingest and analytics
│   │   │   └── artifacts.py          # content-addressed paths, stage version registry
│   │   └── tests/
│   │
│   ├── ingest/                       # fra-ingest: the only package importing docling
│   │   ├── pyproject.toml
│   │   ├── src/fra_ingest/
│   │   │   ├── cli.py                # fra-ingest locate|convert|structure|review-report
│   │   │   ├── config.py             # IngestConfig loaded from configs/ingest.toml
│   │   │   ├── locate.py             # text pass -> candidate statement page ranges
│   │   │   ├── ocr_policy.py         # per-page text-layer coverage -> OcrMode
│   │   │   ├── converter.py          # DocumentConverter factory from configs/ingest.toml
│   │   │   ├── table_grid.py         # TableItem.data.table_cells -> GridCell matrix with bboxes and header flags
│   │   │   ├── classify.py           # grid + nearby headings -> StatementType, confidence
│   │   │   ├── header.py             # header rows -> periods, note-reference column, restated markers
│   │   │   ├── hierarchy.py          # label x-offset and subtotal cues -> depth, parent, is_subtotal
│   │   │   ├── continuation.py       # merge a statement across consecutive pages
│   │   │   ├── metadata.py           # scale, currency, entity, consolidated flag
│   │   │   ├── table_checks.py       # subtotal sums, column-to-header geometry
│   │   │   ├── structure.py          # orchestration -> list[Statement]
│   │   │   └── review_report.py      # HTML report with extracted cells drawn on page images
│   │   └── tests/
│   │
│   ├── analytics/                    # fra-analytics: pure margins built; broader engine planned
│   │   ├── pyproject.toml
│   │   ├── src/fra_analytics/
│   │   │   ├── frame.py              # statements -> tidy frame of typed rows (statement, canonical_id, period, reported, scale, value, provenance); no pandas yet
│   │   │   ├── policy.py             # Policy (D1 margins, D3, D4) loaded from the flat configs/analytics.toml; no defaults
│   │   │   ├── identities.py         # balance identity and subtotal ties, D6 tolerance from fra_core.tolerance
│   │   │   ├── unit_caveats.py       # scale and currency caveats and flags carried into each metric
│   │   │   ├── period_math.py        # opening balance, prior-year period, actual days
│   │   │   ├── metrics/profitability.py  # compute_margins(Statement, policy): gross, operating, net, the one margin formula
│   │   │   ├── metrics/division.py   # the one ratio division and its D1 flags
│   │   │   ├── metrics/{registry,inputs}.py   # formulas; the reader that applies D1, D2, D4 and records provenance (cash-flow metrics not built: structure converts no cash-flow statement)
│   │   │   ├── formatting.py         # display precision, shared with grounding and UI payloads
│   │   │   ├── charts/{style,trend,margins,composition,waterfall}.py
│   │   │   └── reference/naive.py    # independent Decimal implementation, imported only by tests
│   │   └── tests/
│   │
│   ├── modeling/                     # fra-model: the only package importing mlx
│   │   ├── pyproject.toml
│   │   ├── src/fra_model/
│   │   │   ├── runtime.py            # load and unload base plus adapter, wired and cache limits, peak memory log
│   │   │   ├── registry.py           # adapter manifests: base id and revision, data hash, config hash, eval scores
│   │   │   ├── prompts/{normalize,narrate,repair}.j2
│   │   │   ├── lexicon.py            # exact and normalized-string label lookup before any model call
│   │   │   ├── normalize.py          # residual labels -> canonical ids
│   │   │   ├── narrate.py            # metrics payload -> Narrative
│   │   │   ├── structured.py         # parse, validate, one repair, fallback
│   │   │   ├── grounding.py          # section 02, 2.7
│   │   │   └── templates.py          # deterministic fallback sentences
│   │   └── tests/
│   │
│   └── api-client/                   # generated TypeScript client
│       ├── package.json
│       ├── openapi.json              # committed snapshot
│       └── src/{schema.d.ts,client.ts}
│
├── apps/
│   ├── api/                          # fra-api
│   │   ├── pyproject.toml
│   │   ├── src/fra_api/
│   │   │   ├── main.py
│   │   │   ├── settings.py
│   │   │   ├── db/{engine,tables}.py
│   │   │   ├── db/migrations/        # alembic
│   │   │   ├── routes/{documents,jobs,analyses,corrections,pages,health}.py
│   │   │   ├── jobs.py               # enqueue, transitions, SSE stream
│   │   │   └── storage.py            # upload validation, sha256 dedupe, var/store layout
│   │   └── tests/
│   │
│   ├── worker/                       # fra-worker
│   │   ├── pyproject.toml
│   │   ├── src/fra_worker/
│   │   │   ├── main.py               # fra-worker --role ingest|analysis
│   │   │   ├── lease.py              # heavy lease with heartbeat
│   │   │   ├── ingest_process.py     # convert runner: child per document (dev) or persistent (demo)
│   │   │   ├── profiles.py           # loads configs/profiles/<name>.toml
│   │   │   ├── preflight.py          # fra-worker preflight --profile demo|training: memory, disk, running services
│   │   │   ├── stages.py             # stage table: name, role, version, heavy, callable
│   │   │   └── memory_guard.py       # free-memory check before heavy stages
│   │   └── tests/
│   │
│   └── web/                          # React, TypeScript, Vite
│       ├── package.json
│       ├── vite.config.ts
│       ├── index.html
│       ├── src/
│       │   ├── main.tsx
│       │   ├── app/{router,providers}.tsx
│       │   ├── pages/{UploadPage,DocumentPage,AnalysisPage}.tsx
│       │   ├── features/
│       │   │   ├── upload/{Dropzone.tsx,useUpload.ts}
│       │   │   ├── jobs/{StageTimeline.tsx,useJobEvents.ts}
│       │   │   ├── statements/{StatementTable,SourceViewer,MappingEditor}.tsx
│       │   │   ├── metrics/{MetricGrid,MetricDetail}.tsx
│       │   │   ├── narrative/{NarrativePanel,ClaimChip}.tsx
│       │   │   ├── charts/ChartPanel.tsx
│       │   │   └── review/FlagList.tsx
│       │   ├── lib/{api,format,i18n}.ts
│       │   └── styles/               # logical CSS properties only (margin-inline-start, not margin-left)
│       ├── tests/                    # Vitest, Testing Library
│       └── e2e/                      # Playwright
│
├── training/
│   ├── README.md
│   ├── sources/
│   │   ├── sec_fsds.py               # stream quarterly zips: pre (plabel, tag, stmt), sub, filtered num rows; never unpack to disk
│   │   ├── crosswalk_us_gaap.yaml    # us-gaap tag -> canonical id
│   │   └── corrections_export.py     # user corrections in SQLite -> labeled examples
│   ├── build/
│   │   ├── normalize_set.py
│   │   ├── narrate_set.py
│   │   └── length_audit.py           # fail on any example longer than max_seq_length
│   ├── eval/{normalize_eval,narrate_eval,compare_adapters}.py
│   ├── scripts/{smoke.sh,train.sh}
│   ├── data/                         # gitignored; README.md and manifests tracked
│   └── adapters/                     # weights gitignored; manifest.json per adapter tracked
│
├── eval/
│   ├── corpus/                       # pools (README.md); holdout_moves.yaml, validation_uses.yaml, scoring_log.tsv (04, 2.4)
│   ├── golden/
│   │   ├── documents/                # PDFs through git-lfs
│   │   ├── expected/                 # hand-verified Statement JSON per document
│   │   └── manifest.yaml             # id, source URL, retrieval date, layout traits
│   ├── harness/{extraction,metrics,end_to_end,report}.py
│   ├── harness/holdout_records.py    # the one reader and writer of holdout_moves.yaml, validation_uses.yaml, scoring_log.tsv
│   ├── harness/paths.py              # where the corpus records live, for the harness
│   ├── harness/dry_run.py            # first look at the train holdout: make dry-run
│   ├── reports/<wp-id>/              # Verification and Test run reports (section 05, 5.1)
│   └── thresholds.toml               # gate values read by CI and by adapter promotion
│
├── tests/
│   ├── analytics/                    # analytics on extracted golden statements (the one place it meets ingest)
│   ├── integration/                  # API, workers, SQLite, stub model runtime
│   └── contract/                     # OpenAPI snapshot vs generated client; import boundaries
│
├── scripts/
│   ├── bootstrap.sh                  # uv sync, corepack pnpm install, git lfs pull, fetch models
│   ├── fetch_models.py               # pinned repo ids and revisions into HF_HOME
│   ├── bench_memory.py               # Phase 0 measurements
│   └── disk_report.py
│
└── var/                              # gitignored runtime state
    ├── fra.sqlite
    ├── hf/                           # HF_HOME
    └── store/<sha256>/<stage>@<version>/
```

## 3.2 Dependency direction

```text
fra-core  <-  fra-ingest     (docling, pypdfium2, ocrmac)
fra-core  <-  fra-analytics  (matplotlib, when the charts land; no pandas, D19)
fra-core  <-  fra-model      (mlx, mlx-lm, jinja2)  <- fra-analytics (formatting only)
fra-core  <-  fra-api        (fastapi, uvicorn, sqlalchemy, alembic)
fra-core, fra-ingest, fra-analytics, fra-model  <-  fra-worker
packages/api-client  <-  apps/web
```

Enforced by `import-linter` contracts in `tests/contract`:

- `fra_api` must not import `docling`, `torch`, `mlx` or `fra_ingest`.
- `fra_analytics` must not import `mlx` or `docling`.
- Only `fra_ingest` imports `docling`; only `fra_model` imports `mlx`.

## 3.3 Workspace roots

```toml
# pyproject.toml
[tool.uv.workspace]
members = [
  "packages/core",
  "packages/ingest",
  "packages/analytics",
  "packages/modeling",
  "apps/api",
  "apps/worker",
]

[dependency-groups]
dev = ["pytest", "pytest-cov", "hypothesis", "mypy", "ruff", "import-linter", "schemathesis", "pytest-mpl"]
```

```yaml
# pnpm-workspace.yaml
packages:
  - apps/web
  - packages/api-client
```

## 3.4 Make targets

| Target | Action |
|--------|--------|
| `make setup` | `scripts/bootstrap.sh` |
| `make dev` | api, ingest worker, analysis worker, Vite (development profile) |
| `make demo` | preflight, production web build served by the API, both workers, runtime warm-up (demo profile) |
| `make test` | pytest across the workspace, Vitest, contract tests |
| `make eval` | `eval/harness/*` against `eval/thresholds.toml` |
| `make train-smoke MODEL=...` | 20-iteration QLoRA run, prints peak memory |
| `make train TASK=normalize` | Preflight (training profile), then the full run with the manifest written |
| `make disk-report` | Sizes of `var/hf`, `var/store`, `training/`, `.venv`, `node_modules` |
