# 09. Ingest, Part 1: Locate

Status: v1, 2026-09-27. Implements the first stage of `packages/ingest` for week 1 of
[08-revised-plan.md](08-revised-plan.md). Parts 2 (Convert) and 3 (Structure) get their own
documents.

## Why locate comes first

Extraction splits into three parts, each built and scored on its own:

| # | Part | Output |
|---|------|--------|
| 1 | **Locate**: per-page text, statement page scoring, industry signal | `LocateResult` |
| 2 | Convert: docling on candidate ranges only, page images | `docling.json`, `pages/<n>.png` |
| 3 | Structure: grid, headers to periods, hierarchy, continuation, scale and currency, checks | `list[Statement]` |

Each part consumes the previous one's output, so this is the build order. Locate decides which
pages docling reads, which is what keeps a 275-page annual report inside the time budget.

## Measured before designing

Apple Vision via ocrmac, on a scanned Juhayna statement page (2026-09-27):

| Mode | 72 dpi | 100 dpi | Output |
|------|--------|---------|--------|
| accurate, first call | 27 to 47 s | | Clean English and Arabic. The time is the model loading, once per language per process |
| accurate, warm | 0.19 to 0.24 s | 0.24 to 0.30 s | Clean English and Arabic |
| fast | 0.18 s | 0.32 s | English garbled (`Ha$$an`, `finBnci&l`); Arabic rejected as an unsupported language |

Consequences:

- One accurate pass at 72 dpi over every image page is cheap enough to locate statements. The
  fast-then-accurate scheme in 08, principle 3, is not needed, and fast mode is never used.
- The 24.7 s per page recorded in `eval/golden/README.md` was mostly model load.
- A 64-page scanned filing costs about 15 to 20 s of OCR once the model is loaded.

## Scope

| In | Out |
|----|-----|
| Per-page text layer and OCR of image pages | docling, tables, cell values (Parts 2 and 3) |
| Scoring every page for five statement types | Tesseract and RapidOCR engines (week 2 bake-off) |
| Ranges and `convert_ranges` for the enabled types | Declining banks and insurers (week 2 builds the rule on the signal stored here) |
| Industry signal: corporate, bank, insurer | Job table, workers, API |
| Labelling the 9 scanned golden documents | |
| A `sector` field on the negative controls in `eval/corpus/candidates.yaml` | |
| Scoring harness over the golden set and corpus pools | |

### Statement types

The locator detects all five `StatementType` values. Only the enabled ones are converted:

| Type | Default | Gates recall |
|------|---------|--------------|
| `balance` | enabled | yes |
| `income` | enabled | yes |
| `comprehensive_income` | enabled | yes |
| `cash_flow` | optional | no, reported only |
| `equity` | optional | no, reported only |

Enabling an optional type is a change to `configs/ingest.toml`, not to code. This keeps cash
flow as the first thing to restore (08, schedule) without spending week 1 tuning its titles.

## Components

New workspace member `packages/ingest`, package `fra_ingest`, depending on `fra-core` and
`pypdfium2`. `ocrmac` is an optional extra (`fra-ingest[mac]`) and is imported lazily, so the
package imports on Linux.

| Module | Responsibility |
|--------|----------------|
| `config.py` | `IngestConfig` loaded from `configs/ingest.toml`: enabled and optional statement types, `min_text_chars` (50), `ocr_dpi` (72), `ocr_languages` (`["ar-SA", "en-US"]`), `pad_pages` (1), `header_fraction` (0.35), `low_selectivity_share` (0.25), artifact root (`var/artifacts`) |
| `errors.py` | `IngestError(reason)` with reasons `unreadable_pdf` and `encrypted_pdf` |
| `ocr.py` | `OcrEngine` protocol: `recognize(image, languages) -> list[OcrLine]`, where `OcrLine` has text, bbox in page fraction and confidence. `VisionOcr` uses accurate mode only |
| `pages.py` | `read_pages(pdf, config, ocr) -> list[PageText]`, the page text cache, and `profiles(pages) -> list[PageProfile]` for `Document` |
| `data/statement_titles.yaml` | Per type: English and Arabic titles, Gulf and Egyptian variants, continuation cues. Negative cues for auditor's reports, contents pages and notes |
| `data/industry_cues.yaml` | Per industry: English and Arabic cues with weights, and corporate phrases that must not count (`cash and bank balances`, `prepaid insurance`) |
| `locate.py` | `score_page(page, titles) -> PageScore` and `locate(pages, config) -> LocateResult`. Pure functions over text |
| `industry.py` | `detect_industry(pages, scores) -> IndustrySignal`. Pure |
| `cli.py` | `fra-ingest locate <pdf> [--json]` writes `var/artifacts/<sha256>/locate.json` |

Supporting work outside the package:

| Path | Responsibility |
|------|----------------|
| `configs/ingest.toml` | The defaults above |
| `scripts/label_statement_pages.py` | One-off: OCR every page of the scanned golden documents and write an HTML contact sheet of every page, with suggested statement pages highlighted. The owner marks the pages; confirmed ranges go into `eval/golden/manifest.yaml` |
| `eval/harness/locate.py` | Scoring over the golden set and corpus pools (see Scoring) |

## Data model

```python
class PageText(BaseModel):          # cached
    page_no: int                    # 1-based
    mode: PageMode                  # text | image, from fra_core
    source: TextSource              # text | ocr, from fra_core
    header_text: str                # top header_fraction of the page
    body_text: str                  # the rest
    char_count: int                 # non-whitespace characters from the text layer
    width_pt: float
    height_pt: float
    arabic_chars: int
    latin_chars: int
    flags: list[str]                # ocr_failed, ocr_unavailable

class PageScore(BaseModel):
    page_no: int
    type_scores: dict[StatementType, float]
    title_hits: list[str]           # the cues that matched, for review
    numeric_tokens: int
    negatives: list[str]
    continuation: bool

class StatementRange(BaseModel):
    type: StatementType
    first_page: int                 # before padding
    last_page: int
    score: float
    rank: int                       # 1 = best candidate for this type

class IndustrySignal(BaseModel):
    kind: Literal["corporate", "bank", "insurer", "unknown"]
    score: float
    evidence: list[tuple[int, str]] # (page_no, cue)

class LocateResult(BaseModel):
    version: str                    # LOCATE_VERSION
    document: Document              # from fra_core, with page profiles and language
    pages: list[PageScore]
    ranges: list[StatementRange]
    convert_ranges: list[tuple[int, int]]  # padded, merged, enabled types only
    industry: IndustrySignal
    flags: list[str]
    timings: dict[str, float]       # seconds: text, ocr, score
```

## Data flow

```
PDF ─► read_pages ─► PageText[] ─► score_page ─► PageScore[] ─► locate ─► LocateResult
       │ pypdfium2 per page      (cached)        (pure)        │ detect_industry  (locate.json)
       │ char_count < 50: render at 72 dpi, OcrEngine
```

1. **Read.** The file's sha256 identifies the document. For each page, pypdfium2 extracts
   the header region (top 35%) and the body separately, with bidi controls removed
   (`fra_core.numbers.strip_bidi`). A page under `min_text_chars` is an image page: it is
   rendered at `ocr_dpi` and read with both OCR languages, and OCR lines are split into header
   and body by their bbox. Script counts give each page, and the document, its language.
2. **Cache.** `PageText[]` is written to `var/artifacts/<sha256>/pages.v<N>.json`, keyed by
   document, stage and stage version (ADR 0005). OCR is the only expensive step, so only it is
   cached; scoring always reruns, which keeps rule tuning to seconds per pass over a pool.
3. **Score.** For every page and type: title matches in the header text on
   `normalize_label` forms; numeric density, counted as tokens that `fra_core.numbers`
   parses with at least three digits; negative cues, which subtract. A page whose header
   carries a continuation cue (`continued`, `تابع`) is marked as a continuation.
4. **Locate.** Pages over the threshold are grouped into consecutive ranges of one type. A
   numeric page without a title, directly after a titled page, extends that page's range.
   Every candidate range is kept and ranked, since a filing can hold consolidated and
   standalone statements and an annual report can hold a highlights summary; recall comes
   before selectivity. Ranges are padded by `pad_pages`, clamped to the document, and the
   enabled types' ranges are merged into `convert_ranges`.
5. **Industry.** Cues are counted over all pages, with candidate statement pages weighted
   higher, since line items show what kind of business it is. Excluded corporate phrases are
   removed before matching. The verdict is stored; nothing is declined in this part.

## Failure handling

Every failure is visible; none turns into a blank or a guess.

| Situation | Behaviour |
|-----------|-----------|
| Not a PDF, or corrupt | `IngestError("unreadable_pdf")`; the job stops with that reason |
| Password protected | `IngestError("encrypted_pdf")` |
| No OCR engine available (Linux or Docker before week 2) | Image pages flagged `ocr_unavailable`; result flag `image_pages_not_read:<n>`. Never scored as empty pages |
| OCR fails on one page | That page is flagged `ocr_failed`; the run continues |
| An enabled type has no range | `statement_not_found:<type>`; later stages carry on and the results page shows the gap |
| No enabled type found | Empty `convert_ranges` and `no_statements_found`; the pipeline stops with that reason |
| Candidate pages above `low_selectivity_share` of the document | `low_selectivity`; the run goes ahead and the eval counts it |
| Industry verdict bank or insurer | `likely_bank` or `likely_insurer`; no decline yet |

## Testing

Tests are written before the code they cover (repository rule).

| Area | Cases | Marker |
|------|-------|--------|
| `score_page` | English and Arabic titles, Gulf and Egyptian variants. Must not score: an auditor's report quoting statement titles, a contents page, a notes heading. A continuation page. A garbled OCR title on a dense numeric page, found by numeric density | fast |
| `locate` | Grouping, continuation, padding clamped at both ends, `convert_ranges` following config, every flag in the failure table | fast |
| `detect_industry` | Bank and insurer in both languages. Stay `corporate`: `cash and bank balances`, `prepaid insurance` | fast |
| `read_pages` | Juhayna EN standalone pp. 3 to 5 are `image` and the rest `text`; Almarai AR has no bidi controls left; a fake `OcrEngine` receives exactly the image pages; the cache is reused on a second call | `golden` |
| `VisionOcr` | One English and one Arabic scanned page; skipped when ocrmac is not installed | `slow` |
| CLI | `fra-ingest locate` writes a `locate.json` that validates against `LocateResult` | `golden` |
| Errors | Truncated file gives `unreadable_pdf` | fast |

## Scoring

`make eval-locate` runs `eval/harness/locate.py`.

**Golden set**, once the 9 scanned documents are labelled: recall per type, candidate share
(pages in `convert_ranges` over document pages), and time. Manifest keys map to types as
`financial_position` to `balance`, `profit_or_loss` to `income`, `comprehensive_income` to
`comprehensive_income`, `cash_flows` to `cash_flow` and `changes_in_equity` to `equity`. A type
is scored only on documents that label it: comprehensive income printed inside the profit or
loss page has no key of its own and is covered by `income`. A labelled page counts as found
when it falls inside a padded range of its own type; for enabled types that range must also be
inside `convert_ranges`.

**Corpus pools**, which have no page labels: for each corporate document, whether at least one
`balance` and one `income` range was found; candidate share; and industry verdicts against
`sector` in `eval/corpus/candidates.yaml`, as a confusion table. `role: negative_control` is
wider than banks and insurers (it also holds investment holdings, brokerages and exchange
operators), so each negative control gets `sector: bank | insurer | other_financial`. Only
`bank` and `insurer` have a target here; how `other_financial` is treated is part of the week 2
decline rule. Development runs use `dev` and
`train`. `model_test` runs at checkpoints only. The harness refuses `blind` (R17).

### Done when

| Measure | Target |
|---------|--------|
| Recall on the enabled types, golden set | 100% |
| Median candidate share, golden set | 15% or less |
| Corporate documents with a balance and an income range, `train` | 95% or more, every miss explained |
| `sector: bank` or `insurer` documents given that verdict, `train` (6 documents) | all 6 |
| Corporate documents marked bank or insurer, `train` | at most 2 |
| Time, digital annual report | under 10 s |
| Time, 64-page scanned filing, OCR model loaded | under 30 s |

## Labelling the scanned golden documents

The 9 documents with `statement_pages: null` are labelled before the locator is scored
against them, because `statement_pages` is an answer key and never an input (08, principle 1).

1. `scripts/label_statement_pages.py` OCRs every page with `VisionOcr` and writes
   `var/labels/<doc id>.html`: a thumbnail of every page with its page number, pages holding a
   statement title highlighted as suggestions.
2. The owner marks the pages of each statement from the full sheet, including any the
   suggestions missed, and the ranges are written into `manifest.yaml`.
3. Step 1 reads pages through `read_pages` with the configured dpi and languages, so it fills
   the page cache and the locator's first run on these documents costs no OCR.
4. A comprehensive income statement on its own page gets its own `comprehensive_income` key,
   as in `juhayna-2025-en-standalone`.

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R21 | Vision's result depends on the order of the language list, degrading one script when both are given | Checked first in the plan on an English and an Arabic scan. If it degrades, OCR runs with the script guessed from a 36 dpi pass |
| R22 | Statement titles appear in only one language on bilingual pages, or as images | Numeric density and continuation still find the page; the eval shows every miss with its page scores |
| R23 | Label suggestions bias the owner toward the same pages the locator would find | The sheet shows every page, not only the suggestions |
