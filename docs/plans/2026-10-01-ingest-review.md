# Ingest Part 3b: Review Implementation Plan

**Goal:** Settle which rows each total covers by the sums, put shifted values back on their labels, name the cell a failed sum points at, decide per statement whether it is passed or held for review, show all of it on the page image, and score the extraction against expected files.

**Architecture:** Everything runs inside the structure stage and stays pure: `infer_sums` replaces 3a's run-based subtotal scope, `realign_rows` repairs a grid before line items are built, `figure_checks` adds the single-digit diagnosis, period outliers and the net profit tie, and `review_statement` turns checks and flags into `passed` or `needs_review`. `fra-ingest review-report <sha256>` renders the stored results over the page images Part 2 wrote. The extraction eval reads `eval/golden/expected/` and keeps drafts out of the gate.

**Tech Stack:** Python 3.12, pydantic 2, the `fra_core` schemas and parsers, pytest, uv. No new dependency: the report is built with `html.escape` and string joins.

**Spec:** `docs/blueprint/12-ingest-review.md` (read it first). Background: `docs/blueprint/11-ingest-structure.md`, `docs/blueprint/05-verification-and-test.md` V1 to V8, `docs/blueprint/04-execution-phases.md` 1.6, 1.8, 1.9 and Gate G1.

## Global Constraints

- Nothing in this part imports `docling` or `docling_core`. Page sizes come from the docling JSON through `fra_ingest/docling_json.py`.
- Tests are written before the code they cover. `make test`, `make lint`, `make typecheck` and `make docs-check` pass before every commit.
- `STRUCTURE_VERSION = "4"`, bumped in Task 1. Artifacts stay under `<artifact_root>/<sha256>/`; `review.html` joins them. `var/` is gitignored; never commit artifacts or PDFs.
- Tolerance is D6: n x 0.5 reported units, n the number of addends present. A suffix needs two or more rows, two of them not zero throughout.
- `Cell.reported` is never changed by a check. A repair moves cells; it does not edit them.
- The rules 3a settled stay: a row with no values is a heading, except a known total; `subtotal: true` in the taxonomy alone does not make a printed total; "Net" is no cue; per-share rows join no sum; an implicit total needs two rows that are not zero.
- Before each task that changes structure output, copy every document's `statements.raw.json` and `table_checks.json` out of `var/artifacts`, run `make eval-structure` after it, and compare. Record every intended difference in the commit message body; any other difference is a regression.
- Expected files are drafts until two readings are done. No figure is presented as hand-checked that was not.
- Never run anything against the `blind` or `model_test` pools.
- Commits: author and committer `noah-mclain <nadam.30032415@gmail.com>`; messages carry no trailers.

## Review Focus

- A total over one row and dashes (one non-zero addend) must still be checked against its run as in 3a, not skipped because no suffix qualifies (Task 1, run fallback test).
- An addend that is `unparsed` (text on the page that did not read) must not be counted as a blank zero (Task 1, blank test).
- A label wrapped over two lines with its values beside the first or the last line must not be taken for a shifted row (Task 2, wrapped label test).
- A failed sum whose difference is one digit times a power of ten, where settling it would change a figure's length (95 to 105), must not name that cell (Task 3, carry test).
- Label text holding `<`, `&` or a quote must not break the report, and a statement whose page image is missing must be listed without its overlay, not crash the report (Task 5, escape and missing image tests).
- An expected statement the extraction did not produce must count every cell as a miss, not drop out of the score (Task 7, missing statement test).

## File Structure

| File | Task | Responsibility |
|------|------|----------------|
| `packages/ingest/src/fra_ingest/sum_hierarchy.py` | 1 | `infer_sums`, `SumGroup`, `SumOutcome` |
| `packages/ingest/src/fra_ingest/table_checks.py` | 1, 3 | Subtotal results from sum groups; parents and `blank_confirmed`; calls the figure checks |
| `packages/ingest/src/fra_ingest/row_alignment.py` | 2 | `realign_rows`, `Realignment` |
| `packages/ingest/src/fra_ingest/table_grid.py`, `parts.py` | 2 | `GridCell.source_row`; `build_part(..., merged_rows=...)` |
| `packages/ingest/src/fra_ingest/figure_checks.py` | 3 | `diagnose_digits`, `flag_period_outliers`, `check_net_profit_tie` |
| `packages/core/src/fra_core/schemas/check.py` | 3 | `kind` gains `net_profit_tie` |
| `packages/ingest/src/fra_ingest/review.py` | 4 | `review_statement`, `StatementReview` |
| `packages/ingest/src/fra_ingest/results.py`, `structure.py` | 1 to 4 | `StructureResult.reviews`; orchestration |
| `packages/ingest/src/fra_ingest/review_report.py`, `cli.py` | 5 | The report and its command |
| `eval/harness/structure.py` | 6 | Identity rule for held statements; review status printed |
| `eval/harness/expected.py`, `eval/harness/extraction.py`, `Makefile` | 7 | Drafts and the extraction eval |
| `eval/golden/expected/*.json`, `docs/blueprint/12-ingest-review.md`, `var/pr/ingest-structure.md` | 8 | Drafts with a first comparison against the page image; results; pull request text |

---

### Task 1: Sum-based hierarchy

**Files:**
- Create: `packages/ingest/src/fra_ingest/sum_hierarchy.py`
- Modify: `packages/ingest/src/fra_ingest/table_checks.py` (`check_subtotals`, `run_checks`), `packages/ingest/src/fra_ingest/structure.py` (`STRUCTURE_VERSION`), `packages/ingest/src/fra_ingest/hierarchy.py` (docstring)
- Test: `packages/ingest/tests/test_sum_hierarchy.py`, `packages/ingest/tests/test_table_checks.py`, `packages/ingest/tests/test_structure_golden.py`

**Interfaces:**
- Produces: `infer_sums(statement: Statement) -> list[SumGroup]`; `SumGroup(total_id: str, addend_ids: tuple[str, ...], basis: Literal["sums", "run"], implicit: bool, outcomes: dict[str, SumOutcome])` with `confirmed: bool`; `SumOutcome(status: Literal["pass", "fail", "skipped"], addend_ids: tuple[str, ...], expected: Decimal | None, actual: Decimal | None, blank_ids: tuple[str, ...], detail: str)`.
- Produces: `check_subtotals(statement) -> list[CheckResult]` (unchanged signature) and `run_checks(statement, index) -> tuple[Statement, list[CheckResult]]`, which now also sets `parent_id` of each addend of a confirmed group to its total, `is_subtotal` on implicit totals, and `blank_confirmed` on cells counted as zero. Check details are joined with `"; "`: `implicit_subtotal`, `sum_based`, `blank_as_zero`.

- [ ] **Step 1: Snapshot the golden output**

```bash
S=$(mktemp -d) && for d in var/artifacts/*/; do mkdir -p $S/$(basename $d) && cp $d/statements.raw.json $d/table_checks.json $S/$(basename $d)/; done && echo $S
```

- [ ] **Step 2: Write the failing tests**

`packages/ingest/tests/test_sum_hierarchy.py`:

```python
"""Which rows each total covers, found by the sums (spec 12, Data flow step 10)."""

from datetime import date
from decimal import Decimal

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.sum_hierarchy import infer_sums

P1 = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
P2 = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, label: str, *values: str | None, total: bool = False) -> LineItem:
    """A row with one value per period: a number, "" for a blank cell, "?" for a cell that did
    not parse. No values at all makes a heading."""
    cells = []
    for period, value in zip((P1, P2), values, strict=False):
        assert value is not None
        flags = ["numbers_missing"] if value == "" else ["unparsed"] if value == "?" else []
        cells.append(
            Cell(
                period_key=period.key,
                reported=None if flags else Decimal(value),
                raw_text="" if value == "" else value,
                provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
                flags=flags,
            )
        )
    return LineItem(id=f"r{n}", raw_label=label, is_subtotal=total, cells=cells)


def statement(items: list[LineItem], periods: int = 2) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[P1, P2][:periods],
        line_items=items,
    )


BALANCE = [
    item(1, "Non-current assets"),
    item(2, "Property", "100", "90"),
    item(3, "Goodwill", "20", "20"),
    item(4, "Total non-current assets", "120", "110", total=True),
    item(5, "Current assets"),
    item(6, "Inventories", "30", "25"),
    item(7, "Cash", "10", "5"),
    item(8, "Total current assets", "40", "30", total=True),
    item(9, "Total assets", "160", "140", total=True),
]


def test_a_total_over_two_section_totals_is_found_across_headings() -> None:
    groups = {g.total_id: g for g in infer_sums(statement(BALANCE))}
    assert groups["r9"].addend_ids == ("r4", "r8")
    assert groups["r9"].basis == "sums"
    assert [o.status for o in groups["r9"].outcomes.values()] == ["pass", "pass"]
    assert groups["r4"].basis == "run" and groups["r4"].addend_ids == ("r2", "r3")


def test_a_running_total_takes_the_total_before_it() -> None:
    rows = [
        item(1, "Revenue", "100", "90"),
        item(2, "Cost of sales", "-60", "-50"),
        item(3, "Total gross profit", "40", "40", total=True),
        item(4, "Selling expenses", "-10", "-10"),
        item(5, "Administrative expenses", "-5", "-5"),
        item(6, "Total operating profit", "25", "25", total=True),
    ]
    groups = {g.total_id: g for g in infer_sums(statement(rows))}
    assert groups["r6"].addend_ids == ("r3", "r4", "r5")
    assert groups["r6"].basis == "run"


def test_a_row_without_a_cue_that_sums_the_rows_above_is_an_implicit_total() -> None:
    rows = [
        item(1, "Share capital", "100", "100"),
        item(2, "Retained earnings", "50", "40"),
        item(3, "Equity attributable to owners", "150", "140"),
        item(4, "Non-controlling interests", "10", "10"),
        item(5, "Total equity", "160", "150", total=True),
    ]
    groups = {g.total_id: g for g in infer_sums(statement(rows))}
    assert groups["r3"].implicit and groups["r3"].addend_ids == ("r1", "r2")
    assert groups["r5"].addend_ids == ("r3", "r4")


def test_a_suffix_that_fits_one_period_confirms_the_scope_and_the_other_period_fails() -> None:
    rows = [*BALANCE[:8], item(9, "Total assets", "160", "145", total=True)]
    group = next(g for g in infer_sums(statement(rows)) if g.total_id == "r9")
    assert group.addend_ids == ("r4", "r8") and group.basis == "sums"
    assert group.outcomes[P1.key].status == "pass"
    assert group.outcomes[P2.key].status == "fail"
    assert group.outcomes[P2.key].expected == Decimal("140")


def test_a_blank_addend_counts_as_zero_when_the_sum_holds_and_another_period_is_complete() -> None:
    rows = [
        item(1, "Share capital", "100", "100"),
        item(2, "Treasury shares", "-20", ""),
        item(3, "Retained earnings", "50", "40"),
        item(4, "Total equity", "130", "140", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.outcomes[P1.key].blank_ids == ()
    assert group.outcomes[P2.key].status == "pass"
    assert group.outcomes[P2.key].blank_ids == ("r2",)


def test_a_blank_is_not_counted_when_no_period_is_complete() -> None:
    rows = [
        item(1, "Share capital", "100"),
        item(2, "Treasury shares", ""),
        item(3, "Retained earnings", "50"),
        item(4, "Total equity", "150", total=True),
    ]
    group = infer_sums(statement(rows, periods=1))[0]
    assert group.outcomes[P1.key].status == "skipped"
    assert group.outcomes[P1.key].detail == "missing_values"


def test_an_unparsed_addend_is_a_lost_value_not_a_blank() -> None:
    rows = [
        item(1, "Share capital", "100", "100"),
        item(2, "Reserves", "30", "?"),
        item(3, "Retained earnings", "50", "40"),
        item(4, "Total equity", "180", "140", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.outcomes[P2.key].status == "skipped"
    assert group.outcomes[P2.key].detail == "missing_values"


def test_a_total_over_one_row_and_dashes_is_checked_against_its_run() -> None:
    rows = [
        item(1, "Assets"),
        item(2, "Investments", "0", "0"),
        item(3, "Cash", "70", "60"),
        item(4, "Total assets", "70", "65", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.basis == "run"
    assert [o.status for o in group.outcomes.values()] == ["pass", "fail"]


def test_per_share_rows_join_no_sum() -> None:
    rows = [
        item(1, "Profit", "100", "90"),
        item(2, "Other income", "10", "10"),
        LineItem(
            id="r3",
            raw_label="Earnings per share",
            cells=[c.model_copy(update={"flags": ["per_share"]}) for c in item(3, "", "2", "1").cells],
        ),
        item(4, "Total income", "110", "100", total=True),
    ]
    assert infer_sums(statement(rows))[0].addend_ids == ("r1", "r2")


def test_nothing_fits_so_the_run_is_judged_as_before() -> None:
    lone = [item(1, "Assets"), item(2, "Cash", "70", "60"), item(3, "Total", "99", "98", total=True)]
    assert {o.detail for o in infer_sums(statement(lone))[0].outcomes.values()} == {
        "subtotal_scope_unknown"
    }
    cut = [
        item(1, "Property", "100", "90"),
        item(2, "Current assets"),
        item(3, "Inventories", "30", "25"),
        item(4, "Cash", "10", "5"),
        item(5, "Total assets", "999", "999", total=True),
    ]
    assert {o.detail for o in infer_sums(statement(cut))[0].outcomes.values()} == {
        "subtotal_scope_uncertain"
    }
```

Append to `packages/ingest/tests/test_table_checks.py` (it already has `item`, `statement`, `INDEX`):

```python
def test_sum_groups_set_parents_implicit_totals_and_confirmed_blanks() -> None:
    rows = [
        item(1, "Share capital", "100"),
        item(2, "Retained earnings", "50"),
        item(3, "Equity attributable to owners", "150"),
        item(4, "Non-controlling interests", "10"),
        item(5, "Total equity", "160", subtotal=True),
    ]
    checked, results = run_checks(statement(rows), INDEX)
    by_id = {i.id: i for i in checked.line_items}
    assert by_id["r1"].parent_id == "r3" and by_id["r3"].parent_id == "r5"
    assert by_id["r3"].is_subtotal
    assert [r.detail for r in results if r.kind == "subtotal"] == ["implicit_subtotal", ""]


def test_a_total_found_by_sums_says_so() -> None:
    rows = [
        item(1, "Non-current assets", None),
        item(2, "Property", "100"),
        item(3, "Goodwill", "20"),
        item(4, "Total non-current assets", "120", subtotal=True),
        item(5, "Current assets", None),
        item(6, "Inventories", "30"),
        item(7, "Cash", "10"),
        item(8, "Total current assets", "40", subtotal=True),
        item(9, "Total assets", "160", subtotal=True),
    ]
    results = check_subtotals(statement(rows))
    assert [(r.status, r.detail) for r in results] == [("pass", ""), ("pass", ""), ("pass", "sum_based")]
    assert results[-1].line_item_ids == ["r4", "r8", "r9"]
```

The 3a test that expects a total over section totals to be `skipped` with `subtotal_scope_unknown` now describes behaviour this task replaces: change its expectation to `pass` with `sum_based`, and keep its name truthful.

Append to `packages/ingest/tests/test_structure_golden.py`:

```python
def test_almarai_balance_sheet_totals_are_all_checked(golden: Callable[[str], Path]) -> None:
    for name in ("almarai-2025-en-annualreport.pdf", "almarai-2025-ar-annualreport.pdf"):
        pdf = golden(name)
        statements(golden, name)
        checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
        balance = [c for c in checks if "-balance-1:" in c["id"] and c["kind"] == "subtotal"]
        assert balance and {c["status"] for c in balance} == {"pass"}


def test_edita_ifrs_total_equity_passes(golden: Callable[[str], Path]) -> None:
    pdf = golden("edita-2025-en-consolidated-ifrs.pdf")
    balance = statements(golden, "edita-2025-en-consolidated-ifrs.pdf")[StatementType.BALANCE]
    assert "subtotal_failed" not in balance.flags
    checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
    equity = [c for c in checks if c["id"].startswith(f"{balance.id}:subtotal:p8-t0-r26:")]
    assert [c["status"] for c in equity] == ["pass", "pass"]
```

(add `import json` to that file.)

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_sum_hierarchy.py packages/ingest/tests/test_table_checks.py -q`
Expected: FAIL (`ModuleNotFoundError: fra_ingest.sum_hierarchy`; the two new checks tests fail on parents and `sum_based`).

- [ ] **Step 4: Implement `sum_hierarchy.py`**

```python
"""Which rows each total covers, found by the sums themselves (spec 12, Data flow step 10).

Rows are read top to bottom into a list of open rows. A row with values closes the shortest
suffix of that list that sums to it, and takes its place. A heading marks where a run starts
and removes nothing, so a total over section totals needs no special case. A total that no
suffix explains is judged against the run above it, as Part 3a did.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fra_core.schemas import LineItem, Statement

HALF = Decimal("0.5")
_Fit = Literal["fit", "blank", "clash", "open"]


class SumOutcome(BaseModel):
    """How one total came out in one period."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["pass", "fail", "skipped"]
    addend_ids: tuple[str, ...] = ()
    expected: Decimal | None = None
    actual: Decimal | None = None
    blank_ids: tuple[str, ...] = ()
    detail: str = ""


class SumGroup(BaseModel):
    """A total and the rows it covers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_id: str
    addend_ids: tuple[str, ...]
    basis: Literal["sums", "run"]
    implicit: bool
    outcomes: dict[str, SumOutcome]

    @property
    def confirmed(self) -> bool:
        return any(o.status == "pass" for o in self.outcomes.values())


def _is_blank(item: LineItem, key: str) -> bool:
    return any(
        c.period_key == key and c.reported is None and "numbers_missing" in c.flags
        for c in item.cells
    )


def _fit(item: LineItem, addends: Sequence[LineItem], key: str) -> tuple[_Fit, SumOutcome]:
    actual = item.value_for(key)
    values = [a.value_for(key) for a in addends]
    blanks = tuple(
        a.id for a, v in zip(addends, values, strict=True) if v is None and _is_blank(a, key)
    )
    lost = sum(1 for v in values if v is None) - len(blanks)
    missing = SumOutcome(status="skipped", detail="missing_values")
    if actual is None or lost:
        return "open", missing
    present = [v for v in values if v is not None]
    expected = sum(present, Decimal(0))
    within = abs(actual - expected) <= HALF * len(present)
    if blanks and not within:
        return "open", missing
    outcome = SumOutcome(
        status="pass" if within else "fail",
        addend_ids=tuple(a.id for a in addends),
        expected=expected,
        actual=actual,
        blank_ids=blanks,
    )
    return ("blank" if blanks else "fit") if within else "clash", outcome


def _non_zero(addends: Sequence[LineItem], keys: Sequence[str]) -> int:
    return sum(1 for a in addends if any(a.value_for(k) for k in keys))


def _closing_suffix(
    item: LineItem, open_rows: Sequence[LineItem], keys: Sequence[str]
) -> tuple[int, dict[str, SumOutcome]] | None:
    """The length of the suffix of ``open_rows`` that ``item`` closes, with its outcomes.

    Shortest first. Every candidate fits in at least one period with every value present. A
    row with a total cue takes a suffix that clashes in no period, else the one that fits the
    most periods. A row without one must fit in every period."""
    best: tuple[int, int, dict[str, SumOutcome]] | None = None
    for length in range(2, len(open_rows) + 1):
        addends = open_rows[-length:]
        if _non_zero(addends, keys) < 2:
            continue
        fits = {key: _fit(item, addends, key) for key in keys}
        kinds = [kind for kind, _ in fits.values()]
        complete = kinds.count("fit")
        if not complete:
            continue
        outcomes = {key: outcome for key, (_, outcome) in fits.items()}
        if "clash" not in kinds and (item.is_subtotal or "open" not in kinds):
            return length, outcomes
        if item.is_subtotal and "clash" in kinds and (best is None or complete > best[0]):
            best = (complete, length, outcomes)
    return (best[1], best[2]) if best is not None else None


def _run_outcomes(
    item: LineItem,
    run: Sequence[LineItem],
    previous: LineItem | None,
    after_heading: bool,
    keys: Sequence[str],
) -> dict[str, SumOutcome]:
    """Part 3a's reading: the run since the last heading or total, alone or with the total
    before it."""
    outcomes: dict[str, SumOutcome] = {}
    for key in keys:
        if len(run) < 2:
            outcomes[key] = SumOutcome(status="skipped", detail="subtotal_scope_unknown")
            continue
        actual = item.value_for(key)
        values = [r.value_for(key) for r in run]
        if actual is None or any(v is None for v in values):
            outcomes[key] = SumOutcome(status="skipped", detail="missing_values")
            continue
        plain = sum((v for v in values if v is not None), Decimal(0))
        ids = tuple(r.id for r in run)
        candidates = [(plain, ids)]
        prior = previous.value_for(key) if previous is not None else None
        if previous is not None and prior is not None:
            candidates.append((plain + prior, (previous.id, *ids)))
        expected, addend_ids = min(candidates, key=lambda c: abs(actual - c[0]))
        within = abs(actual - expected) <= HALF * len(addend_ids)
        status: Literal["pass", "fail", "skipped"] = "pass" if within else "fail"
        detail = ""
        if not within and after_heading:
            # The heading may have cut rows the total covers.
            status, detail = "skipped", "subtotal_scope_uncertain"
        outcomes[key] = SumOutcome(
            status=status, addend_ids=addend_ids, expected=expected, actual=actual, detail=detail
        )
    return outcomes


def infer_sums(statement: Statement) -> list[SumGroup]:
    keys = [p.key for p in statement.periods]
    groups: list[SumGroup] = []
    open_rows: list[LineItem] = []
    run_start = 0
    previous: LineItem | None = None
    after_heading = False
    for item in statement.line_items:
        if item.cells and all("per_share" in c.flags for c in item.cells):
            continue
        if not item.cells:
            after_heading = after_heading or len(open_rows) > run_start
            run_start = len(open_rows)
            continue
        run = open_rows[run_start:]
        closing = _closing_suffix(item, open_rows, keys)
        if closing is None and not item.is_subtotal:
            open_rows.append(item)
            continue
        if closing is not None:
            length, outcomes = closing
            addends = open_rows[-length:]
            ids = tuple(a.id for a in addends)
            run_ids = tuple(r.id for r in run)
            with_previous = (previous.id, *run_ids) if previous is not None else None
            basis: Literal["sums", "run"] = "run" if ids in (run_ids, with_previous) else "sums"
            if not item.is_subtotal and length <= len(run):
                basis = "run"
            groups.append(
                SumGroup(
                    total_id=item.id,
                    addend_ids=ids,
                    basis=basis,
                    implicit=not item.is_subtotal,
                    outcomes=outcomes,
                )
            )
            del open_rows[-length:]
        else:
            groups.append(
                SumGroup(
                    total_id=item.id,
                    addend_ids=tuple(r.id for r in run),
                    basis="run",
                    implicit=False,
                    outcomes=_run_outcomes(item, run, previous, after_heading, keys),
                )
            )
            del open_rows[run_start:]
        open_rows.append(item)
        if item.is_subtotal:
            run_start, previous, after_heading = len(open_rows), item, False
        else:
            # An implicit total stands in the run for the rows it sums.
            run_start = min(run_start, len(open_rows) - 1)
    return groups
```

- [ ] **Step 5: Build the subtotal checks from the groups**

In `table_checks.py`, replace `_suffix_sum`, `_implicit_subtotal` and the body of `check_subtotals` with:

```python
def _sum_results(statement: Statement, group: SumGroup) -> list[CheckResult]:
    results = []
    for key, outcome in group.outcomes.items():
        expected, actual = outcome.expected, outcome.actual
        judged = expected is not None and actual is not None
        details = [
            outcome.detail,
            "implicit_subtotal" if group.implicit else "",
            "sum_based" if group.basis == "sums" and not group.implicit and judged else "",
            "blank_as_zero" if outcome.blank_ids else "",
        ]
        addends = len(outcome.addend_ids) - len(outcome.blank_ids)
        results.append(
            _result(
                statement,
                "subtotal",
                key,
                group.total_id,
                outcome.status,
                expected=expected,
                actual=actual,
                difference=actual - expected if actual is not None and expected is not None else None,
                tolerance=_HALF * addends if judged else None,
                line_item_ids=[*outcome.addend_ids, group.total_id],
                detail="; ".join(d for d in details if d),
            )
        )
    return results


def check_subtotals(statement: Statement) -> list[CheckResult]:
    return [r for group in infer_sums(statement) for r in _sum_results(statement, group)]


def _apply_sums(statement: Statement, groups: Sequence[SumGroup]) -> Statement:
    """Parents from the sums, implicit totals marked, blanks the sums confirmed flagged."""
    parents: dict[str, str] = {}
    implicit: set[str] = set()
    blanks: set[tuple[str, str]] = set()
    for group in groups:
        if not group.confirmed:
            continue
        if group.implicit:
            implicit.add(group.total_id)
        for key, outcome in group.outcomes.items():
            if outcome.status != "pass":
                continue
            for addend in outcome.addend_ids:
                parents.setdefault(addend, group.total_id)
            blanks.update((addend, key) for addend in outcome.blank_ids)
    items = []
    for item in statement.line_items:
        cells = [
            c.model_copy(update={"flags": [*c.flags, "blank_confirmed"]})
            if (item.id, c.period_key) in blanks
            else c
            for c in item.cells
        ]
        items.append(
            item.model_copy(
                update={
                    "cells": cells,
                    "parent_id": parents.get(item.id, item.parent_id),
                    "is_subtotal": item.is_subtotal or item.id in implicit,
                }
            )
        )
    return statement.model_copy(update={"line_items": items})
```

`run_checks` becomes:

```python
def run_checks(statement: Statement, index: LabelIndex) -> tuple[Statement, list[CheckResult]]:
    groups = infer_sums(statement)
    results = [r for group in groups for r in _sum_results(statement, group)]
    statement = _apply_sums(statement, groups)
    results += check_identity(statement, index)
    flags = list(statement.flags)
    ...  # the three flag rules, unchanged
```

Update the module docstring to name spec 12 for the subtotal scope, the docstring of `hierarchy.py` ("Sum-based inference ... is Part 3b" becomes "Parents from sums are set by `table_checks.run_checks` (spec 12)"), and set `STRUCTURE_VERSION = "4"` in `structure.py`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_sum_hierarchy.py packages/ingest/tests/test_table_checks.py packages/ingest/tests/test_structure.py -q`
Expected: PASS.

- [ ] **Step 7: Compare the golden output**

Run `make eval-structure`, then diff each document against the snapshot of Step 1. Intended: totals that were `subtotal_scope_unknown` or `subtotal_scope_uncertain` now pass, fail or stay skipped by the rules above; Edita IFRS total equity passes; `parent_id`, `is_subtotal` on implicit totals and `blank_confirmed` change; no `reported`, `raw_text`, period, scale or currency changes anywhere. Run `uv run pytest -m golden packages/ingest/tests/test_structure_golden.py -q` and expect PASS.

- [ ] **Step 8: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add packages/ingest/src/fra_ingest/sum_hierarchy.py packages/ingest/src/fra_ingest/table_checks.py packages/ingest/src/fra_ingest/structure.py packages/ingest/src/fra_ingest/hierarchy.py packages/ingest/tests/test_sum_hierarchy.py packages/ingest/tests/test_table_checks.py packages/ingest/tests/test_structure_golden.py
git commit -m "Find each total's rows by the sums, across headings and blanks"
```

---

### Task 2: Row-alignment repair

**Files:**
- Create: `packages/ingest/src/fra_ingest/row_alignment.py`
- Modify: `packages/ingest/src/fra_ingest/table_grid.py` (`GridCell.source_row`), `packages/ingest/src/fra_ingest/parts.py` (`build_part`), `packages/ingest/src/fra_ingest/structure.py`
- Test: `packages/ingest/tests/test_row_alignment.py`, `packages/ingest/tests/test_parts.py`, `packages/ingest/tests/test_structure_golden.py`

**Interfaces:**
- Consumes: `Grid`, `GridCell`, `HeaderLayout` (`label_col`, `value_cols`, `data_rows(grid)`).
- Produces: `realign_rows(grid: Grid, layout: HeaderLayout) -> Realignment`; `Realignment(grid: Grid, moved: list[tuple[int, int, int]], unresolved: list[int], merged_labels: list[int])`, `moved` as `(col, from_row, to_row)`; `GridCell.source_row: int | None = None`; `build_part(..., merged_rows: Collection[int] = ())`. Cell flags `row_realigned`, `row_misaligned`, `label_merged`; statement flags `rows_realigned`, `row_alignment_unresolved`.

- [ ] **Step 1: Snapshot the golden output** (as Task 1, Step 1)

- [ ] **Step 2: Write the failing tests**

`packages/ingest/tests/test_row_alignment.py`:

```python
"""Values put back on their label's line by geometry (spec 12, Data flow step 6a)."""

from fra_core.schemas import BBox, StatementType
from fra_ingest.header import HeaderLayout, parse_header
from fra_ingest.row_alignment import realign_rows
from fra_ingest.table_grid import Grid, GridCell

LINE = 12.0  # one printed line; value boxes are 8 high


def cell(text: str, row: int, col: int, line: float, lines: float = 1) -> GridCell:
    """A cell whose box starts at printed line ``line``; a label may span several lines."""
    top = 100 + LINE * line
    height = LINE * lines - 2 if col == 0 else 8
    return GridCell(
        text=text,
        row=row,
        col=col,
        bbox=BBox(left=100 * col + 10, top=top, right=100 * col + 90, bottom=top + height),
        page_no=5,
    )


def grid(cells: list[GridCell], rows: int) -> Grid:
    header = [
        GridCell(text=t, row=0, col=c, bbox=BBox(left=100 * c + 10, top=80, right=100 * c + 90, bottom=90), page_no=5, is_column_header=True)
        for c, t in ((1, "31 December 2024"), (2, "31 December 2023"))
    ]
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p4-9.json",
        page_no=5,
        page_width=600,
        num_rows=rows,
        num_cols=3,
        cells=tuple([*header, *cells]),
    )


def aligned() -> Grid:
    return grid(
        [
            cell("Share capital", 1, 0, 0), cell("140", 1, 1, 0), cell("140", 1, 2, 0),
            cell("Retained earnings", 2, 0, 1), cell("4,085", 2, 1, 1), cell("3,244", 2, 2, 1),
            cell("Total equity", 3, 0, 2), cell("4,225", 3, 1, 2), cell("3,384", 3, 2, 2),
        ],
        rows=4,
    )


def layout(g: Grid) -> HeaderLayout:
    return parse_header(g, StatementType.BALANCE, None)


def test_an_aligned_grid_is_returned_unchanged() -> None:
    g = aligned()
    result = realign_rows(g, layout(g))
    assert result.grid == g and not result.moved and not result.unresolved


def shifted() -> Grid:
    """Edita 2024 AR, equity: the first label cell holds two printed lines, the values of the
    third line sit on an unlabelled row, and the third label holds the second line's values."""
    return grid(
        [
            cell("Equity attributable Non-controlling interests", 1, 0, 0, lines=2),
            cell("4,055", 1, 1, 0), cell("3,373", 1, 2, 0),
            cell("4,157", 2, 1, 2), cell("3,447", 2, 2, 2),
            cell("Total equity", 3, 0, 2), cell("102", 3, 1, 1), cell("74", 3, 2, 1),
        ],
        rows=4,
    )


def test_displaced_groups_move_to_the_label_line_that_contains_them() -> None:
    g = shifted()
    result = realign_rows(g, layout(g))
    fixed = result.grid
    assert [fixed.text(3, 1), fixed.text(3, 2)] == ["4,157", "3,447"]
    assert [fixed.text(2, 1), fixed.text(2, 2)] == ["102", "74"]
    assert [fixed.text(1, 1), fixed.text(1, 2)] == ["4,055", "3,373"]
    moved = fixed.cell(3, 1)
    assert moved is not None and moved.source_row == 2 and "row_realigned" in moved.flags
    assert sorted(result.moved) == [(1, 2, 3), (1, 3, 2), (2, 2, 3), (2, 3, 2)]
    assert result.merged_labels == [1] and not result.unresolved


def test_a_heading_cell_gives_its_values_to_the_label_below() -> None:
    g = grid(
        [
            cell("Liabilities Non-current liabilities", 1, 0, 0, lines=2),
            cell("2,282", 1, 1, 2), cell("1,129", 1, 2, 2),
            cell("Borrowings", 2, 0, 2), cell("19", 2, 1, 3), cell("17", 2, 2, 3),
            cell("Government grants", 3, 0, 3),
            cell("Employee benefits", 4, 0, 4), cell("75", 4, 1, 4), cell("55", 4, 2, 4),
        ],
        rows=5,
    )
    fixed = realign_rows(g, layout(g)).grid
    assert fixed.text(1, 1) == "" and fixed.text(2, 1) == "2,282" and fixed.text(3, 1) == "19"
    assert fixed.text(4, 1) == "75"


def test_a_wrapped_label_keeps_its_values_on_either_line() -> None:
    for value_line in (0, 1):
        g = grid(
            [
                cell("Financial assets at fair value through profit", 1, 0, 0, lines=2),
                cell("578", 1, 1, value_line), cell("0", 1, 2, value_line),
                cell("Cash", 2, 0, 2), cell("716", 2, 1, 2), cell("518", 2, 2, 2),
            ],
            rows=3,
        )
        result = realign_rows(g, layout(g))
        assert not result.moved and not result.unresolved and result.merged_labels == [1]


def test_a_group_on_no_label_line_is_left_and_flagged() -> None:
    g = grid(
        [
            cell("Share capital", 1, 0, 0), cell("140", 1, 1, 0), cell("140", 1, 2, 0),
            cell("Reserves", 2, 0, 1), cell("72", 2, 1, 5), cell("72", 2, 2, 5),
        ],
        rows=3,
    )
    result = realign_rows(g, layout(g))
    assert result.unresolved == [2] and not result.moved
    left = result.grid.cell(2, 1)
    assert left is not None and left.text == "72" and "row_misaligned" in left.flags


def test_a_mirrored_table_is_repaired_the_same_way() -> None:
    g = shifted()
    mirrored = g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(
                    update={
                        "col": 2 - c.col,
                        "bbox": BBox(left=500 - c.bbox.right, top=c.bbox.top, right=500 - c.bbox.left, bottom=c.bbox.bottom),
                    }
                )
                for c in g.cells
                if c.bbox is not None
            )
        }
    )
    fixed = realign_rows(mirrored, layout(mirrored)).grid
    assert [fixed.text(3, 0), fixed.text(3, 1)] == ["3,447", "4,157"]
```

Append to `packages/ingest/tests/test_parts.py`:

```python
def test_a_moved_cell_keeps_its_docling_row_and_merged_labels_are_flagged() -> None:
    g = grid(ROWS, pads={2: 10, 3: 10, 4: 10})
    cells = tuple(
        c.model_copy(update={"source_row": 9, "flags": ("row_realigned",)})
        if (c.row, c.col) == (2, 2)
        else c
        for c in g.cells
    )
    g = g.model_copy(update={"cells": cells})
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    p = build_part(g, layout, classification, source=TextSource.TEXT, merged_rows=[3])
    moved = p.line_items[1].cells[0]
    assert moved.provenance.row == 9 and "row_realigned" in moved.flags
    assert all("label_merged" in c.flags for c in p.line_items[2].cells)
```

Append to `packages/ingest/tests/test_structure_golden.py`:

```python
def test_edita_2024_ar_equity_and_borrowings_sit_on_their_labels(
    golden: Callable[[str], Path],
) -> None:
    balance = statements(golden, "edita-2024-ar-consolidated-eas.pdf")[StatementType.BALANCE]
    rows = {i.id: i for i in balance.line_items}
    assert "rows_realigned" in balance.flags
    assert rows["p5-t0-r22"].value_for("2024-12-31") == Decimal("4157569146")
    assert rows["p5-t0-r21"].value_for("2024-12-31") == Decimal("102084427")
    assert not rows["p5-t0-r23"].cells
    assert rows["p5-t0-r24"].value_for("2024-12-31") == Decimal("2282057066")
    assert rows["p5-t0-r25"].value_for("2024-12-31") == Decimal("19343101")
    moved = next(c for c in rows["p5-t0-r22"].cells if c.period_key == "2024-12-31")
    assert moved.provenance.row == 21 and "row_realigned" in moved.flags
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_row_alignment.py packages/ingest/tests/test_parts.py -q`
Expected: FAIL (`ModuleNotFoundError: fra_ingest.row_alignment`; `source_row` unknown).

- [ ] **Step 4: Implement**

`table_grid.py`: add to `GridCell` after `page_no`:

```python
    source_row: int | None = None  # the docling row of a cell that row alignment moved
```

`row_alignment.py`:

```python
"""Values put back on their label's line by geometry (spec 12, Data flow step 6a).

OCR tables can come out with a row's values one row away from its label. The grid says which
row a cell is in; the cell's box says where it sits on the page. Where the two disagree, the
box is right: a group of values whose vertical centre lies outside its own label's top and
bottom is moved to the row whose label line contains it. A repair moves cells and never edits
them, and anything that cannot be placed is left where it was and flagged.
"""

from __future__ import annotations

from itertools import pairwise
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from fra_ingest.header import HeaderLayout
from fra_ingest.table_grid import Grid, GridCell

_SLACK = 0.25  # of the median value-cell height
_TALL = 1.7  # a label cell this many value heights tall holds more than one printed line


class Realignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grid: Grid
    moved: list[tuple[int, int, int]] = Field(default_factory=list)  # (col, from_row, to_row)
    unresolved: list[int] = Field(default_factory=list)
    merged_labels: list[int] = Field(default_factory=list)


def _centre(cell: GridCell) -> float:
    assert cell.bbox is not None
    return (cell.bbox.top + cell.bbox.bottom) / 2


def realign_rows(grid: Grid, layout: HeaderLayout) -> Realignment:
    rows = layout.data_rows(grid)
    groups: dict[int, list[GridCell]] = {}
    for cell in grid.cells:
        if cell.row in rows and cell.col in layout.value_cols and cell.text and cell.bbox:
            groups.setdefault(cell.row, []).append(cell)
    if not groups or layout.label_col is None:
        return Realignment(grid=grid)
    height = median(c.bbox.height for cells in groups.values() for c in cells if c.bbox)
    slack = _SLACK * height
    bands: dict[int, tuple[float, float]] = {}
    for cell in grid.cells:
        if cell.row in rows and cell.col == layout.label_col and cell.text and cell.bbox:
            bands[cell.row] = (cell.bbox.top, cell.bbox.bottom)
    merged = sorted(r for r, (top, bottom) in bands.items() if bottom - top > _TALL * height)
    at = {row: median(_centre(c) for c in cells) for row, cells in groups.items()}

    def inside(y: float, row: int) -> bool:
        top, bottom = bands[row]
        return top - slack <= y <= bottom + slack

    off = sorted(r for r in groups if r in bands and not inside(at[r], r))
    if not off:
        return Realignment(grid=grid, merged_labels=merged)

    # Where each row's group ends up: start from the grid, then vacate the rows that are off.
    holds: dict[int, int | None] = {r: (r if r in groups else None) for r in rows}
    for row in off:
        holds[row] = None
    # An unlabelled group that sits on the label line of a vacated row belongs to that row.
    vacated_unlabelled: list[int] = []
    for row in sorted(r for r in groups if r not in bands):
        target = next((t for t in off if holds[t] is None and inside(at[row], t)), None)
        if target is not None:
            holds[target], holds[row] = row, None
            vacated_unlabelled.append(row)
    unresolved: list[int] = []
    for row in sorted(off, key=lambda r: at[r]):
        targets = sorted(
            (t for t in bands if t != row and inside(at[row], t)),
            key=lambda t: abs(at[row] - sum(bands[t]) / 2),
        )
        free = next((t for t in targets if holds[t] is None), None)
        if free is None and targets:
            # The label line already keeps a group: a cell holding two printed lines. The
            # lower group takes the unlabelled row that was vacated below it.
            free = next((u for u in vacated_unlabelled if u > targets[0] and holds[u] is None), None)
        if free is None:
            unresolved.append(row)
            continue
        holds[free] = row
    for row in unresolved:
        if holds[row] is None:
            holds[row] = row
    placed = [(row, at[source]) for row, source in sorted(holds.items()) if source is not None]
    in_order = all(a[1] <= b[1] + slack for a, b in pairwise(placed))
    lost = set(groups) - {s for s in holds.values() if s is not None}
    if not in_order or lost:
        # A repair that would reorder or drop figures is no repair: leave the grid, flag it.
        holds = {r: (r if r in groups else None) for r in rows}
        unresolved = off

    target_of = {source: row for row, source in holds.items() if source is not None}
    moved: list[tuple[int, int, int]] = []
    cells: list[GridCell] = []
    for cell in grid.cells:
        if cell.row in groups and cell in groups[cell.row]:
            target = target_of[cell.row]
            if target != cell.row:
                moved.append((cell.col, cell.row, target))
                cell = cell.model_copy(
                    update={"row": target, "source_row": cell.row, "flags": (*cell.flags, "row_realigned")}
                )
            elif cell.row in unresolved:
                cell = cell.model_copy(update={"flags": (*cell.flags, "row_misaligned")})
        cells.append(cell)
    return Realignment(
        grid=grid.model_copy(update={"cells": tuple(cells)}),
        moved=sorted(moved),
        unresolved=sorted(unresolved),
        merged_labels=merged,
    )
```

`parts.py`, `build_part`: add the keyword `merged_rows: Collection[int] = ()`; where the cell's flags are built add, after `flags.extend(grid_cell.flags)`'s branch:

```python
            if row in merged_rows:
                flags.append("label_merged")
```

and build the provenance row from the cell's source:

```python
                        row=grid_cell.source_row
                        if grid_cell is not None and grid_cell.source_row is not None
                        else row,
```

`structure.py`, in `structure_document`, where the part is built:

```python
        realigned = realign_rows(grid, layout)
        part = build_part(
            realigned.grid,
            layout,
            result,
            source=source,
            index=index,
            merged_rows=realigned.merged_labels,
        )
        if realigned.moved:
            part.flags.append("rows_realigned")
        if realigned.unresolved:
            part.flags.append("row_alignment_unresolved")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_row_alignment.py packages/ingest/tests/test_parts.py packages/ingest/tests/test_structure.py -q`
Expected: PASS.

- [ ] **Step 6: Compare the golden output**

Run `make eval-structure` and diff against the snapshot. Intended: on edita-2024-ar-consolidated p5, rows `r21` to `r25` as in the golden test, with the checks that follow from them; `label_merged` on cells of rows with tall labels in the scanned documents and Almarai AR; nothing else. Run the golden tests and expect PASS.

- [ ] **Step 7: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add packages/ingest/src/fra_ingest/row_alignment.py packages/ingest/src/fra_ingest/table_grid.py packages/ingest/src/fra_ingest/parts.py packages/ingest/src/fra_ingest/structure.py packages/ingest/tests/test_row_alignment.py packages/ingest/tests/test_parts.py packages/ingest/tests/test_structure_golden.py
git commit -m "Move values back onto their label's line where the boxes show a shifted row"
```

---

### Task 3: Figure checks

**Files:**
- Create: `packages/ingest/src/fra_ingest/figure_checks.py`
- Modify: `packages/core/src/fra_core/schemas/check.py` (`kind`), `packages/ingest/src/fra_ingest/table_checks.py` (`run_checks`), `packages/ingest/src/fra_ingest/structure.py` (the tie, after every statement is built)
- Test: `packages/ingest/tests/test_figure_checks.py`, `packages/core/tests/test_schemas.py` (or the file that tests `CheckResult`), `packages/ingest/tests/test_structure_golden.py`

**Interfaces:**
- Consumes: `CheckResult`, `Statement`; the subtotal results' `line_item_ids` end with the total, the identity's start with total assets.
- Produces: `single_digit_place(difference: Decimal) -> int | None`; `one_digit_apart(a: Decimal, b: Decimal) -> bool`; `diagnose_digits(statement: Statement, checks: list[CheckResult]) -> tuple[Statement, list[CheckResult]]`; `flag_period_outliers(statement: Statement) -> Statement`; `check_net_profit_tie(income: Statement, comprehensive: Statement) -> list[CheckResult]`. `CheckResult.kind` is `Literal["subtotal", "balance_identity", "net_profit_tie"]`. Cell flags `digit_suspect`, `period_outlier`; statement flag `tie_failed`; details `single_digit:10^k` and `suspect:<item id>=<figure>`.

- [ ] **Step 1: Snapshot the golden output** (as Task 1, Step 1)

- [ ] **Step 2: Write the failing tests**

`packages/ingest/tests/test_figure_checks.py`:

```python
"""Misreads the sums can point at or that no sum covers (spec 12, Data flow step 10)."""

from datetime import date
from decimal import Decimal

import pytest

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.figure_checks import (
    check_net_profit_tie,
    flag_period_outliers,
    one_digit_apart,
    single_digit_place,
)
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_checks import run_checks

INDEX = LabelIndex(load_taxonomy())
P1 = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
P2 = Period(key="2023-12-31", end_date=date(2023, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, label: str, *values: str, total: bool = False, flags: tuple[str, ...] = ()) -> LineItem:
    cells = [
        Cell(
            period_key=period.key,
            reported=Decimal(value),
            raw_text=value,
            provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
            flags=list(flags),
        )
        for period, value in zip((P1, P2), values, strict=False)
    ]
    return LineItem(id=f"r{n}", raw_label=label, is_subtotal=total, cells=cells)


def statement(items: list[LineItem], kind: StatementType = StatementType.BALANCE, sid: str = "s") -> Statement:
    return Statement(
        id=sid,
        document_sha256="a" * 64,
        type=kind,
        currency="EGP",
        scale=1,
        periods=[P1, P2],
        line_items=items,
    )


@pytest.mark.parametrize(
    ("difference", "place"),
    [("5", 0), ("-800000", 5), ("5000000", 6), ("12", None), ("0", None), ("0.5", None)],
)
def test_single_digit_place(difference: str, place: int | None) -> None:
    assert single_digit_place(Decimal(difference)) == place


def test_one_digit_apart_needs_the_same_length_and_sign() -> None:
    assert one_digit_apart(Decimal("7743342656"), Decimal("7743342651"))
    assert not one_digit_apart(Decimal("95"), Decimal("105"))
    assert not one_digit_apart(Decimal("-5"), Decimal("5"))
    assert not one_digit_apart(Decimal("120"), Decimal("210"))


ASSETS = [
    item(1, "Property", "4491", "3371"),
    item(2, "Goodwill", "126", "81"),
    item(3, "Total non-current assets", "4617", "3452", total=True),
    item(4, "Inventories", "3034", "1866"),
    item(5, "Cash", "518", "1003"),
    item(6, "Total current assets", "3552", "2869", total=True),
]


def test_the_only_cell_one_digit_can_settle_is_the_suspect() -> None:
    rows = [*ASSETS, item(7, "Total assets", "8169", "6326", total=True)]
    checked, results = run_checks(statement(rows), INDEX)
    failed = next(r for r in results if r.status == "fail")
    assert failed.difference == Decimal("5")
    assert failed.detail == "sum_based; single_digit:10^0; suspect:r7=6321"
    flagged = [(i.id, c.period_key) for i in checked.line_items for c in i.cells if "digit_suspect" in c.flags]
    assert flagged == [("r7", P2.key)]
    assert checked.line_items[6].value_for(P2.key) == Decimal("6326")


def test_several_cells_that_could_carry_the_digit_are_all_suspect() -> None:
    rows = [
        item(1, "Property", "4491", "3371"),
        item(2, "Right of use", "292", "122"),
        item(3, "Goodwill", "126", "81"),
        item(4, "Total non-current assets", "4929", "3574", total=True),
    ]
    checked, results = run_checks(statement(rows), INDEX)
    failed = next(r for r in results if r.status == "fail")
    assert failed.detail == "single_digit:10^1"
    flagged = {i.id for i in checked.line_items for c in i.cells if "digit_suspect" in c.flags}
    # 4491 + 20 and 292 + 20 carry into a second digit, so neither is a candidate.
    assert flagged == {"r3", "r4"}


def test_a_difference_that_is_not_one_digit_names_nothing() -> None:
    rows = [*ASSETS, item(7, "Total assets", "8169", "6399", total=True)]
    checked, results = run_checks(statement(rows), INDEX)
    assert next(r for r in results if r.status == "fail").detail == "sum_based"
    assert not any("digit_suspect" in c.flags for i in checked.line_items for c in i.cells)


def test_a_row_whose_periods_differ_a_thousand_times_is_an_outlier() -> None:
    rows = [
        item(1, "Share capital", "140002731", "2731"),
        item(2, "Legal reserve", "999000", "1000"),
        item(3, "Retained earnings", "1000000", "1000"),
        item(4, "Earnings per share", "5000", "2", flags=("per_share",)),
    ]
    flagged = flag_period_outliers(statement(rows))
    outliers = [i.id for i in flagged.line_items if any("period_outlier" in c.flags for c in i.cells)]
    assert outliers == ["r1", "r3"]


INCOME = statement(
    [item(1, "Revenue", "900", "800"), item(2, "Net profit for the year", "160", "140")],
    StatementType.INCOME,
    "inc",
)


def test_net_profit_ties_between_the_two_statements() -> None:
    comprehensive = statement(
        [item(1, "Net profit for the year", "160", "140"), item(2, "Translation", "-5", "3")],
        StatementType.COMPREHENSIVE_INCOME,
        "ci",
    )
    results = check_net_profit_tie(INCOME, comprehensive)
    assert [(r.kind, r.status, r.statement_id) for r in results] == [("net_profit_tie", "pass", "ci")] * 2
    assert results[0].line_item_ids == ["r2", "r1"]


def test_net_profit_that_equals_no_income_row_fails_the_tie() -> None:
    comprehensive = statement(
        [item(1, "Net profit for the year", "160", "141")], StatementType.COMPREHENSIVE_INCOME, "ci"
    )
    results = check_net_profit_tie(INCOME, comprehensive)
    assert [r.status for r in results] == ["fail", "fail"]
    assert results[0].detail == "no_income_row_equal"


def test_no_shared_period_means_no_tie_check() -> None:
    other = Period(key="FY2020", end_date=date(2020, 12, 31), kind=PeriodKind.DURATION, months=12)
    comprehensive = statement(
        [item(1, "Net profit", "160", "140")], StatementType.COMPREHENSIVE_INCOME, "ci"
    ).model_copy(update={"periods": [other], "line_items": []})
    assert check_net_profit_tie(INCOME, comprehensive) == []
```

Append to `packages/ingest/tests/test_structure_golden.py`:

```python
def test_edita_2024_ar_total_assets_is_the_named_suspect(golden: Callable[[str], Path]) -> None:
    pdf = golden("edita-2024-ar-consolidated-eas.pdf")
    balance = statements(golden, "edita-2024-ar-consolidated-eas.pdf")[StatementType.BALANCE]
    total = next(i for i in balance.line_items if i.id == "p5-t0-r14")
    suspect = next(c for c in total.cells if c.period_key == "2023-12-31")
    assert suspect.reported == Decimal("7743342656") and "digit_suspect" in suspect.flags
    checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
    identity = next(
        c for c in checks if c["kind"] == "balance_identity" and c["period_key"] == "2023-12-31"
    )
    assert identity["status"] == "fail"
    assert identity["detail"].endswith("single_digit:10^0; suspect:p5-t0-r14=7743342651")


def test_almarai_net_profit_ties(golden: Callable[[str], Path]) -> None:
    pdf = golden("almarai-2025-en-annualreport.pdf")
    statements(golden, "almarai-2025-en-annualreport.pdf")
    checks = json.loads((artifact_dir(pdf) / "table_checks.json").read_text(encoding="utf-8"))
    ties = [c for c in checks if c["kind"] == "net_profit_tie"]
    assert [c["status"] for c in ties] == ["pass", "pass"]
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_figure_checks.py -q`
Expected: FAIL (`ModuleNotFoundError: fra_ingest.figure_checks`).

- [ ] **Step 4: Implement**

`check.py`: `kind: Literal["subtotal", "balance_identity", "net_profit_tie"]`, and the module docstring gains "or a tie between statements".

`figure_checks.py`:

```python
"""Misreads the sums can point at, or that no sum covers (spec 12, Data flow step 10).

Nothing here changes a figure. A cell is flagged, and a check's detail says what would settle
it; the read value stays as it was read.
"""

from __future__ import annotations

from decimal import Decimal

from fra_core.schemas import Cell, CheckResult, LineItem, Statement

OUTLIER_RATIO = 1000
_HALF = Decimal("0.5")
_NOT_COMPARED = {"per_share", "percent", "implausible_magnitude"}


def single_digit_place(difference: Decimal) -> int | None:
    """k when the difference is one digit times 10^k, else None."""
    magnitude = abs(difference)
    if magnitude == 0 or magnitude != magnitude.to_integral_value():
        return None
    digits = str(int(magnitude))
    return len(digits) - 1 if len(digits.rstrip("0")) == 1 else None


def one_digit_apart(a: Decimal, b: Decimal) -> bool:
    """Two whole figures of the same sign and length that differ in exactly one digit."""
    if a == b or (a < 0) != (b < 0):
        return False
    if a != a.to_integral_value() or b != b.to_integral_value():
        return False
    x, y = str(abs(int(a))), str(abs(int(b)))
    return len(x) == len(y) and sum(1 for p, q in zip(x, y, strict=True) if p != q) == 1


def _total_id(check: CheckResult) -> str:
    return check.line_item_ids[0] if check.kind == "balance_identity" else check.line_item_ids[-1]


def _with_flag(statement: Statement, cells: set[tuple[str, str]], flag: str) -> Statement:
    if not cells:
        return statement
    items = [
        item.model_copy(
            update={
                "cells": [
                    c.model_copy(update={"flags": [*c.flags, flag]})
                    if (item.id, c.period_key) in cells and flag not in c.flags
                    else c
                    for c in item.cells
                ]
            }
        )
        for item in statement.line_items
    ]
    return statement.model_copy(update={"line_items": items})


def diagnose_digits(
    statement: Statement, checks: list[CheckResult]
) -> tuple[Statement, list[CheckResult]]:
    """Name the cells where one misread digit explains a failed check."""
    by_id = {item.id: item for item in statement.line_items}
    vouched = {(i, c.period_key) for c in checks if c.status == "pass" for i in c.line_item_ids}
    candidates: dict[str, dict[str, Decimal]] = {}
    places: dict[str, int] = {}
    for check in checks:
        if check.status != "fail" or check.difference is None:
            continue
        place = single_digit_place(check.difference)
        if place is None:
            continue
        places[check.id] = place
        total = _total_id(check)
        found: dict[str, Decimal] = {}
        for item_id in check.line_item_ids:
            item = by_id.get(item_id)
            value = item.value_for(check.period_key) if item is not None else None
            if value is None or (item_id, check.period_key) in vouched:
                continue
            settled = value - check.difference if item_id == total else value + check.difference
            if one_digit_apart(value, settled):
                found[item_id] = settled
        candidates[check.id] = found
    period_of = {c.id: c.period_key for c in checks}
    sole = {
        (next(iter(found)), period_of[check_id])
        for check_id, found in candidates.items()
        if len(found) == 1
    }
    suspects: set[tuple[str, str]] = set()
    updated: list[CheckResult] = []
    for check in checks:
        if check.id not in places:
            updated.append(check)
            continue
        found = candidates[check.id]
        named = {i: v for i, v in found.items() if (i, check.period_key) in sole} or found
        suspects.update((i, check.period_key) for i in named)
        details = [check.detail, f"single_digit:10^{places[check.id]}"]
        if len(named) == 1:
            item_id, settled = next(iter(named.items()))
            details.append(f"suspect:{item_id}={settled}")
        updated.append(check.model_copy(update={"detail": "; ".join(d for d in details if d)}))
    return _with_flag(statement, suspects, "digit_suspect"), updated


def _compared(cell: Cell) -> bool:
    return bool(cell.reported) and not _NOT_COMPARED & set(cell.flags)


def flag_period_outliers(statement: Statement) -> Statement:
    """Flag the values of a row whose largest is ``OUTLIER_RATIO`` times its smallest or more."""
    outliers: set[tuple[str, str]] = set()
    for item in statement.line_items:
        cells = [c for c in item.cells if _compared(c)]
        sizes = [abs(c.reported) for c in cells if c.reported is not None]
        if len(sizes) >= 2 and max(sizes) >= OUTLIER_RATIO * min(sizes):
            outliers.update((item.id, c.period_key) for c in cells)
    return _with_flag(statement, outliers, "period_outlier")


def _first_valued(statement: Statement) -> LineItem | None:
    return next(
        (i for i in statement.line_items if any(c.reported is not None for c in i.cells)), None
    )


def check_net_profit_tie(income: Statement, comprehensive: Statement) -> list[CheckResult]:
    """The first valued row of comprehensive income against the rows of the income statement:
    one of them must equal it in every period the two statements share."""
    head = _first_valued(comprehensive)
    shared = {p.key for p in income.periods}
    keys = [p.key for p in comprehensive.periods if p.key in shared]
    keys = [k for k in keys if head is not None and head.value_for(k) is not None]
    if head is None or not keys:
        return []

    def equal(item: LineItem) -> bool:
        return all(
            (v := item.value_for(k)) is not None and abs(v - head.value_for(k)) <= _HALF  # type: ignore[operator]
            for k in keys
        )

    match = next((i for i in income.line_items if i.cells and equal(i)), None)
    results = []
    for key in keys:
        actual = head.value_for(key)
        expected = match.value_for(key) if match is not None else None
        results.append(
            CheckResult(
                id=f"{comprehensive.id}:net_profit_tie:{head.id}:{key}",
                statement_id=comprehensive.id,
                kind="net_profit_tie",
                period_key=key,
                status="pass" if match is not None else "fail",
                expected=expected,
                actual=actual,
                difference=actual - expected if actual is not None and expected is not None else None,
                tolerance=_HALF if match is not None else None,
                line_item_ids=[match.id, head.id] if match is not None else [head.id],
                detail="" if match is not None else "no_income_row_equal",
            )
        )
    return results
```

`table_checks.run_checks`, after the identity:

```python
    statement, results = diagnose_digits(statement, results)
    statement = flag_period_outliers(statement)
```

`structure.py`, in `structure_document` after the loop that builds `statements` and `checks`:

```python
    first: dict[StatementType, int] = {}
    for position, s in enumerate(statements):
        first.setdefault(s.type, position)
    if StatementType.INCOME in first and StatementType.COMPREHENSIVE_INCOME in first:
        income, comprehensive = first[StatementType.INCOME], first[StatementType.COMPREHENSIVE_INCOME]
        ties = check_net_profit_tie(statements[income], statements[comprehensive])
        checks.extend(ties)
        if any(t.status == "fail" for t in ties):
            for position in (income, comprehensive):
                s = statements[position]
                statements[position] = s.model_copy(update={"flags": [*s.flags, "tie_failed"]})
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_figure_checks.py packages/ingest/tests/test_table_checks.py packages/ingest/tests/test_structure.py packages/core -q`
Expected: PASS.

- [ ] **Step 6: Compare the golden output**

Run `make eval-structure` and diff against the snapshot. Intended: `digit_suspect` and `period_outlier` flags, the two details on failed checks, `net_profit_tie` results on documents with both statements, `tie_failed` on Juhayna 2025 EN consolidated and standalone; no value changes. Run the golden tests and expect PASS.

- [ ] **Step 7: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add packages/core/src/fra_core/schemas/check.py packages/ingest/src/fra_ingest/figure_checks.py packages/ingest/src/fra_ingest/table_checks.py packages/ingest/src/fra_ingest/structure.py packages/ingest/tests/test_figure_checks.py packages/ingest/tests/test_structure_golden.py
git commit -m "Name the cell one misread digit explains, flag period outliers and tie net profit"
```

---

### Task 4: The held-for-review decision

**Files:**
- Create: `packages/ingest/src/fra_ingest/review.py`
- Modify: `packages/ingest/src/fra_ingest/results.py` (`StructureResult.reviews`), `packages/ingest/src/fra_ingest/structure.py`, `packages/ingest/src/fra_ingest/cli.py` (`_structure_summary`)
- Test: `packages/ingest/tests/test_review.py`, `packages/ingest/tests/test_structure_results.py`, `packages/ingest/tests/test_structure_golden.py`

**Interfaces:**
- Produces: `StatementReview(statement_id: str, status: Literal["passed", "needs_review"], reasons: list[str], warnings: list[str], numeric_cells: int, checked_cells: int, flagged_cells: int)`; `review_statement(statement: Statement, checks: Sequence[CheckResult], *, primary: bool) -> StatementReview`; `is_critical(cell: Cell) -> bool`; `CRITICAL_CELL_FLAGS`; `StructureResult.reviews: list[StatementReview]`. A held statement carries the flag `needs_review`.

- [ ] **Step 1: Snapshot the golden output** (as Task 1, Step 1)

- [ ] **Step 2: Write the failing tests**

`packages/ingest/tests/test_review.py`:

```python
"""Passed, or held for review with the reasons (spec 12, Data flow step 10a)."""

from datetime import date
from decimal import Decimal

from fra_core.schemas import (
    BBox,
    Cell,
    CheckResult,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.review import review_statement

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, value: str | None, *flags: str) -> LineItem:
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value) if value is not None else None,
        raw_text=value or "",
        provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
        flags=list(flags),
    )
    return LineItem(id=f"r{n}", raw_label=f"row {n}", cells=[cell])


def statement(items: list[LineItem], kind: StatementType = StatementType.INCOME, flags: tuple[str, ...] = ()) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1000,
        periods=[P],
        line_items=items,
        flags=list(flags),
    )


def check(kind: str, status: str, ids: list[str], detail: str = "") -> CheckResult:
    return CheckResult.model_validate(
        {"id": f"s:{kind}:{ids[-1] if ids else 'none'}:{P.key}", "statement_id": "s", "kind": kind,
         "period_key": P.key, "status": status, "line_item_ids": ids, "detail": detail}
    )


ROWS = [item(1, "10"), item(2, "5"), item(3, "15"), item(4, "7")]
PASSING = [check("subtotal", "pass", ["r1", "r2", "r3"])]


def test_a_clean_statement_passes_and_counts_what_the_checks_vouch_for() -> None:
    review = review_statement(statement(ROWS), PASSING, primary=True)
    assert review.status == "passed" and review.reasons == []
    assert (review.numeric_cells, review.checked_cells, review.flagged_cells) == (4, 3, 0)


def test_each_failed_check_is_a_reason() -> None:
    checks = [*PASSING, check("subtotal", "fail", ["r1", "r4"]), check("net_profit_tie", "fail", ["r4"])]
    review = review_statement(statement(ROWS), checks, primary=True)
    assert review.status == "needs_review"
    assert review.reasons == ["subtotal_failed", "tie_failed"]


def test_a_balance_sheet_needs_its_identity_in_every_period() -> None:
    balance = statement(ROWS, StatementType.BALANCE)
    assert review_statement(balance, PASSING, primary=True).reasons == ["identity_not_checked"]
    skipped = [*PASSING, check("balance_identity", "skipped", [], "identity_totals_not_found")]
    assert review_statement(balance, skipped, primary=True).reasons == [
        "identity_not_checked:identity_totals_not_found"
    ]
    held = [*PASSING, check("balance_identity", "fail", ["r3", "r4"])]
    assert review_statement(balance, held, primary=True).reasons == ["identity_failed"]
    ok = [*PASSING, check("balance_identity", "pass", ["r3", "r4"])]
    assert review_statement(balance, ok, primary=True).status == "passed"


def test_a_statement_no_check_vouches_for_is_held() -> None:
    assert review_statement(statement(ROWS), [], primary=True).reasons == ["unchecked"]


def test_critical_cell_flags_hold_with_their_count() -> None:
    rows = [item(1, "10", "digit_suspect"), item(2, None, "numbers_missing"), item(3, None, "unparsed"), item(4, "7", "row_realigned")]
    review = review_statement(statement(rows), PASSING, primary=True)
    assert review.reasons == ["numbers_missing:1", "unparsed:1", "digit_suspect:1", "row_realigned:1"]
    assert review.flagged_cells == 4


def test_a_confirmed_blank_holds_nothing() -> None:
    rows = [item(1, "10"), item(2, None, "numbers_missing", "blank_confirmed"), item(3, "10")]
    review = review_statement(statement(rows), PASSING, primary=True)
    assert review.status == "passed" and review.warnings == ["blank_confirmed:1"]


def test_statement_flags_hold_or_warn() -> None:
    held = review_statement(statement(ROWS, flags=("period_unbound:2", "scale_missing")), PASSING, primary=True)
    assert held.reasons == ["period_unbound:2"] and held.warnings == ["scale_missing"]
    merged = [item(1, "10", "label_merged"), *ROWS[1:]]
    assert review_statement(statement(merged), PASSING, primary=True).warnings == ["label_merged:1"]


def test_a_second_statement_of_a_type_is_held_as_a_duplicate() -> None:
    assert review_statement(statement(ROWS), PASSING, primary=False).reasons == ["duplicate_statement"]
```

Append to `packages/ingest/tests/test_structure_golden.py`:

```python
def reviews(golden: Callable[[str], Path], name: str) -> dict[str, StatementReview]:
    pdf = golden(name)
    statements(golden, name)
    stored = StructureResult.model_validate_json(
        (artifact_dir(pdf) / "statements.raw.json").read_text(encoding="utf-8")
    )
    return {r.statement_id: r for r in stored.reviews}


def test_almarai_statements_pass_review(golden: Callable[[str], Path]) -> None:
    for name in ("almarai-2025-en-annualreport.pdf", "almarai-2025-ar-annualreport.pdf"):
        found = reviews(golden, name)
        for kind in ("balance-1", "income-", "comprehensive_income-"):
            first = next(r for i, r in found.items() if kind in i and r.reasons != ["duplicate_statement"])
            assert first.status == "passed", (name, first)


def test_edita_2024_ar_balance_sheet_is_held_with_its_reasons(golden: Callable[[str], Path]) -> None:
    found = reviews(golden, "edita-2024-ar-consolidated-eas.pdf")
    review = next(r for i, r in found.items() if "-balance-" in i)
    assert review.status == "needs_review"
    assert "identity_failed" in review.reasons
    assert any(r.startswith("digit_suspect:") for r in review.reasons)
    assert any(r.startswith("row_realigned:") for r in review.reasons)
```

(import `StatementReview` from `fra_ingest.review` and `StructureResult` from `fra_ingest.results`.)

Append to `packages/ingest/tests/test_structure_results.py` a test that `StructureResult` round-trips with a `reviews` entry and defaults to an empty list.

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_review.py -q`
Expected: FAIL (`ModuleNotFoundError: fra_ingest.review`).

- [ ] **Step 4: Implement**

`review.py`:

```python
"""Passed, or held for a person to look at (spec 12, Data flow step 10a).

A statement passes when its checks hold, something vouches for its figures and nothing on it
is flagged as unsure. Passing is not proof: ``checked_cells`` says how many figures a passing
check covers, and the rest were read and nothing more.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import Cell, CheckResult, Statement, StatementType

# In the order the reasons are listed.
CRITICAL_CELL_FLAGS = (
    "unparsed",
    "implausible_magnitude",
    "digit_suspect",
    "period_outlier",
    "row_misaligned",
    "row_realigned",
    "ambiguous_separator",
)
_HOLD_FLAGS = (
    "period_unbound",
    "scale_conflict",
    "currency_conflict",
    "currency_missing",
    "row_alignment_unresolved",
)
_WARNING_FLAGS = ("scale_missing", "currency_from_domicile", "currency_inferred")
_WARNING_CELL_FLAGS = ("label_merged", "blank_confirmed")
_FAILED = (("subtotal", "subtotal_failed"), ("balance_identity", "identity_failed"), ("net_profit_tie", "tie_failed"))


class StatementReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str
    status: Literal["passed", "needs_review"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    numeric_cells: int = Field(ge=0)
    checked_cells: int = Field(ge=0)
    flagged_cells: int = Field(ge=0)


def _lost(cell: Cell) -> bool:
    return "numbers_missing" in cell.flags and "blank_confirmed" not in cell.flags


def is_critical(cell: Cell) -> bool:
    return _lost(cell) or any(f in CRITICAL_CELL_FLAGS for f in cell.flags)


def review_statement(
    statement: Statement, checks: Sequence[CheckResult], *, primary: bool
) -> StatementReview:
    cells = [(item.id, c) for item in statement.line_items for c in item.cells]
    reasons = [
        reason for kind, reason in _FAILED if any(c.kind == kind and c.status == "fail" for c in checks)
    ]
    identity = [c for c in checks if c.kind == "balance_identity"]
    if statement.type is StatementType.BALANCE and not any(c.status == "fail" for c in identity):
        skipped = sorted({c.detail for c in identity if c.status == "skipped" and c.detail})
        if not identity or skipped or any(c.status != "pass" for c in identity):
            reasons.append(":".join(["identity_not_checked", *skipped[:1]]))
    if not any(c.status == "pass" for c in checks):
        reasons.append("unchecked")
    lost = sum(1 for _, c in cells if _lost(c))
    if lost:
        reasons.append(f"numbers_missing:{lost}")
    counts = Counter(f for _, c in cells for f in c.flags)
    reasons += [f"{flag}:{counts[flag]}" for flag in CRITICAL_CELL_FLAGS if counts[flag]]
    reasons += [f for f in statement.flags if f.split(":")[0] in _HOLD_FLAGS]
    if not primary:
        reasons = ["duplicate_statement"]
    warnings = [f for f in statement.flags if f in _WARNING_FLAGS]
    warnings += [f"{flag}:{counts[flag]}" for flag in _WARNING_CELL_FLAGS if counts[flag]]
    vouched = {(i, c.period_key) for c in checks if c.status == "pass" for i in c.line_item_ids}
    numeric = [(i, c) for i, c in cells if c.reported is not None]
    return StatementReview(
        statement_id=statement.id,
        status="needs_review" if reasons else "passed",
        reasons=reasons,
        warnings=warnings,
        numeric_cells=len(numeric),
        checked_cells=sum(1 for i, c in numeric if (i, c.period_key) in vouched),
        flagged_cells=sum(1 for _, c in cells if is_critical(c)),
    )
```

`results.py`: `StructureResult` gains `reviews: list[StatementReview] = Field(default_factory=list)` (import from `fra_ingest.review`).

`structure.py`, after the tie, before the result is built:

```python
    seen: set[StatementType] = set()
    reviews: list[StatementReview] = []
    for position, s in enumerate(statements):
        own = [c for c in checks if c.statement_id == s.id]
        if s.type is StatementType.INCOME and s.type not in seen:
            own += ties  # recorded on comprehensive income; they vouch for this statement too
        review = review_statement(s, own, primary=s.type not in seen)
        seen.add(s.type)
        reviews.append(review)
        if review.status == "needs_review":
            statements[position] = s.model_copy(update={"flags": [*s.flags, "needs_review"]})
```

`ties` is the list Task 3 built (empty when there was no tie check), and `reviews` goes into the `StructureResult`. `_structure_summary` in `cli.py` prints each statement's review status after its flags.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_review.py packages/ingest/tests/test_structure_results.py packages/ingest/tests/test_structure.py packages/ingest/tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 6: Compare the golden output**

Run `make eval-structure` and diff against the snapshot. Intended: `reviews` in every result and `needs_review` on held statements; nothing else. Run the golden tests and expect PASS.

- [ ] **Step 7: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add packages/ingest/src/fra_ingest/review.py packages/ingest/src/fra_ingest/results.py packages/ingest/src/fra_ingest/structure.py packages/ingest/src/fra_ingest/cli.py packages/ingest/tests/test_review.py packages/ingest/tests/test_structure_results.py packages/ingest/tests/test_structure_golden.py
git commit -m "Decide per statement whether it passes or is held for review, with reasons"
```

---

### Task 5: The review report

**Files:**
- Create: `packages/ingest/src/fra_ingest/review_report.py`
- Modify: `packages/ingest/src/fra_ingest/cli.py`
- Test: `packages/ingest/tests/test_review_report.py`, `packages/ingest/tests/test_cli.py`

**Interfaces:**
- Consumes: `StructureResult` with `reviews`, `list[CheckResult]`, `ConvertResult.page_images`, `load_docling_json(...).page_size(page_no)`, `is_critical`.
- Produces: `cell_class(cell: Cell, item_id: str, checks: Sequence[CheckResult]) -> str` returning `failed`, `flagged`, `checked` or `unchecked`; `render_report(result: StructureResult, checks: Sequence[CheckResult], page_sizes: Mapping[int, tuple[float, float]], page_images: Mapping[int, str], title: str) -> str`; `resolve_artifacts(root: Path, sha256: str) -> Path`; `write_review_report(sha256: str, config: IngestConfig) -> Path`. CLI: `fra-ingest review-report <sha256> [--artifacts DIR] [--config PATH]`, exit 0 with the path printed, exit 2 with the reason on an `IngestError` (`unknown_document`, `ambiguous_document`, `artifact_missing`).

- [ ] **Step 1: Write the failing tests**

`packages/ingest/tests/test_review_report.py`:

```python
"""Every extracted cell drawn on its page image (spec 12, The review report)."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fra_core.schemas import (
    BBox,
    Cell,
    CheckResult,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.results import StructureResult, TableDecision
from fra_ingest.review import StatementReview
from fra_ingest.review_report import cell_class, render_report, resolve_artifacts, write_review_report

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
SHA = "ab" * 32


def item(n: int, label: str, value: str | None, *flags: str) -> LineItem:
    box = BBox(left=300, top=100 + 20 * n, right=380, bottom=110 + 20 * n)
    cell = Cell(
        period_key=P.key,
        reported=Decimal(value) if value else None,
        raw_text=value or "",
        provenance=Provenance(page_no=5, bbox=box, table_ref="#/tables/0", row=n, col=2),
        flags=list(flags),
    )
    return LineItem(id=f"p5-t0-r{n}", raw_label=label, cells=[cell])


ITEMS = [
    item(1, "Inventories <b>&\"net\"", "10"),
    item(2, "Cash", "5"),
    item(3, "Total current assets", "16", "digit_suspect"),
    item(4, "Other", "7"),
    item(5, "Lost", None, "numbers_missing", "bbox_synthesized"),
]
STATEMENT = Statement(
    id="abababababab-balance-1",
    document_sha256=SHA,
    type=StatementType.BALANCE,
    currency="EGP",
    scale=1,
    periods=[P],
    line_items=ITEMS,
    source_pages=[5],
    flags=["subtotal_failed", "needs_review"],
)
CHECKS = [
    CheckResult(
        id="c1", statement_id=STATEMENT.id, kind="subtotal", period_key=P.key, status="fail",
        expected=Decimal(15), actual=Decimal(16), difference=Decimal(1), tolerance=Decimal(1),
        line_item_ids=["p5-t0-r1", "p5-t0-r2", "p5-t0-r3"], detail="single_digit:10^0",
    )
]
RESULT = StructureResult(
    version="4",
    sha256=SHA,
    convert_version="1",
    settings_hash="h",
    statements=[STATEMENT],
    tables=[TableDecision(table_ref="#/tables/0", docling_path="docling/p4-9.json", page_no=5, type=StatementType.BALANCE, confidence=0.9, statement_id=STATEMENT.id, evidence=["title:balance"])],
    reviews=[StatementReview(statement_id=STATEMENT.id, status="needs_review", reasons=["subtotal_failed", "digit_suspect:1"], numeric_cells=4, checked_cells=0, flagged_cells=2)],
)


def html() -> str:
    return render_report(RESULT, CHECKS, {5: (600.0, 800.0)}, {5: "pages/5.png"}, "edita")


def test_cell_classes_follow_checks_and_flags() -> None:
    passing = [CHECKS[0].model_copy(update={"status": "pass"})]
    assert cell_class(ITEMS[0].cells[0], ITEMS[0].id, CHECKS) == "failed"
    assert cell_class(ITEMS[0].cells[0], ITEMS[0].id, passing) == "checked"
    assert cell_class(ITEMS[2].cells[0], ITEMS[2].id, passing) == "flagged"
    assert cell_class(ITEMS[3].cells[0], ITEMS[3].id, CHECKS) == "unchecked"
    assert cell_class(ITEMS[4].cells[0], ITEMS[4].id, CHECKS) == "flagged"


def test_every_cell_has_a_box_inside_its_page() -> None:
    boxes = re.findall(r'class="box ([^"]*)"[^>]*style="left:([\d.]+)%;top:([\d.]+)%;width:([\d.]+)%;height:([\d.]+)%"', html())
    assert len(boxes) == 5
    for _, left, top, width, height in boxes:
        assert 0 <= float(left) and float(left) + float(width) <= 100
        assert 0 <= float(top) and float(top) + float(height) <= 100
    assert boxes[0][1:3] == ("50.00", "15.00")
    assert "synthesized" in boxes[4][0]


def test_text_from_the_document_is_escaped() -> None:
    page = html()
    assert "Inventories &lt;b&gt;&amp;&quot;net&quot;" in page
    assert "<b>&" not in page


def test_the_image_is_linked_relatively_and_the_review_is_shown() -> None:
    page = html()
    assert '<img src="pages/5.png"' in page
    assert "needs_review" in page and "digit_suspect:1" in page and "single_digit:10^0" in page
    assert "0 of 4" in page


def test_a_page_without_an_image_is_listed_without_its_overlay() -> None:
    page = render_report(RESULT, CHECKS, {5: (600.0, 800.0)}, {}, "edita")
    assert "<img" not in page and "page image missing" in page and "Total current assets" in page


def test_a_unique_prefix_finds_the_artifacts(tmp_path: Path) -> None:
    (tmp_path / SHA).mkdir()
    (tmp_path / ("cd" * 32)).mkdir()
    assert resolve_artifacts(tmp_path, "abab") == tmp_path / SHA
    with pytest.raises(IngestError) as unknown:
        resolve_artifacts(tmp_path, "ee")
    assert unknown.value.reason == "unknown_document"
    (tmp_path / ("ab" * 31 + "cd")).mkdir()
    with pytest.raises(IngestError) as ambiguous:
        resolve_artifacts(tmp_path, "abab")
    assert ambiguous.value.reason == "ambiguous_document"


def test_a_missing_artifact_is_named(tmp_path: Path) -> None:
    (tmp_path / SHA).mkdir()
    with pytest.raises(IngestError) as missing:
        write_review_report(SHA, IngestConfig(artifact_root=tmp_path))
    assert missing.value.reason == "artifact_missing"
    assert "statements.raw.json" in missing.value.detail


def test_the_report_is_written_beside_the_artifacts(tmp_path: Path) -> None:
    out = tmp_path / SHA
    (out / "docling").mkdir(parents=True)
    (out / "statements.raw.json").write_text(RESULT.model_dump_json(), encoding="utf-8")
    (out / "table_checks.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in CHECKS]), encoding="utf-8"
    )
    (out / "convert.json").write_text(
        json.dumps({"version": "1", "sha256": SHA, "locate_version": "2", "docling_version": "2", "device": "cpu", "settings_hash": "h", "ranges": [], "page_images": {"5": "pages/5.png"}}),
        encoding="utf-8",
    )
    (out / "docling" / "p4-9.json").write_text(
        json.dumps({"tables": [], "texts": [], "pages": {"5": {"page_no": 5, "size": {"width": 600, "height": 800}}}}),
        encoding="utf-8",
    )
    path = write_review_report(SHA[:8], IngestConfig(artifact_root=tmp_path))
    assert path == out / "review.html" and "Total current assets" in path.read_text(encoding="utf-8")
```

Append to `packages/ingest/tests/test_cli.py` a test that `main(["review-report", "ee", "--artifacts", str(tmp_path)])` returns 2 and prints `unknown_document` on stderr, and that with `write_review_report` monkeypatched to return a path it returns 0 and prints the path.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_review_report.py -q`
Expected: FAIL (`ModuleNotFoundError: fra_ingest.review_report`).

- [ ] **Step 3: Implement**

`review_report.py` holds four things, each a small function:

```python
def cell_class(cell: Cell, item_id: str, checks: Sequence[CheckResult]) -> str:
    """failed, flagged, checked or unchecked, the first that applies."""
    mine = [c for c in checks if item_id in c.line_item_ids and c.period_key == cell.period_key]
    if any(c.status == "fail" for c in mine):
        return "failed"
    if is_critical(cell):
        return "flagged"
    return "checked" if any(c.status == "pass" for c in mine) else "unchecked"


def _box(cell: Cell, item: LineItem, size: tuple[float, float], checks: Sequence[CheckResult]) -> str:
    width, height = size
    b = cell.provenance.bbox
    left = max(0.0, min(100.0, 100 * b.left / width))
    top = max(0.0, min(100.0, 100 * b.top / height))
    wide = max(0.0, min(100.0 - left, 100 * b.width / width))
    tall = max(0.0, min(100.0 - top, 100 * b.height / height))
    classes = [cell_class(cell, item.id, checks)]
    classes += ["synthesized"] if "bbox_synthesized" in cell.flags else []
    classes += ["moved"] if "row_realigned" in cell.flags else []
    title = f"{item.id} | {item.raw_label} | {cell.period_key} | {cell.raw_text} | {', '.join(cell.flags)}"
    return (
        f'<div class="box {" ".join(classes)}" data-row="{escape(item.id)}" title="{escape(title)}" '
        f'style="left:{left:.2f}%;top:{top:.2f}%;width:{wide:.2f}%;height:{tall:.2f}%"></div>'
    )
```

- `render_report` builds, in order: a `<style>` block (one colour per class, a dashed border for `synthesized`, a double border for `moved`, `.page{position:relative}` and `.page img{width:100%}`, `.box{position:absolute}`); the summary table, one row per statement from `result.reviews` with "`checked_cells` of `numeric_cells`"; then per statement and per source page, a two-column section: the page (`<div class="page"><img src="..."/>` plus the boxes of that statement's cells on that page, or a paragraph "page image missing" when the page has no image or no size) and the rows table (id, depth, label, note, each period's value with its flags, and the checks whose last or first id is the row with expected, actual, difference and detail); then the table decisions; then twelve lines of script that toggle a `hot` class on boxes and rows sharing a `data-row`. Every string that comes from the document goes through `html.escape(..., quote=True)`.
- `resolve_artifacts(root, sha256)`: the directories under `root` whose name starts with the text; none raises `IngestError("unknown_document", text)`, several raise `IngestError("ambiguous_document", ", ".join(names))`.
- `write_review_report(sha256, config)`: resolves the directory; loads `statements.raw.json`, `table_checks.json` and `convert.json`, raising `IngestError("artifact_missing", name)` for the first that is absent; reads page sizes for the statements' pages from the docling files `convert.json` names; writes `review.html` atomically (temporary file, then `os.replace`) and returns its path.

`cli.py`: a fourth subcommand `review-report` with the positional `sha256` and the options `--config` and `--artifacts`; it calls `write_review_report`, prints the path and returns 0; `IngestError` is handled by the existing branch, which prints `"{sha256}: {reason} {detail}"`. Add the command to the module docstring.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_review_report.py packages/ingest/tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Write the report for the twelve golden documents and look at one**

```bash
for d in var/artifacts/*/; do uv run fra-ingest review-report $(basename $d); done
```

Expected: twelve paths. Open `var/artifacts/ec1f045a.../review.html`: the boxes sit on the printed figures, the 2023 total assets is marked, and the equity rows show their realigned cells.

- [ ] **Step 6: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add packages/ingest/src/fra_ingest/review_report.py packages/ingest/src/fra_ingest/cli.py packages/ingest/tests/test_review_report.py packages/ingest/tests/test_cli.py
git commit -m "Add fra-ingest review-report: every cell on its page image with its checks"
```

---

### Task 6: The golden eval's identity rule

**Files:**
- Modify: `eval/harness/structure.py`
- Test: `tests/eval/test_structure_harness.py`

**Interfaces:**
- Consumes: `StructureResult.reviews`, the `digit_suspect` flag.
- Produces: `identity_excuses(statement, checks)` returns the cells carrying `numbers_missing` or `digit_suspect` on rows of each failed identity check, and `None` when a failed check has none; `main` fails a document whose identity failed unless that list exists and the statement is `needs_review`. Each statement's row in `structure-golden.json` gains `review`, `review_reasons` and `checked_cells`.

- [ ] **Step 1: Write the failing tests**

In `tests/eval/test_structure_harness.py`, beside the existing `identity_excuses` tests, add: a failed identity whose total assets cell carries `digit_suspect` returns `[{"item": ..., "period": ..., "flag": "digit_suspect"}]`; one whose rows carry neither flag returns `None`. Update the existing excuse test for the added `flag` key.

- [ ] **Step 2: Run to see them fail, implement, run again**

`identity_excuses` collects `{"item", "period", "flag"}` for each cell on the check's rows, in the check's period, with either flag. `identity_accepted(excuses, review) -> bool` is true when the excuses exist and the review is `needs_review`. In `main`, load the reviews by statement id from the result, add them to the row, print `passed` or `held` after each statement's identity, and fail a document whose failed identity is not accepted, with the reason text `identity failed and nothing on its rows explains it`.

Run: `uv run pytest tests/eval/test_structure_harness.py -q`
Expected: PASS.

- [ ] **Step 3: Run the eval**

Run: `make eval-structure`
Expected: `PASS`. The Almarai pair is still `(0, 0)` on all three statements.

- [ ] **Step 4: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add eval/harness/structure.py tests/eval/test_structure_harness.py
git commit -m "Accept a failed identity only on a held statement whose rows name the figure"
```

---

### Task 7: Expected files and the extraction eval

**Files:**
- Create: `eval/harness/expected.py`, `eval/harness/extraction.py`
- Modify: `Makefile` (`expected-drafts`, `eval-extraction`), `eval/golden/README.md` (the expected files and their status)
- Test: `tests/eval/test_expected.py`, `tests/eval/test_extraction_harness.py`

**Interfaces:**
- Produces, in `expected.py`: `ExpectedRow(label: str, values: dict[str, Decimal | None], unconfirmed: list[str])`, `ExpectedStatement(type, pages, page_mode: Literal["digital", "scanned"], scale, currency, periods: list[Period], rows)`, `ExpectedFile(id, sha256, status: Literal["draft", "checked"], checked_by: list[str], note: str, statements)`; `draft_expected(document_id: str, result: StructureResult) -> ExpectedFile` (the first statement of each type, rows that have cells, a lost figure as `null`, every period listed in `unconfirmed`); `load_expected(path) -> ExpectedFile`; `ExpectedFile.confirmed_cells`; `write_draft(path: Path, draft: ExpectedFile) -> bool`, which refuses to replace a file whose status is `checked` or that has any confirmed cell, unless `force`.
- Produces, in `extraction.py`: `align_rows(expected: Sequence[ExpectedRow], items: Sequence[LineItem]) -> list[int | None]`; `Score(cells, right, sign_cells, sign_right, periods, periods_right, metadata, metadata_right, unconfirmed, extra_rows, wrong)`, `wrong` listing each missed figure with what was read; `score_statement(expected: ExpectedStatement, statement: Statement | None) -> Score`; `report(files: Sequence[tuple[ExpectedFile, Mapping[StatementType, Statement]]]) -> tuple[list[str], int]`, the printed lines and the exit code: a block for checked files against G1 and a block headed provisional for the confirmed cells of drafts, 1 only when a checked file misses a threshold; `main` loads the files and the stored statements, prints the report and writes `var/eval/extraction-golden.json`.

- [ ] **Step 1: Write the failing tests**

`tests/eval/test_extraction_harness.py` builds expected rows and extracted line items by hand and asserts:

```python
def test_rows_align_by_label_or_by_a_shared_value() -> None:
    expected = [row("Inventories", "10"), row("Cash", "5"), row("Total", "15")]
    items = [item(1, "lnventories", "10"), item(2, "Notes heading", None), item(3, "Cash", "6"), item(4, "Total", "15")]
    assert align_rows(expected, items) == [0, 2, 3]


def test_a_misread_a_missing_row_and_an_extra_row_are_scored() -> None:
    expected = statement_of([row("Inventories", "10"), row("Cash", "5"), row("Receivables", "7"), row("Total", "22")])
    extracted = extracted_of([item(1, "Inventories", "10"), item(2, "Cash", "6"), item(3, "Other", "99"), item(4, "Total", "22")])
    score = score_statement(expected, extracted)
    assert (score.cells, score.right, score.extra_rows) == (4, 2, 1)


def test_a_wrong_sign_counts_against_sign_accuracy_and_cell_accuracy() -> None:
    score = score_statement(statement_of([row("Cost", "-60")]), extracted_of([item(1, "Cost", "60")]))
    assert (score.cells, score.right, score.sign_cells, score.sign_right) == (1, 0, 1, 0)


def test_periods_are_scored_by_key_date_kind_and_length() -> None:
    score = score_statement(statement_of([row("Cash", "5")]), extracted_of([item(1, "Cash", "5")], period=OTHER))
    assert (score.periods, score.periods_right) == (1, 0)


def test_unconfirmed_cells_are_counted_apart() -> None:
    score = score_statement(statement_of([row("Cash", "5", unconfirmed=True), row("Total", "5")]), extracted_of([item(1, "Cash", "9"), item(2, "Total", "5")]))
    assert (score.cells, score.right, score.unconfirmed) == (1, 1, 1)


def test_a_statement_that_was_not_extracted_misses_every_cell() -> None:
    score = score_statement(statement_of([row("Cash", "5"), row("Total", "5")]), None)
    assert (score.cells, score.right, score.periods_right) == (2, 0, 0)


def test_drafts_stay_out_of_the_gate_and_a_checked_miss_fails_it() -> None:
    wrong = extracted_of([item(1, "Cash", "9"), item(2, "Total", "5")])
    expected = statement_of([row("Cash", "5"), row("Total", "5")])
    lines, code = report([(file_of(expected, status="draft"), {StatementType.BALANCE: wrong})])
    text = "\n".join(lines)
    assert code == 0 and "G1 not measured" in text and "provisional" in text
    assert "cells 2  right 1" in text
    lines, code = report([(file_of(expected, status="checked"), {StatementType.BALANCE: wrong})])
    assert code == 1 and "digital  cells 2  right 1   50.00%  below 99.50%" in "\n".join(lines)
```

with small helpers `row`, `item`, `statement_of`, `extracted_of` and `file_of` at the top of the file: `row(label, value, unconfirmed=False)` builds an `ExpectedRow` for one period, `item(n, label, value)` a `LineItem` with one cell, `statement_of(rows)` a digital balance `ExpectedStatement` over that period, `extracted_of(items, period=P)` a `Statement`, and `file_of(statement, status)` an `ExpectedFile`.

`tests/eval/test_expected.py` asserts: a draft takes the first statement of each type, keeps rows with values only, lists every valued period as unconfirmed, records `scanned` when any cell's source is OCR; `write_draft` writes a new file, replaces an untouched draft, and refuses a `checked` file and a draft with a confirmed cell.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/eval/test_expected.py tests/eval/test_extraction_harness.py -q`
Expected: FAIL (`ModuleNotFoundError: harness.expected`).

- [ ] **Step 3: Implement**

`align_rows` is a longest-common-subsequence over the two row lists, two rows matching when `squash(label)` is equal and not empty, or when they share an equal non-zero value in one period; it returns, for each expected row, the index of its extracted row or `None`. `score_statement` walks the expected rows: a valued period listed in `unconfirmed` adds to `unconfirmed`; otherwise it adds to `cells`, and to `right` when the aligned row's value equals it; when the absolute values are equal it adds to `sign_cells`, and to `sign_right` when the values are equal. Periods are compared as `(key, end_date, kind, months)`. `extra_rows` counts extracted rows with values that no expected row aligned to. Scale and currency add two to `metadata` per statement and to `metadata_right` when equal. Thresholds are constants beside G1's names: `DIGITAL = 0.995`, `SCANNED = 0.98`, `PERIODS = 1.0`, `SIGN = 0.999`, `METADATA = 1.0`.

`main` reads every `eval/golden/expected/*.json` and loads each document's statements through the manifest's file name and `structure_pdf(..., use_cache=True)`. `report` sums the scores by `status` and `page_mode` and returns lines such as:

```
checked files: 0
G1 not measured: no expected file is checked yet
provisional (drafts, confirmed cells only; not a gate)
  digital  cells 180  right 180  100.00%   unconfirmed 0
  scanned  cells 141  right 133   94.33%   unconfirmed 9
```

Makefile:

```make
expected-drafts: ## Draft expected files from the extraction (never replaces a checked or confirmed file)
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/expected.py $(if $(ONLY),--only $(ONLY))

eval-extraction: ## Score the extraction against eval/golden/expected (G1 on checked files)
	@$(MAKE) --no-print-directory unhide-pth
	PYTHONPATH=eval $(UV) run python -m harness.extraction
```

`extraction.py` imports `harness.expected`, so it runs as a module with `eval` on the path, as the tests import it.

`eval/golden/README.md` gains a section "Expected files": the format, that a draft comes from the extraction, what `unconfirmed` means, and that only `checked` files count towards G1 (two readings, V1).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/eval -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add eval/harness/expected.py eval/harness/extraction.py Makefile eval/golden/README.md tests/eval/test_expected.py tests/eval/test_extraction_harness.py
git commit -m "Score the extraction against expected files, with drafts kept out of the gate"
```

---

### Task 8: Drafts, results, pull request text

**Files:**
- Create: `eval/golden/expected/almarai-2025-en.json`, `eval/golden/expected/edita-2025-en-consolidated-ifrs.json`, `eval/golden/expected/edita-2024-ar-consolidated.json`
- Modify: `docs/blueprint/12-ingest-review.md` (Results), `docs/blueprint/11-ingest-structure.md` (a line under Results pointing to spec 12), `docs/exec-summaries/extraction-week1-summary.md`, `var/pr/ingest-structure.md` (not tracked)

- [ ] **Step 1: Write the three drafts**

Run: `make expected-drafts ONLY=almarai-2025-en` and the same for the other two.

- [ ] **Step 2: Compare each draft with its page images once**

For each statement, open the review report beside the draft and compare every figure with the page image. Where the image shows a different figure, correct the draft's value. Remove a period from a row's `unconfirmed` only when the figure is legible on the image and equals the draft. Leave `status: draft` and `checked_by: []`, and set `note` to say the file was prepared from the extraction and compared with the page image once, and how many figures stay unconfirmed. Almarai is digital: its figures are also confirmed by the Arabic edition's identical values, which the note says.

- [ ] **Step 3: Run both evals and record the results**

Run: `make eval-structure` and `make eval-extraction`.

Add a Results section to spec 12 in the shape of spec 11's: per document the review status of each statement with reasons, checked cells out of numeric cells, the subtotal outcomes before and after, the Done-when table with a result column, and the extraction eval's two blocks with the count of unconfirmed cells stated beside every figure. State plainly that no file is checked yet, so G1 is not measured.

- [ ] **Step 4: Update the pull request text and the summary**

Rewrite `var/pr/ingest-structure.md`: the title on the first line covering 3a and 3b, then the description, with a 3b section (what it does, results, what the owner needs to do: two readings of the expected files, open decisions 1 to 3) and no attribution footer. In the week 1 summary, update the row for part 3b in "Next" and the balance-sheet identity result.

- [ ] **Step 5: Check, commit and push**

```bash
make test && make lint && make typecheck && make docs-check && make corpus-check
git add eval/golden/expected docs/blueprint/12-ingest-review.md docs/blueprint/11-ingest-structure.md docs/exec-summaries/extraction-week1-summary.md
git commit -m "Record the part 3b results and add draft expected files for three documents"
git log origin/main..HEAD --format='%an <%ae>%n%cn <%ce>%n%B'   # read it against CLAUDE.md, rule 6
git push origin ingest-structure
```

Do not open, merge or comment on the pull request. Tell the owner the branch is ready and what is theirs to do.
