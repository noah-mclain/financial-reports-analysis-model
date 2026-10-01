# 12. Ingest, Part 3b: Review

Status: v1, 2026-10-01, with results of the same day. Completes the structure stage of
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
   - A shifted table with a value that has text and no box is not repaired: such a value cannot
     be placed, and a moved cell could land on top of it. The rows that are off are flagged.
   - A repair that would leave the rows out of page order, or lose a group, is not made: every
     row that was off stays and is flagged.
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
      - A period in which the total and every addend are zero fits any rows at all, so it
        confirms no suffix.
      - A total with exactly one row above it in its run passes with `single_addend` when the
        two are exactly equal (one addend rounds nothing), and passes as a running total when
        it is that row plus the total before it. Otherwise it is skipped as before.
      - Per-share rows are those whose label says so (`per ... share`, `EPS`, `DPS`, `السهم`,
        `لكل سهم`), and the basic and diluted rows under a heading that says so. The heading
        reaches no other row.
    - **Identity**, as in spec 11.
    - **Net profit tie.** When a document has an income statement and a comprehensive income
      statement, the first valued row of comprehensive income must equal one row of the income
      statement in every period the two share. Kind `net_profit_tie`, recorded on the
      comprehensive income statement with both rows. No equal row: fail, and both statements
      carry `tie_failed`.
    - **Single digit.** A failed sum or identity whose difference is one digit times a power
      of ten gets `single_digit:10^k`. A cell is a candidate when changing that one digit of
      it settles the difference, or when the figure read is the settling figure with its leading
      digit lost (302,414,061 read as 2,414,061; at most two zeros between, and never from a
      zero). A candidate is cleared when it sits in another check that
      passes in that period. The candidates left carry `digit_suspect`; with one left, the
      check's detail names it and the figure that would settle it. `reported` is never
      changed.
    - **Period outlier.** A row with two or more values that are not zero, where the largest
      is 1,000 times the smallest or more, carries `period_outlier` on those cells. Per-share,
      percentage and `implausible_magnitude` cells are left out.
    - **Fraction among whole amounts.** A figure with a fractional part, in a statement where
      at most a fifth of the amounts have one, carries `fraction_among_whole`: a decimal mark
      OCR put into a number. It must stand beside a whole amount in its row or be 1,000 or
      more: a row of small fractions alone is a per-share figure or a ratio the label cues
      missed. Per-share and percentage cells are not amounts; a statement printed with
      decimals throughout is left alone.
10a. **Review.** A statement is `passed` when all of these hold, else `needs_review` with one
   reason per rule broken:
   - no check on it failed (`subtotal_failed`, `identity_failed`, `tie_failed`);
   - no subtotal check on it was skipped, for whatever cause (`subtotal_not_checked` with the
     count): a total nothing could be checked against vouches for nothing;
   - a balance sheet's identity passed in every period (`identity_not_checked` with the
     skipped check's detail otherwise);
   - at least one check passed, not counting a total equal to the single row above it, which
     shows only that a figure was printed twice (`unchecked`);
   - no cell carries `numbers_missing` without `blank_confirmed`, `unparsed`,
     `implausible_magnitude`, `digit_suspect`, `period_outlier`, `fraction_among_whole`,
     `row_misaligned`, `row_realigned` or `ambiguous_separator`; the reason is the flag with
     its count;
   - no statement flag among `period_unbound`, `scale_conflict`, `currency_conflict`,
     `currency_missing`, `row_alignment_unresolved`, `scale_implausible` (spec 13),
     `value_without_box`;
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
| A value group lies on no label line, or the table is shifted and holds a value with no box | Left in place, `row_misaligned`, statement `row_alignment_unresolved`, held |
| Rows were realigned | Cells `row_realigned`, statement `rows_realigned`, held: a repair is shown to a person before it is trusted |
| A label cell holds several lines | `label_merged` on the row's cells; a warning in the review |
| A total fits no suffix in any period | 3a's result against its run: fail, `subtotal_scope_uncertain` or `subtotal_scope_unknown` |
| A total fits in one period and clashes in another | Scope confirmed; the clashing period fails, with `single_digit:10^k` when one digit explains it |
| A missing addend, and the sum holds without it | Pass, `blank_as_zero`; the cell `blank_confirmed` |
| A missing addend, and the sum does not hold | `skipped`, `missing_values`, as in 3a |
| One income statement, no comprehensive income statement, or the reverse | No tie check |
| A statement with no passing check | Held, `unchecked` |
| An expected file is a draft | Its confirmed cells are scored and printed as provisional; it does not count towards G1 |
| An expected statement was not extracted | Every cell of it is a miss, blank cells included |
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
confirmed cell is scored: right when the aligned row holds the same value in that period and
does not sit under another row's label. Every expected label of the statement is looked for
inside the label read; those found compete, and the one that accounts for the longest stretch
of the label read is the label it is, so "Cash and cash equivalents" read with a typo is that
label and not "Cash". A figure is mislabelled when its row has no label, when the label read is
another row's or reads as nothing of its own row (a section heading alone), or when its own
label is found only inside the stretch another label accounts for. A cell holding two labels
side by side, or a heading before the row's label, serves the row; so does a label garbled but
still readable as the row's own. Such a figure is
wrong and counted as mislabelled. Where the expected
file says nothing is printed, nothing must be read. Extracted rows with values that no expected
row aligns to are counted as extra rows and printed.

| Measure | Definition | G1 |
|---------|------------|----|
| Numeric cell accuracy, digital pages | right cells / expected cells, statements with `page_mode` digital | 99.5% |
| Numeric cell accuracy, scanned pages | the same, `page_mode` scanned | 98.0% |
| Period mapping | expected periods found with the same key, end date, kind and length | 100% |
| Sign accuracy | among cells equal in absolute value, those equal in sign | 99.9% |

Only files with `status: checked` count, and a file cannot be `checked` without two different
readers in `checked_by` and an empty `unconfirmed` on every row. A draft starts as a copy of the extraction, so scoring
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

## Results

Measured 2026-10-01 on the `ingest-structure` branch, structure version 4, with
`make eval-structure` and `make eval-extraction` on the stored conversions of the golden set.
Structure version 5 adds unit caveats ([13-unit-caveats.md](13-unit-caveats.md)), and versions
6 to 8 the fixes from the review of this branch. The review table and the extraction figures
below are from version 8; no value, check or review status differs from version 4.

### Against Done when

| Measure | Target | Result |
|---------|--------|--------|
| Edita 2024 AR balance sheet | `needs_review`, with `identity_failed` and the suspect cell named; the equity and non-current liability rows on their own labels | met. Held with `subtotal_failed`, `identity_failed`, 6 `digit_suspect` cells, 7 `row_realigned` cells and the figures OCR lost. The 2023 identity fails by 5 and names `p5-t0-r14` at 7,743,342,651, which is what the page prints. Total equity (4,157,569,146) is on `r22`, non-controlling interests on `r21`, borrowings on `r24`, government grants on `r25`, and `r23` is the heading it prints as |
| Almarai EN and AR | Every statement `passed`; the pair check still 0 unmatched rows; no `subtotal_scope_unknown` on the three primary statements | met. The six primary statements pass, the pair is (0, 0) on all three, and no subtotal on them is skipped |
| Edita IFRS balance sheet | Total equity passes; no `subtotal_failed` | met. All totals pass, 68 of 68 numeric cells sit in a passing check, and the statement passes review |
| Every other golden statement | Values unchanged from version 3 | met. Against the version 3 output, 10 cells differ, all on Edita 2024 AR p5 and all moved by row alignment. No value, label, period, scale or currency changed on the other eleven documents |
| `make eval-structure` | PASS | met |
| Review report | Written for all 12 documents, every box inside its page | met in part. 12 reports, 1,302 boxes. Boxes are clamped to the page when drawn, so "inside its page" holds by construction and says nothing about whether a box is right. On Edita 2024 AR p5 the boxes were drawn over the page image and sit on the printed figures; the other pages were not looked at this way, and the report was not opened in a browser |
| Extraction eval | Runs on the golden set; G1 stated for checked files, drafts as provisional | runs. No expected file is checked, so **G1 is not measured**. Provisional figures are below |

### Checks, version 3 against version 4

Counted per check and period over the twelve documents.

| Check | Outcome | Version 3 | Version 4 |
|-------|---------|-----------|-----------|
| Subtotal | pass | 139 | 184 |
| Subtotal | fail | 19 | 18 |
| Subtotal | skipped, `subtotal_scope_unknown` | 79 | 51 |
| Subtotal | skipped, `subtotal_scope_uncertain` | 8 | 6 |
| Subtotal | skipped, `missing_values` | 23 | 19 |
| Balance identity | pass / fail / skipped | 14 / 1 / 12 | 14 / 1 / 12 |
| Net profit tie | pass / fail | none | 14 / 3 |

The 51 that remain `subtotal_scope_unknown` are totals with fewer than two open rows above them
that no sum explains: 28 on second tables of a type (Almarai's five-year highlights table in both
editions, and a table on page 28 of Juhayna 2024 EN), 16 on unlabelled rows of the scanned
Juhayna and Edita sheets, and 7 on labelled totals of scanned statements. The tie passes on seven documents and fails on Juhayna 2025 EN
consolidated and standalone, whose comprehensive income cells hold two merged figures.

Single-digit failures found: Edita 2024 AR non-current assets (800,000) and current assets
(5,000,000) for 2024 and total assets (5) for 2023; Edita EAS non-current liabilities (20,000)
for 2024; Juhayna 2024 EN total equity (10) for 2024. On Edita 2024 AR the page confirms all
three: the misread cells (202,114,513 read as 202,914,513; 136,103,684 read as 131,103,684;
7,743,342,651 read as 7,743,342,656) are among the suspects each time, and total assets is the
only suspect of its check.

### Review status

The first statement of each type per document, 29 in all: 12 pass and 17 are held. Four more
statements are second tables of a type and are held as `duplicate_statement`. Across the 29, 672
of 980 numeric cells sit in a passing check.

| Document | Statement | Review | Cells in a passing check | Reasons |
|----------|-----------|--------|--------------------------|---------|
| almarai-2025-en | balance | passed | 80 of 80 |  |
| almarai-2025-en | comprehensive income | passed | 20 of 20 |  |
| almarai-2025-en | income | passed | 34 of 38 |  |
| almarai-2025-ar | balance | passed | 80 of 80 |  |
| almarai-2025-ar | comprehensive income | passed | 20 of 20 |  |
| almarai-2025-ar | income | passed | 34 of 38 |  |
| juhayna-2025-ar-standalone | income | held | 0 of 15 | `unchecked`, `numbers_missing:4`, `unparsed:9` |
| juhayna-2025-ar-standalone | balance, comprehensive income | not extracted | | |
| juhayna-2025-ar-consolidated | income | held | 6 of 30 | `numbers_missing:2`, `unparsed:8` |
| juhayna-2025-ar-consolidated | balance, comprehensive income | not extracted | | |
| juhayna-2025-en-consolidated | balance | held | 0 of 7 | `identity_not_checked:identity_totals_not_found`, `subtotal_not_checked:8`, `unchecked`, `numbers_missing:5`, `period_unbound:1` |
| juhayna-2025-en-consolidated | comprehensive income | held | 0 of 5 | `subtotal_failed`, `tie_failed`, `subtotal_not_checked:1`, `unchecked`, `unparsed:2`, `implausible_magnitude:4` |
| juhayna-2025-en-consolidated | income | held | 38 of 40 | `tie_failed` |
| juhayna-2024-ar-consolidated | comprehensive income | held | 6 of 11 | `subtotal_not_checked:2`, `numbers_missing:2`, `unparsed:1` |
| juhayna-2024-ar-consolidated | balance, income | not extracted | | |
| juhayna-2024-en-consolidated | balance | held | 12 of 73 | `subtotal_failed`, `identity_not_checked:identity_totals_not_found`, `subtotal_not_checked:10`, `numbers_missing:2`, `unparsed:1`, `implausible_magnitude:2`, `digit_suspect:3` |
| juhayna-2024-en-consolidated | comprehensive income | passed | 12 of 12 |  |
| juhayna-2024-en-consolidated | income | passed | 36 of 38 |  |
| edita-2025-ar-consolidated | balance | held | 14 of 37 | `subtotal_failed`, `identity_not_checked:identity_totals_not_found`, `subtotal_not_checked:3`, `unparsed:2`, `period_unbound:0` |
| edita-2025-ar-consolidated | comprehensive income | held | 5 of 12 | `subtotal_failed`, `subtotal_not_checked:2`, `unparsed:2` |
| edita-2025-ar-consolidated | income | held | 12 of 34 | `subtotal_not_checked:2`, `numbers_missing:1`, `unparsed:7` |
| edita-2025-en-consolidated-eas | balance | held | 17 of 72 | `subtotal_failed`, `subtotal_not_checked:7`, `numbers_missing:3`, `unparsed:1`, `implausible_magnitude:3`, `digit_suspect:3` |
| edita-2025-en-consolidated-eas | comprehensive income | passed | 14 of 14 |  |
| edita-2025-en-consolidated-eas | income | passed | 37 of 39 |  |
| edita-2025-en-consolidated-ifrs | balance | passed | 68 of 68 |  |
| edita-2025-en-consolidated-ifrs | comprehensive income | held | 14 of 14 | `subtotal_not_checked:2`, `numbers_missing:1`, `unparsed:1` |
| edita-2025-en-consolidated-ifrs | income | held | 37 of 38 | `unparsed:1` |
| edita-2024-ar-consolidated | balance | held | 28 of 67 | `subtotal_failed`, `identity_failed`, `subtotal_not_checked:5`, `numbers_missing:5`, `unparsed:2`, `implausible_magnitude:3`, `digit_suspect:6`, `period_outlier:4`, `fraction_among_whole:1`, `row_realigned:7` |
| edita-2024-ar-consolidated | comprehensive income | passed | 14 of 14 |  |
| edita-2024-ar-consolidated | income | held | 8 of 31 | `unparsed:7` |
| juhayna-2025-en-standalone | comprehensive income | held | 0 of 5 | `subtotal_failed`, `tie_failed`, `subtotal_not_checked:1`, `unchecked`, `unparsed:1`, `implausible_magnitude:2` |
| juhayna-2025-en-standalone | income | held | 26 of 28 | `tie_failed`, `ambiguous_separator:1` |
| juhayna-2025-en-standalone | balance | not extracted | | |

Seven of the twelve passes are on statements nobody has compared with the page: Almarai AR's
three, Edita EAS's two and Juhayna 2024 EN's two. Their checks hold; that is all a pass says.

### Extraction eval, provisional

Three expected files exist, all with `status: draft`. Each was prepared from the extraction and
compared with its page images once, figure by figure; none has had the two readings V1 asks for,
so none counts towards G1 and the figures below are not gate results. No figure is left
unconfirmed in the three files.

| Document | Pages | Figures | Right | Share | Misread | Not read |
|----------|-------|---------|-------|-------|---------|----------|
| almarai-2025-en | digital | 138 | 138 | 100.00% | 0 | 0 |
| edita-2025-en-consolidated-ifrs | scanned, English | 126 | 120 | 95.24% | 0 | 6 |
| edita-2024-ar-consolidated | scanned, Arabic | 128 | 98 | 76.56% | 11 | 18 |
| Digital | | 138 | 138 | 100.00% | | |
| Scanned | | 254 | 218 | 85.83% | | |

One more figure on Edita 2024 AR is read correctly and counted wrong: non-controlling interests
for 2024 (102,084,427) sits on a row with no label, because its label is merged into the cell
above. One extracted row aligns to no expected row.

Period mapping is 18 of 18, sign 357 of 357 on the figures equal in size, and scale and
currency 18 of 18 against the files (the Edita scales are still `unconfirmed` in the manifest).

How each file was compared:

- Almarai EN: every row and figure on pages 156 to 161 against the image. Each figure was also
  re-read from the PDF text layer inside its box, 138 of 138 equal.
- Edita IFRS: pages 8 to 10 print English digits legibly. The six misses are figures the
  extraction did not read: three printed dashes, the 2024 earnings per share, and both figures
  of total comprehensive income.
- Edita 2024 AR: pages 5 to 7 print Arabic-Indic digits, read at three times zoom. Every sum
  the pages print holds on the figures as read, which is what makes the corrections credible
  from one reading. The row for total current liabilities, which the extraction merged into the
  row above it, was added by hand.

What the checks did with the 36 wrong figures on the two scanned documents:

| | Figures |
|---|---|
| Not read at all, so missing or flagged | 24 |
| Misread, and the cell flagged | 6 |
| Misread, not flagged, inside a failed check | 2 |
| Misread with no flag and no failed check on it | 3 |
| Read correctly, on a row with no label; the cell flagged `row_realigned` | 1 |

The three silent ones are on Edita 2024 AR, on statements held for other reasons: deferred tax
liabilities 2024 (302,414,061 read as 2,414,061), other credit balances 2024 (643,699,632 read
as 143,699,632) and profit before tax 2024 (2,163,025,380 read as 21,302,538). Each sits in a
block whose total could not be checked because other figures in it were lost. No statement that
passed review holds a misread figure in these three documents; the Edita IFRS balance sheet
passed with two unread dashes, which its sums confirmed as blanks.

### What this says

- Almarai, the digital document, is right on every figure compared, and both editions pass.
- The scanned English sheet is close: 95.24%, every miss a figure not read, none misread.
- The scanned Arabic sheet is at 76.56%, against a G1 threshold of 98.0% for scanned pages.
  Part 3b makes its faults visible and holds the statements; it does not make the OCR read
  them. That is the week 2 bake-off's job, and this eval is now there to score it.
- The checks caught or held 33 of the 36 wrong figures by themselves. The three they missed
  are the case R39 names. A statement with any subtotal that could not be checked is now held
  (`subtotal_not_checked`), which covers the blocks two of the three sit in. The third, profit
  before tax, is on an income statement held only for its unparsed cells.

### Known limits

Found by the review of this branch. The first list is fixed; the second is left as it is, each
with the case that shows it.

Fixed, each with a test:

- A period of zeros no longer confirms which rows a total covers.
- A figure that lost its leading digit (302,414,061 read as 2,414,061) is among the suspects of
  a single-digit failure.
- Per-share rows named only by their heading ("- Basic" under an earnings-per-share heading),
  by `EPS` or `DPS`, or by an Arabic label OCR clipped, are recognised. The heading reaches
  only basic and diluted rows, so an amount printed after them is not taken for per-share.
- A fraction among whole amounts (132,705,608 read as 1327.5608) is flagged and holds. A row
  of small fractions alone is not, so a per-share row the cues miss does not hold a statement.
- A total over a single row is checked as an exact equality, or as that row plus the total
  before it, instead of holding its statement. An equality alone does not make a statement
  count as checked.
- `eval/harness` is under `make typecheck`.
- The eval's label rule no longer confuses a short label with a longer one that contains it.
  Stress on the three expected files (`eval/harness/label_stress.py`): a correct figure whose own label has one character
  corrupted is judged wrong in 1 of 3,888 cases (6.1% before); a figure under its neighbour's
  label is judged right in 0 of 346 cases, 0 of 7,174 with one character of that label
  corrupted, and 48 of 6,429 (0.75%) with two; a cell holding a row's label merged with its
  neighbour's counts the figure wrong in 3 of 346 cases.

Left:

- **An implicit total by rounding.** Two addends allow a difference of 1. With small printed
  figures (3, 4, then 8) a plain row can be taken for the sum of the two above it. The real
  total is then unexplained, and the statement is held as `subtotal_not_checked`. Needs figures
  printed in millions; none in the golden set.
- **The eval's label rule is a judgement, not a proof.** The residual rates are the ones above.
  A label garbled beyond reading counts its figure wrong even when the figure is on the right
  row.
- **Extra rows gate nothing.** An extracted row with values that no expected row aligns to is
  counted and printed, but G1 has no threshold for it.
- **`period_unbound:0` reads like a count** in the review reasons; the number is a column.

## Decisions

1. **A figure the checks can name.** For Edita 2024 AR's 2023 total assets, the sum of its
   parts and the printed closing total both give 7,743,342,651 against a read of
   7,743,342,656. Decided by the owner, 2026-10-01 (D10): the cell is flagged and the figure
   that would settle it is reported; `reported` stays as read. To revisit once the extraction
   eval shows how often the suggestion is right.
2. **Scale.** Decided by the owner, 2026-10-01 (D11): `scale_missing` is a warning, not a reason
   to hold, and every amount that depends on the assumed scale carries a footnote, through to
   the written summary. The plan is [13-unit-caveats.md](13-unit-caveats.md). The scales marked
   `unconfirmed` in the manifest still need confirming per document before they are scored.
3. **Expected files.** Open. The three drafts in `eval/golden/expected/` have been compared
   with their pages once. V1 asks for two independent readings per file before it is `checked`;
   until a second reading is done, Gate G1 is not measured. Order when more are added: digital
   first, then scanned English, then scanned Arabic.
4. **Pull request #4** stays a draft until the last changes on this branch are in, by the
   owner's decision of 2026-10-01.
