# 12. Ingest, Part 3b: Review

Status: v1, 2026-10-01. Completes the structure stage of
[11-ingest-structure.md](11-ingest-structure.md): tasks 1.6, 1.8 and the first line of 1.9 in
[04-execution-phases.md](04-execution-phases.md), towards Gate G1 and Gate A of
[08-revised-plan.md](08-revised-plan.md). The table-model comparison of 1.9 and the OCR engine
bake-off are week 2 work and are not in this part.

## What review does

| # | Part | Output |
|---|------|--------|
| 3a | Structure | `statements.raw.json`, `table_checks.json` |
| 3b | **Review**: sum-based hierarchy, row-alignment repair, figure checks, the held-for-review decision, the review report, extraction eval | the same two files (structure version 4), `review.html`, `eval/golden/expected/`, `var/eval/extraction-golden.json` |

Part 3a made failures visible. Part 3b decides what they mean: which rows a total covers, which
cell a failed sum points at, whether labels and values still sit on the same line, and whether a
statement can be passed on or must be held for a person to look at. The review report is where
that person looks.

## Measured before designing

Read from the golden output of Part 3a (structure version 3, 2026-10-01), 33 statements in 12
documents.

| Question | Measured | Consequence |
|----------|----------|-------------|
| Which subtotals does 3a leave unchecked, and would sums settle them? | A prototype that keeps a list of open rows and lets each total close the shortest suffix that sums to it. Almarai EN and AR balance sheets: 8 totals pass and 1 implicit, none skipped (3a: 5 pass, 3 `subtotal_scope_unknown`). Edita IFRS balance sheet: all 8 totals pass, including total assets, total liabilities and the closing total; total equity passes against "Equity attributable to the Owners" plus non-controlling interest (3a: fail by 5,510,862,436) | The open list replaces 3a's run. A heading no longer ends the search, so a total over section subtotals needs no special case |
| How many figures does a passing sum vouch for? | Numeric cells that sit in a passing sum, 3a against the prototype: Almarai EN and AR balance 74 to 80 of 80; Almarai EN comprehensive income 6 to 20 of 20; Edita IFRS balance 40 to 53 of 68; Edita EAS comprehensive income 6 to 14 of 14. Income statements do not move (Almarai 34 of 38) | Coverage is reported for every statement. The cells no check vouches for are drawn apart in the report |
| Is a blank cell a lost number? | Edita IFRS: the 2024 treasury shares cell and the 2024 fair-value assets cell are `numbers_missing`. In both, the section total equals the sum of the other rows exactly, so the cells are blank on the page | A missing value counts as zero in a sum when the sum then holds and another period holds with every value present. The cell is `blank_confirmed`, not a gap |
| Can geometry find the Edita 2024 AR row shift? | For every row, whether each value cell's vertical centre lies inside its label cell's top and bottom, with a quarter of the median value height as slack. 3 rows off (`p5-t0-r22`, `r23`, `r24`) and 1 row with values and no label (`r21`), all on Edita 2024 AR p5. 0 on the other 32 statements | Detection needs no arithmetic and no label knowledge. Each displaced value group lies inside exactly one other row's label line, so it can be moved there |
| What do the label cells look like around the shift? | Label cells more than 1.7 value heights tall hold two or three printed lines: a heading joined to the first item under it, or two items. 9 on Edita 2024 AR p5, and between 0 and 6 on other scanned statements. Their values sit on the last line and stay inside the cell | A tall label is not a shift. It is flagged `label_merged`. Only a label line that receives two value groups needs a second row |
| What does the page print where the identity fails by 5? | Edita 2024 AR p5, 2023: total assets and the closing total both print 7,743,342,651. Non-current plus current assets as extracted sum to 7,743,342,651. Total assets was read as 7,743,342,656 | The sum names the cell: the two section totals pass their own sums, so total assets is the only cell where one digit explains the difference |
| Are other failed sums one digit off? | Edita 2024 AR, 2024, checked against the page image: non-current assets off by 800,000 (the page prints 202,114,513 for right-of-use assets, read as 202,914,513), current assets off by 5,000,000 (136,103,684 for related parties, read as 131,103,684). Edita EAS, 2024: non-current liabilities off by 20,000 | A difference of one digit times a power of ten is reported as such, with the cells that could carry it |
| Does a row's size across periods catch misreads no sum covers? | Largest ratio between the periods of one row on Almarai (digital, correct): 326. On Edita 2024 AR p5: 51,264 (share capital read as 2,731) and 173,790 (a lease liability read as 1,327.5608) | A row whose periods differ by 1,000 times or more is flagged |
| Does net profit tie between statements? | The first valued row of comprehensive income equals a row of the income statement in every period on 7 of 7 documents where both statements are read cleanly. On Juhayna 2025 EN consolidated and standalone it does not: the comprehensive income cells hold two merged columns | A tie check between the two statements, by value, with no label knowledge |
| Where do page images and sizes come from? | `pages/<n>.png` at `images_scale` 2.0 (144 dpi, 1192 x 1685 px for a 595.8 x 842.4 pt page); page sizes in points are in the docling JSON | Boxes are drawn as percentages of the page size, so the report does not depend on the image scale |

## Scope

| In | Out |
|----|-----|
| Sum-based subtotal scope, parents from sums | Label normalization to canonical ids (week 2) |
| Row-alignment detection and repair by geometry | Splitting two figures OCR merged into one cell (week 2 bake-off) |
| Single-digit diagnosis, period outliers, the net profit tie | Changing a read figure to the one arithmetic suggests (open decision 1) |
| `passed` or `needs_review` per statement, with reasons | V1's independent re-read of every cell's region |
| `fra-ingest review-report <sha256>` | The corrections interface (04, 3.7) |
| `eval/harness/extraction.py` and draft expected files | Table-model comparison, VLM re-read (04, 1.9) |
| The golden eval's identity rule updated for held statements | Cash flow and equity statements and their ties (V5) |

## Components

| File | Responsibility |
|------|----------------|
| `fra_ingest/sum_hierarchy.py` | `infer_sums(statement) -> list[SumGroup]`: which rows each total covers and how each period came out |
| `fra_ingest/table_checks.py` | Subtotal results built from the sum groups; the identity unchanged; `run_checks` adds the figure checks |
| `fra_ingest/row_alignment.py` | `realign_rows(grid, layout) -> Realignment`: detection and repair, before line items are built |
| `fra_ingest/figure_checks.py` | Single-digit diagnosis, period outliers, the net profit tie |
| `fra_ingest/review.py` | `review_statement(statement, checks) -> StatementReview` |
| `fra_ingest/review_report.py` | `render_report(...) -> str` and `write_review_report(sha256, config) -> Path` |
| `fra_ingest/cli.py` | `fra-ingest review-report <sha256>` |
| `fra_core/schemas/check.py` | `kind` gains `net_profit_tie` |
| `eval/harness/expected.py` | Draft expected files from the extraction; never overwrites a checked file |
| `eval/harness/extraction.py` | Scores the extraction against `eval/golden/expected/` |
| `eval/harness/structure.py` | An identity failure is accepted only on a statement held for a reason that names it |

Nothing here imports docling, and nothing needs a child process.

## Data model

```python
class SumGroup(BaseModel):          # frozen
    total_id: str
    addend_ids: list[str]
    basis: Literal["sums", "run"]   # scope found by the sums, or the run above the total
    implicit: bool                  # the total carries no cue; the sums alone found it
    outcomes: dict[str, SumOutcome] # by period key

class SumOutcome(BaseModel):        # frozen
    status: Literal["pass", "fail", "skipped"]
    expected: Decimal | None
    actual: Decimal | None
    blank_ids: list[str]            # addends whose missing value was counted as zero
    detail: str

class Realignment(BaseModel):
    grid: Grid
    moved: list[tuple[int, int, int]]   # (col, from_row, to_row)
    unresolved: list[int]               # rows left off their label
    merged_labels: list[int]            # rows whose label cell holds several printed lines

class StatementReview(BaseModel):   # frozen
    statement_id: str
    status: Literal["passed", "needs_review"]
    reasons: list[str]
    warnings: list[str]
    numeric_cells: int
    checked_cells: int              # in at least one passing check
    flagged_cells: int
```

`GridCell` gains `source_row: int | None`, the docling row a moved cell came from, so
`Provenance.row` still names the cell docling produced. `StructureResult` gains
`reviews: list[StatementReview]`, and its `version` is "4".

New cell flags: `row_realigned`, `row_misaligned`, `label_merged`, `blank_confirmed`,
`digit_suspect`, `period_outlier`. New statement flags: `rows_realigned`,
`row_alignment_unresolved`, `tie_failed`, `needs_review`. New check details: `sum_based`,
`blank_as_zero`, `single_digit:10^k`.

An expected file, `eval/golden/expected/<document id>.json`:

```json
{
  "id": "almarai-2025-en",
  "sha256": "08ec6e6c...",
  "status": "draft",
  "checked_by": [],
  "note": "Prepared from the extraction and compared with the page image once.",
  "statements": [
    {
      "type": "balance",
      "pages": [156, 157, 158],
      "page_mode": "digital",
      "scale": 1000,
      "currency": "SAR",
      "periods": [{"key": "2025-12-31", "end_date": "2025-12-31", "kind": "instant", "months": null}],
      "rows": [
        {"label": "TOTAL ASSETS", "values": {"2025-12-31": "39966898"}, "unconfirmed": []}
      ]
    }
  ]
}
```

`status` is `draft` until two readings against the page are done (V1), then `checked` with the
readers in `checked_by`. A draft is written from the extraction with every figure listed in
`unconfirmed`; a period key leaves a row's list only when its figure has been compared with the
page image and found legible and equal, or corrected to what the page prints.

## Data flow

Steps 1 to 6 of spec 11 are unchanged. Step numbers below continue spec 11's.

6a. **Row alignment**, after the header is parsed and before line items are built.
   - For each data row: the label cell's top and bottom, and the row's value group, which is
     the value cells that start in that row and have a box. A group's position is the median
     of its cells' vertical centres. Slack is a quarter of the median value-cell height.
   - A row is off when its group lies outside its label's top and bottom by more than the
     slack. A row with a group and no label is unlabelled. With neither in the table, the grid
     is returned as it came, which is every golden table but one.
   - Each off or unlabelled group moves to the row whose label line contains it. When that row
     keeps a group of its own (a label cell holding two printed lines), the upper group stays
     and the lower one takes the unlabelled row between, if there is one. Vertical order is
     never changed by a move.
   - A group that fits no label line stays where it is and its cells carry `row_misaligned`;
     the statement carries `row_alignment_unresolved`. Moved cells carry `row_realigned` and
     keep their docling row in `source_row`; the statement carries `rows_realigned`.
   - A label cell taller than 1.7 value heights gives its row's cells `label_merged`. The text
     is not split: nothing on the page says where one label ends.
7. **Line items**, as in spec 11, from the realigned grid.
9. **Hierarchy.** Depth and section parents as in spec 11. After the sums are inferred, each
   addend's `parent_id` is the total that covers it, and an implicit total is `is_subtotal`.
10. **Checks.**
    - **Sums.** Rows are read top to bottom into a list of open rows. Per-share rows join
      nothing. A heading marks where a run starts and removes nothing from the list. For a
      row with values, suffixes of the open list are tried shortest first; a suffix needs two
      or more rows, two of them not zero throughout. In a period, a suffix fits when the row
      equals its sum within n x 0.5 (D6), clashes when every value is present and it does
      not, and is open when a value is missing.
      - A missing addend counts as zero when the sum then fits and some other period fits
        with every value present: `blank_as_zero`, and the cell is `blank_confirmed`.
      - A row with a total cue takes the shortest suffix that fits in at least one period and
        clashes in none: pass, `sum_based` when the suffix is not the run 3a would have
        taken. Failing that, the suffix that fits the most periods: its scope is confirmed
        by the periods that fit, and the periods that clash fail. Failing that, the run since
        the last heading or total, judged as in 3a, including `subtotal_scope_uncertain` and
        `subtotal_scope_unknown`.
      - A row without a cue takes the shortest suffix that fits in every period (blanks as
        above): an implicit total, as in 3a.
      - A row that closes a suffix replaces those rows in the open list. A total that fails
        against its run replaces the run.
    - **Identity**, as in spec 11.
    - **Net profit tie.** When a document has an income statement and a comprehensive income
      statement, the first valued row of comprehensive income must equal one row of the income
      statement in every period the two share. Kind `net_profit_tie`, recorded on the
      comprehensive income statement with both rows. No equal row: fail, and both statements
      carry `tie_failed`.
    - **Single digit.** A failed sum or identity whose difference is one digit times a power
      of ten gets `single_digit:10^k`. A cell is a candidate when changing that one digit of
      it settles the difference. A candidate is cleared when it sits in another check that
      passes in that period. The candidates left carry `digit_suspect`; with one left, the
      check's detail names it and the figure that would settle it. `reported` is never
      changed.
    - **Period outlier.** A row with two or more values that are not zero, where the largest
      is 1,000 times the smallest or more, carries `period_outlier` on those cells. Per-share,
      percentage and `implausible_magnitude` cells are left out.
10a. **Review.** A statement is `passed` when all of these hold, else `needs_review` with one
   reason per rule broken:
   - no check on it failed (`subtotal_failed`, `identity_failed`, `tie_failed`);
   - a balance sheet's identity passed in every period (`identity_not_checked` with the
     skipped check's detail otherwise);
   - at least one check passed (`unchecked`);
   - no cell carries `numbers_missing` without `blank_confirmed`, `unparsed`,
     `implausible_magnitude`, `digit_suspect`, `period_outlier`, `row_misaligned`,
     `row_realigned` or `ambiguous_separator`; the reason is the flag with its count;
   - no statement flag among `period_unbound`, `scale_conflict`, `currency_conflict`,
     `currency_missing`, `row_alignment_unresolved`;
   - it is the first statement of its type in the document (`duplicate_statement` on the
     others, which `ambiguous_statement` already marks).

   `scale_missing`, `currency_from_domicile` and `currency_inferred` are warnings, not reasons
   (open decision 2). A held statement carries the flag `needs_review`, which is what N4 reads.
   `checked_cells` counts numeric cells that are an addend or a total of a passing check in
   their period.
11. **Write** `statements.raw.json`, now with `reviews`, and `table_checks.json`.

`fra-ingest review-report <sha256>` takes a full hash or a unique prefix of one under the
artifact root, reads `statements.raw.json`, `table_checks.json`, `convert.json` and the docling
JSON for page sizes, and writes `review.html` beside them. It never runs convert or structure;
with an artifact missing it exits 2 and names it.

## The review report

One self-contained HTML file with no script dependencies, linking `pages/<n>.png` beside it.

- **Top.** Document, structure version, and a line per statement: type, pages, periods, scale
  and currency, `passed` or `needs_review` with its reasons, and checked cells out of numeric
  cells.
- **Per page.** The page image with a box over every extracted cell, placed by percentages of
  the page size. Colours: passing check, no check, warning flag, failed check or critical flag;
  a dashed box where the box was synthesized for a missing value; a second outline on a
  realigned cell. Each box shows its row id, label, period, raw text and flags on hover.
- **Beside it.** The statement's rows in order: id, depth, label, note, each period's value
  with its flags, and the checks on the row with expected, actual and difference. Hovering a
  row marks its boxes on the page, and the reverse.
- **Tables.** Every table decision with type or rejection, confidence and evidence (V7).

What it serves: V1 (each read figure next to the page), V2 (boxes under their period column),
V3 (totals with their addends and outcome), V4 (scale and currency in the statement line), V6
(rows across a page break, in order), and the reasons a statement is held.

## Failure handling

| Situation | Behaviour |
|-----------|-----------|
| A value group lies on no label line | Left in place, `row_misaligned`, statement `row_alignment_unresolved`, held |
| Rows were realigned | Cells `row_realigned`, statement `rows_realigned`, held: a repair is shown to a person before it is trusted |
| A label cell holds several lines | `label_merged` on the row's cells; a warning in the review |
| A total fits no suffix in any period | 3a's result against its run: fail, `subtotal_scope_uncertain` or `subtotal_scope_unknown` |
| A total fits in one period and clashes in another | Scope confirmed; the clashing period fails, with `single_digit:10^k` when one digit explains it |
| A missing addend, and the sum holds without it | Pass, `blank_as_zero`; the cell `blank_confirmed` |
| A missing addend, and the sum does not hold | `skipped`, `missing_values`, as in 3a |
| One income statement, no comprehensive income statement, or the reverse | No tie check |
| A statement with no passing check | Held, `unchecked` |
| An expected file is a draft | Its confirmed cells are scored and printed as provisional; it does not count towards G1 |
| An expected statement was not extracted | Every cell of it is a miss |
| `review-report` on an unknown or ambiguous hash, or a missing artifact | Exit 2 with the reason |

## Testing

Tests are written before the code they cover.

| Area | Cases | Marker |
|------|-------|--------|
| `infer_sums` | A total over two section totals across headings; a running total; an implicit total; a suffix that fits one period and clashes in the other; a blank addend counted as zero, and not counted when no period is complete; two non-zero rows needed; per-share rows left out; nothing fits, so 3a's run and its skip reasons | fast |
| Checks from sums | Every 3a subtotal test still passes; `sum_based` and `blank_as_zero` details; parents and `is_subtotal` from the groups | fast |
| `realign_rows` | An aligned grid returned unchanged; a group moved to the label line that contains it; a two-line label keeping the upper group and passing the lower to the unlabelled row; a group on no label line left and flagged; a mirrored table; `source_row` kept | fast |
| Figure checks | A single-digit difference with one candidate, with several, and with one cleared by a passing check; a difference that is not one digit; period outlier at 999 and 1,000 times; per-share rows exempt; tie pass, fail and absent | fast |
| `review_statement` | Each reason on its own; warnings do not hold; a clean statement passes; coverage counts | fast |
| Review report | Every cell has a box inside the page; classes follow status; text is escaped; relative image links; unique-prefix lookup; missing artifact exits 2 | fast |
| Extraction eval | Row alignment by label or value; a misread, a missing row and an extra row scored; sign and period accuracy; drafts kept out of the gate; unconfirmed cells out of the denominator | fast |
| End to end | Edita 2024 AR p5: rows 21 to 25 realigned, total equity 4,157,569,146, total assets 2023 `digit_suspect`, statement held. Almarai EN and AR: all three statements `passed`. Edita IFRS: total equity passes | `golden` |

Before each task that touches structure code, every document's `statements.raw.json` and
`table_checks.json` are copied out of `var/artifacts`, and compared after `make eval-structure`.
A difference that the task did not intend is a regression.

## Scoring

`make eval-structure`, as in spec 11, with one rule changed: a failed identity is accepted when
its statement is `needs_review` and a row of the check carries `numbers_missing` or
`digit_suspect`. It also prints each statement's review status.

`make eval-extraction` runs `eval/harness/extraction.py`. For each expected statement, its rows
are aligned in order with the extracted rows (two rows match when their labels are equal with
spaces ignored, or they share an equal non-zero value in one period), and every expected
numeric cell is scored: right when the aligned row holds the same value in that period.

| Measure | Definition | G1 |
|---------|------------|----|
| Numeric cell accuracy, digital pages | right cells / expected cells, statements with `page_mode` digital | 99.5% |
| Numeric cell accuracy, scanned pages | the same, `page_mode` scanned | 98.0% |
| Period mapping | expected periods found with the same key, end date, kind and length | 100% |
| Sign accuracy | among cells equal in absolute value, those equal in sign | 99.9% |

Only files with `status: checked` count. A draft starts as a copy of the extraction, so scoring
all of it would measure nothing: drafts are scored on their confirmed cells only, in a separate
block headed provisional. Unconfirmed cells are counted and left out of both blocks.

### Done when

| Measure | Target |
|---------|--------|
| Edita 2024 AR balance sheet | `needs_review`, with `identity_failed` and the suspect cell named; the equity and non-current liability rows on their own labels |
| Almarai EN and AR | Every statement `passed`; the pair check still 0 unmatched rows; no `subtotal_scope_unknown` on the three primary statements |
| Edita IFRS balance sheet | Total equity passes; no `subtotal_failed` |
| Every other golden statement | Values unchanged from version 3; every changed check and flag accounted for in Results |
| `make eval-structure` | PASS: every identity holds, or its statement is held for a reason that names the failing figure |
| Review report | Written for all 12 documents, every box inside its page |
| Extraction eval | Runs on the golden set. G1 figures stated for checked files; for drafts, stated as provisional with the count of unconfirmed cells |

## Risks

| ID | Risk | Mitigation |
|----|------|------------|
| R36 | A suffix sums to a total by coincidence and the wrong rows are taken for its scope | Two rows not zero; the shortest suffix; no period may clash; the golden comparison before and after |
| R37 | Geometry moves values on a layout where they were right: a label wrapped over lines, or a label box docling drew too small | A move needs the group outside its own label line and inside another's; anything else is flagged, not moved; a realigned statement is always held; 0 detections on the 32 other golden statements |
| R38 | The digit diagnosis names the wrong cell | It flags and never changes `reported`; candidates are cleared only by a check that passes |
| R39 | `passed` is taken for proof that every figure is right | Checked cells out of numeric cells on every statement; unvouched cells drawn apart in the report; the extraction eval measures what the checks cannot |
| R40 | A draft expected file is taken for a hand-checked one | `status` in the file; the harness keeps drafts out of the gate and prints them as provisional; unconfirmed cells listed per row |
| R41 | Counting a blank as zero hides a lost figure | Only when the total holds without it at the D6 tolerance, so the lost figure would have to be zero within rounding, and only with another period complete |
| R42 | The 1,000 times outlier rule flags a real swing | It holds the statement for a look; it changes nothing. Almarai's largest real ratio is 326 |

## Open decisions

1. **A figure the checks can name.** For Edita 2024 AR's 2023 total assets, the sum of its
   parts and the printed closing total both give 7,743,342,651 against a read of
   7,743,342,656. This design flags the cell and reports the figure that would settle it, and
   leaves `reported` as read. The alternative is to replace the read when two independent
   printed witnesses agree. Recommended: flag only, until the extraction eval shows how often
   the suggestion is right.
2. **Scale.** `scale_missing` is a warning, not a reason to hold, because the ten Juhayna and
   Edita documents print no scale and are in units. This rests on the scales still marked
   `unconfirmed` in the manifest; confirming them there closes it.
3. **Expected files.** Two readings per file are the owner's (V1). Suggested order: Almarai EN
   (digital), Edita 2025 EN IFRS (scanned, English), Edita 2024 AR (scanned, Arabic).
