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

- One accurate pass at 100 dpi over every image page is cheap enough to locate statements. 72 dpi
  was the first choice but left some scans nearly unread (Juhayna EN standalone p5: 6 lines
  against 124 at 100 dpi, in about the same time). The
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
| `sector` and `subsector` fields on the negative controls in `eval/corpus/candidates.yaml` | |
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
| `config.py` | `IngestConfig` loaded from `configs/ingest.toml`: enabled and optional statement types, `min_text_chars` (50), `ocr_dpi` (100), `ocr_languages` (`["ar-SA", "en-US"]`), `pad_pages` (1), `header_fraction` (0.35), `low_selectivity_share` (0.25), artifact root (`var/artifacts`). `FRA_ROOT` sets the root for configs and artifacts in an installed copy (the Docker image); `FRA_INGEST_CONFIG` names another settings file |
| `errors.py` | `IngestError(reason)` with reasons `unreadable_pdf`, `encrypted_pdf` and `empty_pdf` |
| `ocr.py` | `OcrEngine` protocol: `recognize(image, languages) -> list[OcrLine]`, where `OcrLine` has text, bbox in page fraction and confidence. `VisionOcr` uses accurate mode only. `read_with_fallback` reads in each configured language in turn until the result is in that language's script (R21) |
| `pages.py` | `read_pages(pdf, config, ocr) -> list[PageText]`, the page text cache, and `profiles(pages) -> list[PageProfile]` for `Document` |
| `data/statement_titles.yaml` | Per type: English and Arabic titles, Gulf and Egyptian variants, continuation cues. Structural cues shared by all statements: period headers, the note column header, currency and scale lines. Negative cues for auditor's reports, contents pages and notes |
| `data/industry_cues.yaml` | Per sector and sub-sector: English and Arabic cues with weights, and corporate phrases that must not count (`cash and bank balances`, `prepaid insurance`) |
| `text_match.py` | Phrase matching on `normalize_label` forms, and detection of Arabic stored in visual order |
| `locate.py` | `score_page(page, titles) -> PageScore` and `locate(pages, config) -> LocateResult`. Pure functions over text |
| `stage.py` | `locate_pdf(pdf, config, ocr)`: read, locate, time, write `locate.json` |
| `industry.py` | `detect_industry(pages, scores) -> IndustrySignal`. Pure |
| `cli.py` | `fra-ingest locate <pdf> [--json]` writes `var/artifacts/<sha256>/locate.json` |

Supporting work outside the package:

| Path | Responsibility |
|------|----------------|
| `configs/ingest.toml` | The defaults above |
| `scripts/label_statement_pages.py` | One-off: OCR every page of the scanned golden documents and write an HTML contact sheet of every page. The owner labels blind, then reconciles against suggestions (see Labelling); confirmed ranges go into `eval/golden/manifest.yaml` |
| `eval/harness/locate.py` | Scoring over the golden set and corpus pools (see Scoring) |

## Data model

```python
class PageText(BaseModel):          # cached
    page_no: int                    # 1-based
    mode: PageMode                  # text | image, from fra_core
    source: TextSource | None       # text | ocr; None when an image page was not read
    header_text: str                # top header_fraction of the page
    body_text: str                  # the rest
    char_count: int                 # non-whitespace characters from the text layer
    width_pt: float
    height_pt: float
    arabic_chars: int
    latin_chars: int
    visual_arabic: bool             # Arabic words stored reversed in the text layer
    ocr_seconds: float
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
    kind: Literal["corporate", "bank", "insurer", "other_financial", "unknown"]
    subkind: Literal["investment_holding", "brokerage", "exchange_operator",
                     "consumer_finance", "asset_manager", "other"] | None
                                    # set only when kind is other_financial
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
    timings: dict[str, float]       # seconds: read, text, ocr, score
```

## Data flow

```
PDF ─► read_pages ─► PageText[] ─► score_page ─► PageScore[] ─► locate ─► LocateResult
       │ pypdfium2 per page      (cached)        (pure)        │ detect_industry  (locate.json)
       │ char_count < 50, or garbled: render at 100 dpi, OcrEngine
```

1. **Read.** The file's sha256 identifies the document. For each page, pypdfium2 extracts
   the header region (top 35%) and the body separately, with bidi controls removed
   (`fra_core.numbers.strip_bidi`). A page under `min_text_chars` is an image page: it is
   rendered at `ocr_dpi` and read Arabic first, then English (R21), and OCR lines are split into header
   and body by their bbox. Script counts give each page, and the document, its language.
   A text page whose Arabic words read backwards is marked `visual_arabic` (see below).
2. **Cache.** `PageText[]` is written to `var/artifacts/<sha256>/pages.v<N>.json`, keyed by
   document, stage and stage version (ADR 0005). OCR is the only expensive step, so only it is
   cached; scoring always reruns, which keeps rule tuning to seconds per pass over a pool.
3. **Score.** For every page and type: title matches in the header text on
   `normalize_label` forms; numeric density, counted as amounts of three or more digits
   after `fra_core.numbers.normalize_digits` (comma, Arabic and space thousands separators;
   years excluded); structural cues, which every statement page carries
   whatever its title: a period header (`2025 2024`, `31 December`, `ديسمبر`), a note column
   header (`Note`, `إيضاح`), and a currency or scale line (`SAR '000`, `بالآلاف`); negative
   cues, which subtract. Structural cues let a page be found when its title is printed in
   only one language, drawn as an image, or garbled by OCR. A page whose header carries a
   continuation cue (`continued`, `تابع`) is marked as a continuation.
4. **Locate.** Pages over the threshold are grouped into consecutive ranges of one type. A
   numeric page without a title, directly after a titled page, extends that page's range.
   Every candidate range is kept and ranked, since a filing can hold consolidated and
   standalone statements and an annual report can hold a highlights summary; recall comes
   before selectivity. Ranges are padded by `pad_pages`, clamped to the document, and the
   enabled types' ranges are merged into `convert_ranges`.
5. **Industry.** Cues are counted on the balance sheet and income statement pages when the
   locator found them, and on every page otherwise, since line items show what kind of
   business it is while a corporate annual report can mention banking in its narrative. Excluded corporate phrases are
   removed before matching. A financial company that is neither a bank nor an insurer is
   `other_financial` with a sub-kind: `investment_holding`, `brokerage`, `exchange_operator`,
   `consumer_finance`, `asset_manager` or `other`. The verdict is stored; nothing is declined in this part.

### Arabic word and letter order in text layers

Found while writing the plan (2026-09-27), measured on the non-blind Arabic corpus:

| What pypdfium2 returns | Documents | Example |
|------------------------|-----------|---------|
| Each line's words in reverse order, letters correct | 77 of 82 | `المالي المركز قائمة` for `قائمة المركز المالي` |
| Letters of every word reversed as well (visual order) | Almarai AR (1 of 61 with a text layer) | `ةمئاق` for `قائمة` |
| Lam-alef ligature split in the wrong order | common in both | `اآلخر` for `الآخر`, `المعامالت` for `المعاملات` |

Amounts are unaffected. OCR output is in logical order.

Handling in locate: any text holding Arabic is searched as extracted and with each line's
words reversed, and a phrase counts if either holds it. Visual-order pages are detected per
page (a word never starts with ta marbuta or alef maqsura and never ends with the article:
1,484 against 5 on Almarai AR, 28 against 1,621 on a normal filing) and have each word's
letters restored first. Phrase comparison folds lam-alef to alef-lam on both sides. Part 3
needs a full repair to display labels; locate only needs to match.

## Failure handling

Every failure is visible; none turns into a blank or a guess.

| Situation | Behaviour |
|-----------|-----------|
| Not a PDF, or corrupt | `IngestError("unreadable_pdf")`; the job stops with that reason |
| A PDF with no pages | `IngestError("empty_pdf")`, or `unreadable_pdf` where pdfium refuses to open it |
| Password protected | `IngestError("encrypted_pdf")` |
| No OCR engine available (Linux or Docker before week 2) | Image pages flagged `ocr_unavailable`; result flag `image_pages_not_read:<n>`. Never scored as empty pages |
| OCR fails on one page | That page is flagged `ocr_failed`; the run continues |
| An enabled type has no range | `statement_not_found:<type>`; later stages carry on and the results page shows the gap |
| No enabled type found | Empty `convert_ranges` and `no_statements_found`; the pipeline stops with that reason |
| Candidate pages above `low_selectivity_share` of the document | `low_selectivity`; the run goes ahead and the eval counts it |
| Industry verdict bank, insurer or other financial | `likely_bank`, `likely_insurer` or `likely_other_financial:<subkind>`; no decline yet |

## Testing

Tests are written before the code they cover (repository rule).

| Area | Cases | Marker |
|------|-------|--------|
| `score_page` | English and Arabic titles, Gulf and Egyptian variants. Must not score: an auditor's report quoting statement titles, a contents page, a notes heading. A continuation page. A garbled OCR title on a dense numeric page, found by numeric density and structural cues. A page with no title at all but a period header, note column and scale line | fast |
| `locate` | Grouping, continuation, padding clamped at both ends, `convert_ranges` following config, every flag in the failure table | fast |
| `detect_industry` | Bank, insurer and each `other_financial` sub-kind in both languages. Stay `corporate`: `cash and bank balances`, `prepaid insurance`, and a manufacturer holding a few listed investments | fast |
| `read_pages` | Juhayna EN standalone pp. 3 to 5 are `image` and the rest `text`; Almarai AR has no bidi controls left and is marked `visual_arabic`; a fake `OcrEngine` receives exactly the image pages; the cache is reused on a second call | `golden` |
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
operators), so each negative control gets `sector: bank | insurer | other_financial`, and an
`other_financial` one also gets `subsector: investment_holding | brokerage | exchange_operator
| consumer_finance | asset_manager | other`. In `train` that is 4 bank, 2 insurer and 13 other
financial documents. Only `bank`
and `insurer` have a target here; sub-sector verdicts are reported, and how `other_financial`
is treated is part of the week 2 decline rule, which can act on the whole group or on one
sub-sector. Development runs use `dev` and `train`. `model_test` runs at checkpoints only. The harness refuses `blind` (R17).

### Done when

| Measure | Target |
|---------|--------|
| Recall on the enabled types, golden set | 100% |
| Median candidate share, golden set | 15% or less |
| Corporate documents with a balance and an income range, `train` | 95% or more, every miss explained |
| `sector: bank` or `insurer` documents given that verdict, `train` (6 documents) | all 6 |
| Corporate documents marked bank or insurer, `train` | at most 2 |
| Time, digital annual report | under 10 s |
| Time, 64-page scanned filing, OCR model loaded | under 30 s (Arabic); about 30 s for English, which needs two reads per page (R21) |

## Labelling the scanned golden documents

The 9 documents with `statement_pages: null` are labelled before the locator is scored
against them, because `statement_pages` is an answer key and never an input (08, principle 1).

1. `scripts/label_statement_pages.py` OCRs every page with `VisionOcr` and writes
   `var/labels/<doc id>.html`: a thumbnail of every page with its page number and no
   highlights.
2. **Blind pass.** The owner marks the pages of each statement type on the sheet, which saves
   them to `var/labels/<doc id>.json`.
3. **Reconcile.** The script compares the marks with pages holding a statement title or
   structural cues and lists every disagreement: pages it would suggest that the owner did not
   mark, and marked pages it would not suggest. The owner settles each one, and the final
   ranges are written into `manifest.yaml`. The disagreement list is kept in the labelling
   notes as a first reading of where the cues are weak.
4. Step 1 reads pages through `read_pages` with the configured dpi and languages, so it fills
   the page cache and the locator's first run on these documents costs no OCR.
5. A comprehensive income statement on its own page gets its own `comprehensive_income` key,
   as in `juhayna-2025-en-standalone`.

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R21 | Vision's result depends on the order of the language list | Measured 2026-09-27 on 24 pages of Juhayna and Edita scans: Vision reads only in the first language given. English first scores 0.00 to 0.17 on Arabic pages; Arabic first scores 0.13 to 0.99 on English pages; each language first scores 1.000 on its own script. A 36 dpi guessing pass recognises almost nothing, so it is not used. Instead each image page is read in Arabic first and read again in English when the Arabic read holds fewer than 10 Arabic letters (English pages gave 0 to 5, Arabic pages 798 to 1,338). Owner's decision. English scans cost two reads per page, about 0.45 s |
| R22 | Statement titles appear in only one language on bilingual pages, or as images | Structural cues (period header, note column, currency and scale line) score the page without its title, alongside numeric density and continuation. The eval shows every miss with its page scores |
| R23 | Label suggestions bias the owner toward the same pages the locator would find | Blind labelling first, suggestions shown only afterwards as a list of disagreements to settle |
| R24 | Text layers return Arabic lines with words reversed (77 of 82), letters reversed (Almarai AR) or ligatures swapped | Two reading orders, per-page letter restoration and lam-alef folding when matching (see above). Part 3 repairs labels properly |

## Results

Measured 2026-09-28 on the `ingest-locate` branch, after labelling the 9 scanned golden
documents (blind, then reconciled: 34 disagreements, no label changed; 32 were extra
suggestions on neighbouring statements or notes tables, 2 were misses on optional types),
and after the branch review's fixes. Times are wall clock from `make eval-locate` with
`--fresh` (no page cache, OCR model loaded).

| Measure | Target | Result | |
|---------|--------|--------|---|
| Recall on the enabled types, golden set (12 documents) | 100% | 100% | pass |
| Median candidate share, golden set | 15% or less | 10.7% (range 8.2% to 16.9%) | pass |
| Corporate documents with a balance and an income range, `train` | 95% or more | 100% (109 of 109) | pass |
| `sector: bank` or `insurer` given that verdict, `train` | all 6 | 6 of 6 | pass |
| Corporate documents marked bank or insurer, `train` | at most 2 | 0 | pass |
| Time, digital annual report (275 pages) | under 10 s | 2.2 s English, 3.6 s Arabic | pass |
| Time, 64-page scanned filing, OCR model loaded | under 30 s | Arabic 12.5 to 16.8 s for 59 to 64 pages; English 24.1 s for 57 pages, 33.7 s for 66, 41.6 s for 85 (about 31 to 33 s per 64 pages) | pass for Arabic; English about 10% over, because each page is read twice (R21) |

`dev`: 3 of 3 covered. Optional types, reported only: cash flow and changes in equity are each
found on 11 of 12 golden documents. Juhayna 2024 AR loses its cash flow title to OCR
(`قالمة التنفقات`), and the landscape equity statement in Edita IFRS has no title OCR can read.

The golden set is not held out: tuning looked at golden pages, as did the labelling
reconcile. The held-out score comes from the `model_test` checkpoint, which the owner has
deferred (running it freezes `model_test` and `blind`, corpus README rule 5).

Other financial sub-kinds carry no target: 2 of 4 exchange operators are recognised;
investment holdings, asset managers and consumer finance companies read as corporate because
their statements use ordinary line items. The week 2 decline rule needs a structural test for
them (an income statement without revenue or cost of sales).

### What tuning and review changed

Every change was measured on `train` or on labelled golden pages and pinned by a test first.

| Change | Found on |
|--------|----------|
| Scans read at 100 dpi, not 72 | Juhayna EN standalone p5: 6 lines at 72 dpi, 124 at 100 |
| Arabic first, English retry (R21) | 24 golden scan pages |
| Arabic read in both word orders, lam-alef folded (R24) | 77 of 82 Arabic corpus PDFs |
| Text layers made of noise are read by OCR | Al Kathiri (Greek-mapped font), Naba (modifier-glyph digits) |
| A currency line counts as structure | Al Dawaa 2023 |
| Comprehensive-income title plus revenue lines is also income | Herfy 2022 |
| Arabic title stems without قائمة, only on short lines without figures | OCR reading قائمة as فائمة or خاامة; the OCI reserve line posing as a title (Jazan 2024, Al Dawaa 2025) |
| Notes headings matched by stem (`notes to the`, `إيضاحات حول`) | Juhayna EN standalone |
| No continuation through pages with negative cues | Juhayna AR consolidated: 52% and 56% of pages down to 16% and 17% |
| Industry: Gulf bank and takaful wording; whole-document fallback needs 3 distinct cues | ABC, Orient Takaful; Saudi Energy, United Electronics, Egypt Kuwait wrongly flagged before |
| Failed OCR reads flagged (`ocr_failed_pages`) and never reused from the cache | Branch review |
| Header and body mapped through the page rotation | Branch review: Edita p36 lost 29% of its text |
| A mostly unread document gets industry `unknown` | Branch review |

The scoring weights are unchanged from the plan: title 3.0, cue 1.5, numbers up to 2.0 (full
at 30 amounts), each structural cue 1.0, each negative -4.0, candidate at 4.5, continuation at
15 amounts. Weight changes are made with the owner.

## Later scope: notes

Owner's decision, 2026-09-28: figures come from the primary statement pages only. Notes are
70 to 90% of a filing, have no common identity to check against, and repeat statement figures
in other groupings, so converting them would break the time budget and weaken the "right, or
visibly unsure" rule. Part 3 records each line item's `note_ref`. Following a reference to its
note (a lookup in the cached page text, then converting only that note's pages) is future scope,
added only for a metric that needs a figure not on the face of the statements. Depreciation and
amortisation for EBITDA is the likely first case, and enabling the cash flow statement covers it
more cheaply than notes.
