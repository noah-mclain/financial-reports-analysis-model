# 11. Ingest, Part 3a: Structure

Status: v1, 2026-09-29. Implements the first half of the structure stage for week 1 of
[08-revised-plan.md](08-revised-plan.md), following tasks 1.3 to 1.8 of
[04-execution-phases.md](04-execution-phases.md). Part 2 is [10-ingest-convert.md](10-ingest-convert.md).
Part 3b (review report, sum-based hierarchy, extraction eval against expected files) gets its
own document.

## What structure does

| # | Part | Output |
|---|------|--------|
| 1 | Locate | `locate.json` |
| 2 | Convert | `convert.json`, `docling/p<a>-<b>.json`, `pages/<n>.png` |
| 3a | **Structure**: grid, classification, periods, scale and currency, continuation, light hierarchy, checks | `statements.raw.json`, `table_checks.json` |
| 3b | Review report, sum-based hierarchy, extraction eval | report HTML, eval |

Structure turns docling's tables into `fra_core.schemas.Statement` objects: one per primary
statement, periods bound from header text, every value with its page, box, table and cell.
Values are kept as printed (sign applied, before scale), and scale and currency are recorded on
the statement.

## Measured before designing

Read from the golden docling output of Part 2 (2026-09-29):

| Document | What docling gives | Consequence |
|----------|--------------------|-------------|
| Almarai EN, p156 | 20 x 4 grid; header `31 December 2025 X '000`; section rows (`ASSETS`) marked | Header text binds periods and scale; `X` is the riyal glyph, so currency is not in the header |
| Almarai EN, p159 | Header split over two rows: `For the year ended` / `31 December 2025 X '000` | Header rows are joined per column before parsing |
| Almarai AR, p156 | 17 x 4 grid, label column on the right. Every number has its digits reversed (`٢٤٣,٠٥٧,٢٢` for 22,750,342), note references too (`٠١` for 10), header years too, with Arabic-Indic and Extended Arabic-Indic digits mixed (`۱۳` beside `٥٢٠٢`). Letters are in reading order but separated by single spaces, so word boundaries are lost (`إ ج م ا ل ي ا ل م و ج و د ا ت`, total assets). The first row's label cell holds both section headings and the first line item, which is why there are three fewer rows than in English | Digit repair only on pages Part 1 marked `visual_arabic`; spaced letters joined wherever they occur; English and Arabic rows matched by values, never by row index |
| Juhayna EN and AR, p5 (scanned) | 13 and 17 numeric cells where about 50 are printed; Vision at 100 dpi in Part 1 also read almost none | The figures are small and blurred with space separators. docling already OCRs at 3x (216 dpi, `OcrMacOptions.scale`). Task 1 measures higher scales and cell matching off; until then these statements come out with visible gaps |
| Edita AR EAS, p5 (scanned) | 76 numeric cells | Scans of normal print are read |
| Juhayna EN and AR, p5, OCR scale test (task 1) | Numeric cells EN / AR: scale 3 13 / 17 (27 s, 2.20 GB / 34 s, 2.27 GB); scale 4 11 / 6 (22 s, 2.27 GB / 35 s, 2.30 GB); scale 5 12 / 7 (22 s, 2.36 GB / 36 s, 2.42 GB); cell matching off 0 / 0 (22 s, 2.22 GB / 27 s, 2.27 GB) | Higher scales do not help (fewer cells, not more) and cell matching off empties the cells, so `ocr_scale` stays 3.0 and `do_cell_matching` stays on. The gaps are not a resolution problem; the statements keep them visible |

The core parsers already handle the text: `parse_period` reads English and Arabic dates, the
"Restated" marker and duration against instant; `detect_scale` reads `'000`, `ألف` and `آلاف`, also inside a joined run such as `مبآلاف`;
`parse_number` reads both digit systems, space separators, parentheses and dashes. A reversed
Arabic number fails to parse (`unparsed`) until its digits are restored. After the repair below,
the Almarai AR header `۱۳ د ي س م ب ر ٥٢٠٢ م ب آ لا ف X` reads `31 ديسمبر 2025 مبآلاف X`,
which parses to 2025-12-31 with scale 1000.

## Scope

| In | Out |
|----|-----|
| Balance, income and comprehensive income (the enabled types) | Cash flow and equity (converted only when enabled; structured the same way then) |
| Grids with boxes in PDF points, top-left origin | The HTML review report (3b) |
| Classification of tables into statement types, notes rejected | Label normalization to canonical ids (week 2) |
| Header rows bound to periods; note column | Sum-based hierarchy inference (3b) |
| Scale, currency, entity, consolidated | Extraction accuracy against hand-checked expected files (3b) |
| Visual-order repair on `visual_arabic` pages | OCR recovery for unreadable scans beyond the task 1 setting (week 2 bake-off) |
| Statements continued across pages | Following note references |
| Light hierarchy: depth, subtotal cues, parents | |
| Subtotal checks and the balance sheet identity | |
| Golden eval, including bilingual pairs | |

## Components

`structure` runs in the ingest worker: it is not heavy, needs no child process and imports
neither docling nor docling-core. It reads Part 2's docling JSON through small models of the
fields it uses; a golden contract test fails if docling's JSON stops matching them.

| File | Responsibility |
|------|----------------|
| `fra_ingest/docling_json.py` | Pydantic models for the docling JSON fields structure reads (tables, cells, boxes, provenance, texts) and a loader |
| `fra_ingest/table_grid.py` | `build_grid(table, page_height) -> Grid` |
| `fra_ingest/visual_order.py` | Digit and bracket restoration on `visual_arabic` pages; joining of spaced letters on any page |
| `fra_ingest/classify.py` | `classify(grid, context) -> Classification` |
| `fra_ingest/header.py` | `parse_header(grid, statement_type, context) -> HeaderLayout` |
| `fra_ingest/metadata.py` | `detect_metadata(...) -> Metadata`: scale, currency, entity, consolidated, conflicts |
| `fra_ingest/hierarchy.py` | `infer_hierarchy(rows) -> list[RowNode]`: depth, parent, subtotal cue |
| `fra_ingest/continuation.py` | `merge_continuations(parts) -> list[PartialStatement]` |
| `fra_ingest/table_checks.py` | Subtotal checks and the balance sheet identity |
| `fra_ingest/structure.py` | `structure_pdf(...)`: orchestration, cache, artifacts |
| `fra_ingest/cli.py` | `fra-ingest structure <pdf>` |
| `fra_core/schemas/check.py` | `CheckResult`, shared with the analytics identities of 04, 1B.1 |
| `eval/harness/structure.py` | Golden eval and bilingual pair check |

## Data model

```python
class GridCell(BaseModel):          # frozen
    text: str
    row: int                        # start_row_offset_idx
    col: int                        # start_col_offset_idx
    row_span: int
    col_span: int
    bbox: BBox                      # PDF points, top-left origin
    is_column_header: bool
    is_row_header: bool
    is_row_section: bool
    page_no: int
    flags: tuple[str, ...]          # e.g. "digits_reversed"

class Grid(BaseModel):
    table_ref: str                  # docling self_ref, "#/tables/3"
    docling_path: str               # which range file it came from
    page_no: int
    num_rows: int
    num_cols: int
    cells: list[GridCell]

class Classification(BaseModel):
    type: StatementType | None
    confidence: float               # 0 to 1
    evidence: list[str]
    industry_flags: list[str]

class HeaderLayout(BaseModel):
    header_rows: list[int]
    label_col: int
    note_col: int | None
    value_cols: dict[int, Period]
    evidence: list[str]

class Metadata(BaseModel):
    scale: int | None
    currency: str | None
    entity_name: str | None
    consolidated: bool | None
    signals: list[str]
    conflict: bool

class RowNode(BaseModel):
    row: int
    depth: int
    parent_row: int | None
    is_subtotal: bool
    is_section: bool

class CheckResult(BaseModel):       # fra_core.schemas.check
    id: str
    statement_id: str
    kind: Literal["subtotal", "balance_identity"]
    period_key: str
    status: Literal["pass", "fail", "skipped"]
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None
    tolerance: Decimal | None
    line_item_ids: list[str]
    detail: str

class TableDecision(BaseModel):
    table_ref: str
    docling_path: str
    page_no: int
    type: StatementType | None      # None: not a statement
    confidence: float
    statement_id: str | None        # the statement it became part of
    evidence: list[str]

class StructureResult(BaseModel):   # statements.raw.json
    version: str                    # STRUCTURE_VERSION, "1"
    sha256: str
    convert_version: str
    settings_hash: str
    statements: list[Statement]
    tables: list[TableDecision]     # every grid: type or rejection, confidence, evidence
    flags: list[str]
    timings: dict[str, float]
```

`table_checks.json` holds `list[CheckResult]`.

## Data flow

`fra-ingest structure <pdf>`:

1. **Inputs.** Load `convert.json` (run convert first if it is missing or stale, through
   `convert_in_child`), each range's docling JSON, and Part 1's page cache for page modes,
   `visual_arabic`, OCR language and page text. A cached `statements.raw.json` with the same
   settings hash is returned (ADR 0005).
2. **Grids.** One `Grid` per docling table. Boxes are converted to a top-left origin with the
   page height from the docling JSON.
3. **Visual order.**
   - Digits, only on `visual_arabic` pages: every token made of digits and separators is
     reversed back, and a bracket pair around it that came out mirrored (`)123(`) is swapped.
     Tokens are whitespace-separated, so dates and years in headers are repaired the same way.
     The cell carries `digits_reversed`. Nothing is reversed on other pages.
   - Spaced letters, on any page: in a cell where most tokens are single Arabic letters, each
     run of single-letter tokens is joined into one word; tokens of two or more characters
     (numbers, `X`, the `لا` ligature is treated as one letter) stay separate. Word boundaries
     inside a run cannot be recovered, so the joined label is kept as is and flagged
     `letters_spaced`. Comparisons with taxonomy aliases and cue words on such text ignore
     spaces on both sides. Readable Arabic labels for these documents are left to week 2's
     label work.
4. **Classification.** Evidence for each grid:
   - locate's title and cue types for the grid's page (`locate.json`, `PageScore`);
   - row labels found in the taxonomy's aliases for each type (`Taxonomy.lookup`, space-free
     comparison for `letters_spaced` labels);
   - comprehensive-income cue words (`other comprehensive income`, `will not be reclassified`
     and the like) count as label evidence for that type, because the taxonomy has no
     comprehensive-income items until week 2;
   - the heading text above the table on its page.

   Negative evidence: a note heading (`Note`, `إيضاح` followed by a number) above the table,
   or no period header. A grid is a statement when its best
   type reaches `min_confidence` (0.5, `[structure]` in `configs/ingest.toml`); every decision
   and its evidence goes into `StructureResult.tables`.
5. **Header.** The header rows are those flagged `column_header` plus any leading rows that
   hold no amounts (a cell that parses as a date or a year is header text, not an amount). They are joined per column top to bottom and parsed with `parse_period`, with
   `default_kind` instant for balance and duration for income and comprehensive income. A
   column whose header is only a year takes the day and month from the statement's own date
   line (caption or header text). The note column is the column of small integers or a
   `Notes` / `إيضاح` header; the label column is the one with the most non-numeric text,
   wherever it sits, so mirrored Arabic tables need no special case. Columns are bound to
   periods by their header text, never by position.
6. **Metadata.** Scale from the header text, then the caption, then the page text; per-share
   rows are exempt. Currency the same way; when none of those name one (Almarai's glyph), the
   country of incorporation decides: the earliest page with a phrase such as "a Saudi Joint
   Stock Company", "S.A.E." or "شركة مساهمة سعودية" (Part 1's page text with its reading
   variants, and the docling texts) gives that country's currency, flagged
   `currency_from_domicile`. Only when no page names one is the currency named most often in
   the document used, flagged `currency_inferred`. Disagreeing signals set `conflict` and a
   flag.
7. **Line items.** One per data row: label text, note reference, and a `Cell` per bound period
   with `reported` from `parse_number`, `raw_text`, parser flags, and provenance (page, box,
   table ref, row, column, source text or OCR from the page mode).
8. **Continuation.** Parts of one type on consecutive pages merge when their period keys match
   and their value columns line up (centre x within 5% of page width); repeated header rows
   are dropped.
9. **Hierarchy (light).** Depth from clustering label left edges; a row is a section when it
   has a label and no values, a subtotal when its label has a total or net cue in either
   language or it follows a rule of rows ending at a section. Parent is the nearest section
   row above with smaller depth.
10. **Checks.**
    - Subtotal: a subtotal row that directly closes a run of two or more plain rows (since
      the section start or the previous subtotal) is checked against their sum, or their sum
      plus the previous subtotal (a running total), per period, with tolerance n x 0.5
      reported units, n the number of rows summed (D6). A heading row (no values) starts a
      new run but keeps the previous subtotal as the running-total candidate. A plain row
      that equals, in every period and within the same tolerance, the sum of the last two or
      more rows of the run (the shortest such suffix) is an implicit subtotal, such as
      Almarai's "Equity Attributable to Equity Holders of the Company" or Almarai AR's "other
      comprehensive income for the year": it passes with `implicit_subtotal` and replaces
      those rows in the run as one addend, so the explicit total after it stays checkable. A subtotal whose run began right
      after a heading and that misses both candidates is `skipped` with
      `subtotal_scope_uncertain`, since the heading may have cut rows it covers. Other
      subtotals, such as total assets over two section subtotals or Almarai AR's rows whose
      section headings are merged, are `skipped` with `subtotal_scope_unknown`; 3b's
      sum-based hierarchy checks them.
    - Balance sheet identity: total assets against the printed total of liabilities and
      equity when the statement has one, otherwise against total liabilities plus total
      equity, per period. The totals are found by taxonomy lookup of their labels
      (`total_assets`, `total_liabilities_and_equity`, `total_liabilities`, `total_equity`);
      when they are not found the check is `skipped` with the reason.
11. **Write** `statements.raw.json` and `table_checks.json` atomically.

## Failure handling

"Right, or visibly unsure."

| Situation | Behaviour |
|-----------|-----------|
| A value cell is empty where the row has values in other periods | Cell with `reported` None and `numbers_missing` |
| A value does not parse | `reported` None, `raw_text` kept, parser flags on the cell |
| A header column does not bind to a period | That column dropped, `period_unbound:<col>` on the statement; with no bound column, no statement and `statement_not_extracted:<type>` |
| An enabled type has no classified table | `statement_not_extracted:<type>` on the result |
| Two tables claim the same type on the same page and cannot be merged | Both kept, `ambiguous_statement:<type>`; the higher-confidence one first |
| Scale or currency signals disagree | `scale_conflict` or `currency_conflict`; the header value wins |
| Currency only inferred from document text | `currency_inferred` |
| A subtotal or the identity fails | `CheckResult` fail with the difference; the statement gets `subtotal_failed` or `identity_failed` |
| Identity totals not found | Check `skipped`, `identity_totals_not_found` |
| `convert.json` missing or a range failed | Structure runs on what converted; failed ranges flagged `range_not_converted:<a-b>` |

## Testing

Tests are written before the code they cover.

| Area | Cases | Marker |
|------|-------|--------|
| docling JSON contract | The models load every table and text of Almarai EN pages 155-164 and Juhayna AR pages 4-9 | `golden` |
| `build_grid` | Boxes converted from bottom-left to top-left; spans; header flags | fast |
| `visual_order` | Digits reversed back in numbers, note references and header years with mixed digit sets; brackets swapped; only on `visual_arabic` pages; `-` untouched; spaced letters joined into runs with numbers and `X` kept apart | fast |
| `parse_header` | Two-row header; mirrored Arabic columns; note column by header and by small integers; year-only header; restated column | fast |
| `classify` | Balance and income from labels in both languages; a notes table with statement labels rejected | fast |
| `detect_metadata` | `'000` and `ألف`; glyph currency inferred from document text; conflicting signals flagged | fast |
| `infer_hierarchy` | Depth from indentation; section rows; total and net cues in both languages | fast |
| `merge_continuations` | Two parts merged; mismatched periods or columns kept apart; repeated header dropped | fast |
| Checks | Subtotal pass and fail at the D6 tolerance; a subtotal over subtotals skipped; identity pass, fail, skipped; identity through a printed liabilities-and-equity total and through the two totals | fast |
| End to end | Almarai EN balance sheet (156-158) and income statement (159): figures, periods, scale, currency; Almarai AR the same after repair | `golden` |

## Scoring

`make eval-structure` runs `eval/harness/structure.py` over the golden set. For each document:
statements per enabled type, scale and currency against `manifest.yaml`, identity status, flags.
The bilingual pair check compares each statement of a document with the same type in its other
language edition (Almarai 2025, Juhayna 2025 and 2024 consolidated, Edita 2025 EAS): rows are
matched by their values in every period; a numeric row with no counterpart is a miss.

### Done when

| Measure | Target |
|---------|--------|
| Almarai EN against AR, balance, income and comprehensive income | every numeric row has a counterpart with the same value in every period |
| Scale and currency, golden set | 12 of 12 correct, or flagged |
| Balance sheet identity, golden set | holds, or flagged with the correct reason |
| Resolution test (task 1) | recorded: numeric cells found on Juhayna EN and AR p5 at docling's OCR scale 3 (its default, 216 dpi), 4 and 5, and at scale 3 with cell matching off. If a setting brings the count near the printed figures, it becomes a `[convert]` setting (a Part 2 change with its own test) |

Other bilingual pairs are reported, not gated, until the resolution question is settled.

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R29 | Digit reversal applied to a page that is not visual, or missed on one that is | Reversal only on `visual_arabic` pages from Part 1; the bilingual pair check catches a wrong or missing repair on Almarai |
| R34 | Arabic labels with lost word boundaries weaken classification and the identity's total lookup | Space-free comparison; locate's title types as classification evidence; a missing total skips the identity with its reason instead of failing it |
| R30 | Scanned figures too small for OCR at the converted resolution (Juhayna), or read but lost when docling matches words to cells | Task 1 measures OCR scale 4 and 5 and cell matching off; the gaps are flagged `numbers_missing` and the identity fails visibly |
| R31 | docling's JSON changes shape between versions | Our own models of the fields used, and a golden contract test; docling is pinned |
| R32 | A notes table taken for a primary statement | Note headings, taxonomy hits and period headers as evidence, every decision recorded in `tables`, and the balance identity as a backstop |
| R33 | Glyph currencies with no currency word near the statement | Country of incorporation from the earliest page naming it, flagged `currency_from_domicile`; else document-level inference, flagged `currency_inferred`; the manifest scores it |

## Results

Measured 2026-10-01 on the `ingest-structure` branch (`worktree/phase2-extraction`), `make eval-structure`. Lines are line items of the first statement of each type; identity is the balance sheet identity.

| Document | Balance | Income | Comprehensive income | Scale and currency | Identity |
|----------|---------|--------|----------------------|--------------------|----------|
| almarai-2025-en | 46 lines | 21 lines | 13 lines | ok (1000 SAR) | ok |
| almarai-2025-ar | 40 lines | 21 lines | 11 lines | ok (1000 SAR) | ok |
| juhayna-2025-ar-standalone | not extracted | 14 lines | not extracted | currency ok, scale flagged | no balance sheet |
| juhayna-2025-ar-consolidated | not extracted | 27 lines | not extracted | currency ok, scale flagged | no balance sheet |
| juhayna-2025-en-consolidated | 23 lines | 21 lines | 8 lines | currency ok, scale flagged | skipped (totals not found, 3 cells with numbers missing) |
| juhayna-2024-ar-consolidated | 39 lines | not extracted | not extracted | currency ok (from domicile), scale flagged | skipped (totals not found) |
| juhayna-2024-en-consolidated | 43 lines | 20 lines | 8 lines | currency ok, scale flagged | skipped (totals not found, 2 cells with numbers missing) |
| edita-2025-ar-consolidated | 39 lines | 22 lines | 7 lines | currency ok, scale flagged | skipped (totals not found) |
| edita-2025-en-consolidated-eas | 45 lines | 21 lines | 11 lines | currency ok, scale flagged | ok |
| edita-2025-en-consolidated-ifrs | 42 lines | 22 lines | 11 lines | currency ok, scale flagged | ok |
| edita-2024-ar-consolidated | 37 lines | 19 lines | 7 lines | currency ok, scale flagged | failed (5 cells with numbers missing) |
| juhayna-2025-en-standalone | not extracted | 14 lines | 4 lines | currency ok, scale flagged | no balance sheet |

The manifest records `scale: unconfirmed` for the ten Juhayna and Edita documents, so the harness scores only their currency; a statement that could not read a scale carries `scale_missing` and stays at 1.

Unmatched numeric rows in each language edition of a pair (first edition, second edition):

| Pair | Balance | Income | Comprehensive income |
|------|---------|--------|----------------------|
| Almarai 2025 EN / AR (gated) | 0 / 0 | 0 / 0 | 0 / 0 |
| Juhayna 2025 consolidated EN / AR | no AR balance sheet | 9 / 13 | no AR statement |
| Juhayna 2024 consolidated EN / AR | 38 / 23 | no AR statement | no AR statement |
| Edita 2025 EAS EN / AR | 37 / 36 | 4 / 3 | 3 / 3 |

| Measure | Target | Result |
|---------|--------|--------|
| Almarai EN against AR | every numeric row matched | met: 0 unmatched rows on balance, income and comprehensive income in both editions |
| Scale and currency, golden set | 12 of 12 correct or flagged | met for the 29 statements extracted: currency correct in every one, scale 1000 correct on Almarai, scale flagged `scale_missing` on the ten documents whose manifest scale is unconfirmed |
| Balance sheet identity, golden set | holds, or flagged with the correct reason | met where a balance sheet was extracted: holds on Almarai, Edita EAS and Edita IFRS; skipped with `identity_totals_not_found` on four Juhayna and Edita statements; failed on edita-2024-ar-consolidated with `numbers_missing` cells and `subtotal_failed`. Not met in coverage: no balance sheet was extracted for three Juhayna documents |
| Resolution test (task 1) | recorded | OCR scale 4 and 5 and cell matching off found no more numeric cells on Juhayna p5 than docling's default scale 3 (13 and 17 cells at 3; fewer at 4 and 5; none with matching off), so `ocr_scale` stays 3.0 and `do_cell_matching` stays on |

Not gated and not solved: the Juhayna and Edita pairs differ because the scanned statements lose cells (`numbers_missing`), periods stay unbound on several Arabic statements (`period_unbound`), and three Juhayna documents lack a statement of a type that the other edition has.
