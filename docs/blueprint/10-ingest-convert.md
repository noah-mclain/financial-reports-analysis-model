# 10. Ingest, Part 2: Convert

Status: v1, 2026-09-28. Implements the second stage of `packages/ingest` for week 1 of
[08-revised-plan.md](08-revised-plan.md), following task 1.2 of
[04-execution-phases.md](04-execution-phases.md). Part 1 is [09-ingest-locate.md](09-ingest-locate.md);
Part 3 (Structure) gets its own document.

## What convert does

| # | Part | Output |
|---|------|--------|
| 1 | Locate: per-page text, statement page scoring, industry signal | `locate.json` |
| 2 | **Convert**: docling on `convert_ranges` only, page images | `convert.json`, `docling/p<a>-<b>.json`, `pages/<n>.png` |
| 3 | Structure: grid, headers to periods, hierarchy, continuation, scale and currency, checks | `list[Statement]` |

docling is the slowest and heaviest stage. It reads only the pages locate chose (a median of
10.7% of a golden document), and it runs in a child process per document so the memory held by
PyTorch on the GPU returns to the system when the child exits (ADR 0007).

## Scope

| In | Out |
|----|-----|
| docling 2.126.0, pinned exactly, with the options of 04, 1.2 | Tables to grids, periods, hierarchy (Part 3) |
| OCR mode and OCR language chosen per range | Tesseract and RapidOCR in docling (week 2 bake-off) |
| Page images at `images_scale = 2.0` for the pages converted | The `fra_worker` package, job table and heavy lease (week 4) |
| A child process per document, with its peak memory recorded | Table model comparison and the VLM re-read (04, 1.9) |
| `fra-ingest convert` CLI | Docker parity: the seams are here, the Linux OCR engine is not |
| Conversion harness over the golden set | Notes pages (09, Later scope) |

### Run profiles

Mac only this week, with the seams Docker needs. The device and the OCR engine come from
`configs/ingest.toml`:

| Setting | Mac (default) | Docker, until week 2 |
|---------|---------------|----------------------|
| `convert.device` | `mps` | `cpu` |
| `convert.ocr_engine` | `ocrmac` | `none`: text ranges convert, image ranges are skipped and flagged |

## Components

Only `converter.py` imports docling, and only inside its functions, so the fast tests never
load PyTorch.

| File | Responsibility |
|------|----------------|
| `fra_ingest/ocr_policy.py` | `plan_ranges(located, page_texts, cfg) -> list[RangePlan]`: OCR mode and language per range, in our own types |
| `fra_ingest/converter.py` | `build_converter(cfg, plan) -> DocumentConverter`, and the mapping from our types to docling's |
| `fra_ingest/convert.py` | `convert_pdf(pdf, located, cfg) -> ConvertResult`: runs docling per range and writes the artifacts |
| `fra_ingest/child.py` | `convert_in_child(pdf, cfg, timeout_s) -> ConvertResult`: runs `fra-ingest convert` in a subprocess and maps how it ended to a result or an `IngestError` |
| `fra_ingest/footprint.py` | The process's lifetime peak memory footprint |
| `fra_ingest/results.py` | `RangePlan`, `RangeConversion`, `ConvertResult` |
| `fra_ingest/cli.py` | `convert` subcommand |
| `fra_ingest/config.py`, `configs/ingest.toml` | `[convert]` section |
| `fra_ingest/errors.py` | Reasons `convert_failed`, `convert_timeout`, `convert_crashed` |
| `eval/harness/convert.py` | Conversion measurements over the golden set |

### Why the CLI is the child

`fra-ingest convert <pdf>` converts in its own process and exits. `convert_in_child` runs it
with `subprocess.run([sys.executable, "-m", "fra_ingest.cli", "convert", ...], timeout=...)`
and reads the `convert.json` it wrote. The week 4 worker calls the same helper.

- A docling crash, an out-of-memory kill or a hang takes down only the child, and the parent
  reports how it ended.
- The command is useful on its own for debugging, as `fra-ingest locate` is.
- Starting a Python process that imports PyTorch costs a few seconds per document, small
  against loading docling's models.

`multiprocessing` with `spawn` gives the same isolation but pickles arguments and exceptions
across the boundary and leaves less to diagnose when the child is killed.

## Data model

```python
RangeOcr = Literal["pdf_aware", "full_page", "skipped"]

class RangePlan(BaseModel):          # frozen
    first_page: int
    last_page: int
    ocr: RangeOcr                    # pdf_aware: every page has a text layer
    ocr_language: str | None         # "ar-SA" | "en-US"; None for pdf_aware and skipped
    image_pages: list[int]

class RangeConversion(BaseModel):
    first_page: int
    last_page: int
    ocr: RangeOcr
    ocr_language: str | None
    docling_path: str | None         # "docling/p12-15.json", relative to the artifact dir
    tables: int
    seconds: float
    status: Literal["ok", "partial", "failed", "skipped"]
    flags: list[str]

class ConvertResult(BaseModel):      # written to <artifact root>/<sha256>/convert.json
    version: str                     # "convert/1"
    sha256: str
    locate_version: str
    docling_version: str
    device: str
    settings_hash: str               # the [convert] settings, the docling version and the plans
    ranges: list[RangeConversion]
    page_images: dict[int, str]      # page_no -> "pages/12.png"
    peak_footprint_gb: float | None
    flags: list[str]
    timings: dict[str, float]        # startup, models, convert, write
```

`[convert]` settings, each mapped to an `IngestConfig` field as the existing sections are:

| Key | Default | Notes |
|-----|---------|-------|
| `device` | `"mps"` | `mps`, `cpu` or `auto` |
| `ocr_engine` | `"ocrmac"` | `ocrmac` or `none` |
| `images_scale` | `2.0` | Page images at 144 dpi |
| `batch_size` | `2` | OCR, layout and table batch sizes (01, 1.5) |
| `do_cell_matching` | `true` | Task 1.9 compares it with `false` |
| `document_timeout_s` | `600` | docling's own limit per `convert` call |
| `child_timeout_s` | `900` | The parent kills the child after this |
| `memory_budget_gb` | `3.0` | Above it the result is flagged, not failed (01, ingest child peak) |

## Data flow

Inside the child, `fra-ingest convert <pdf>`:

1. **Locate result.** Read `<sha256>/locate.json` when its version is current, otherwise run
   locate (its page cache makes that cheap). An empty `convert_ranges` writes a `convert.json`
   flagged `no_statements_found` and exits 0 without importing docling.
2. **Cache.** If `convert.json` exists with the same `settings_hash`, return it (ADR 0005).
3. **Plan.** `plan_ranges` gives each range in `convert_ranges`:
   - `pdf_aware` when every page is `text` in `located.document.pages`, else `full_page`;
   - for `full_page`, the OCR language most common in Part 1's reads of the range's image
     pages (`PageText.ocr_language` in the page cache), falling back to `ar-SA` when
     `document.language` is `ar` and `en-US` otherwise. Vision reads only in the first
     language it is given (R21), so docling gets that one language, not the pair;
   - `skipped` when the range has image pages and `ocr_engine` is `none`.
4. **Converters.** One `DocumentConverter` per distinct `(ocr, ocr_language)`, so a typical
   document loads docling's models once. `pdf_aware` maps to
   `OcrMode.PDF_AWARE_LAYOUT_REGIONS` and `full_page` to `OcrMode.FULL_PAGE`.
5. **Convert** each range with `page_range=(first, last)`. Every page docling returns must
   fall inside the range (R25). The document is saved to `docling/p<first>-<last>.json` and
   each page image to `pages/<n>.png`.
6. **Write** `convert.json` to a temporary file and rename it into place, recording the peak
   footprint and timings. The child exits.

In the parent, `convert_in_child` returns the `ConvertResult` read from disk, or raises.

## Failure handling

"Right, or visibly unsure": one bad range does not sink the document.

| Situation | Behaviour |
|-----------|-----------|
| docling raises on one range | That range `failed`, flag `convert_failed:<a>-<b>`; the other ranges go on |
| docling returns `PARTIAL_SUCCESS` (its own timeout, a page it could not read) | That range `partial`, flag `convert_partial:<a>-<b>` with docling's error text in the flag list |
| docling returns a page outside the range | That range `failed`, flag `page_outside_range:<a>-<b>` |
| Every range failed | The child writes `convert.json` and exits 3; the parent raises `IngestError("convert_failed")` |
| The child outlives `child_timeout_s` | Killed; `IngestError("convert_timeout")` |
| The child ends on a signal (out-of-memory kill) | `IngestError("convert_crashed", "signal <n>")` |
| The child exits with no `convert.json` | `IngestError("convert_crashed", <last lines of stderr>)` |
| An image range with `ocr_engine = "none"` | `skipped`, flag `ocr_unavailable:<a>-<b>` |
| Peak footprint above `memory_budget_gb` | `memory_over_budget`; the result is kept |
| No `convert_ranges` | `no_statements_found`, exit 0, docling never loads |

## Peak memory

On Apple silicon the GPU shares memory with the CPU, and memory PyTorch allocates through MPS
is not all counted in RSS. The child therefore records its lifetime peak physical footprint,
the figure Activity Monitor shows: `proc_pid_rusage(getpid(), RUSAGE_INFO_V4)` read through
`ctypes`, field `ri_lifetime_max_phys_footprint`. Elsewhere it falls back to
`resource.getrusage(RUSAGE_SELF).ru_maxrss`. The child reads it for itself just before
writing, because once it exits the figure is gone (R26).

## Testing

Tests are written before the code they cover (repository rule).

| Area | Cases | Marker |
|------|-------|--------|
| docling contract | Task 1: every import and option name in 04, 1.2 exists in the pinned version; `page_range` keeps the document's page numbers | `slow` |
| `plan_ranges` | All text pages; one image page makes the range `full_page`; language by majority of reads; fallback on `document.language`; `ocr_engine = "none"` skips image ranges and keeps text ranges | fast |
| `ConvertResult` | Round trip through JSON; `settings_hash` changes with any setting and with the plans | fast |
| Config | `[convert]` keys load; an unknown key is rejected | fast |
| Cache | A matching `convert.json` is returned without converting; a changed setting converts again | fast |
| `convert_in_child` | A fake child command that writes a result, exits 3, sleeps past the timeout, kills itself, or exits without writing | fast |
| CLI | `fra-ingest convert` arguments, with `convert_pdf` replaced | fast |
| `footprint` | Returns a positive figure on this machine | fast |
| Conversion | Almarai EN balance sheet range: page numbers are the document's, a PNG per page at scale 2, at least one table | `slow`, `golden` |

## Scoring

`make eval-convert` runs `eval/harness/convert.py` over the 12 golden documents, each in its
own child. For each document it records seconds per converted page, the peak footprint, the
statuses and tables per range, and the flags.

### Done when

| Measure | Target |
|---------|--------|
| docling contract smoke test | passes against 2.126.0 |
| Golden documents converted with every range `ok` | 12 of 12 |
| Peak footprint of the child, every golden document | 3.0 GB or less |
| Seconds per candidate page | recorded; the budget in 08 (1 minute digital, 2 to 4 minutes scanned, whole pipeline) is checked in Part 3 |

The measurements go into a Results section here, as they did for Part 1.

## Setup cost

The first task adds docling and its dependencies (PyTorch 2.14, transformers 5.17 and
docling-ibm-models 4.0.2 among them, about 4 GB in `.venv`). The first conversion downloads
docling's layout and table models, about 1 GB. Both are one-time and inside the disk budget
of 01, 1.4.

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R25 | `page_range` renumbers pages from 1, which would put provenance on the wrong page | The task 1 smoke test asserts the numbering, and every conversion checks each page it returns against its range |
| R26 | RSS misses MPS allocations, so the memory gate passes on a figure that is too low | The child records its lifetime peak physical footprint, which includes them |
| R27 | docling passes its OCR languages to Vision in order, and Vision reads only the first (R21) | One language per range, taken from Part 1's reads of those pages |
| R28 | A range mixing scanned and text pages is OCR'd whole, costing time on its text pages | Recorded per range; if the golden set shows it matters, split mixed ranges at mode changes |
