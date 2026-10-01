# Ingest Part 3a: Structure Implementation Plan

**Goal:** Turn the docling tables Part 2 writes into `Statement` objects with provenance on every value, periods bound from header text, scale and currency, statements joined across pages, a light hierarchy, subtotal and balance-sheet checks, so that Almarai's English and Arabic statements give identical figures.

**Architecture:** `fra-ingest structure <pdf>` runs in the ingest worker without docling: it reads Part 2's docling JSON through small models, builds a grid per table, repairs visual-order Arabic, classifies each grid, binds header columns to periods, builds line items, joins continuations, infers a light hierarchy, runs the checks, and writes `statements.raw.json` and `table_checks.json` next to Part 2's artifacts. Each step is a small module with its own tests; `structure.py` only orchestrates.

**Tech Stack:** Python 3.12, pydantic 2, the `fra_core` parsers (`parse_number`, `parse_period`, `detect_scale`, `detect_currency`, `normalize_label`) and taxonomy, pytest, uv. docling 2.126.0 is used only by Task 1's setting.

**Spec:** `docs/blueprint/11-ingest-structure.md` (read it first). Background: `docs/blueprint/04-execution-phases.md` 1.3 to 1.8, `docs/blueprint/10-ingest-convert.md`, `eval/golden/README.md`.

## Global Constraints

- Only `packages/ingest/src/fra_ingest/converter.py` imports `docling` or `docling_core`, and only inside functions. Structure reads docling JSON through `fra_ingest/docling_json.py`.
- Tests are written before the code they cover. `make test`, `make lint` and `make typecheck` pass before every commit.
- Artifacts live under `<artifact_root>/<sha256>/`: `statements.raw.json` and `table_checks.json` join Part 2's files. `var/` is gitignored; never commit artifacts or PDFs.
- `STRUCTURE_VERSION = "1"`. `min_confidence` default 0.5 in `[structure]` of `configs/ingest.toml`. Continuation column tolerance: 5% of page width. Subtotal tolerance: n x 0.5 reported units, n the number of addends (D6).
- Digit reversal happens only on pages Part 1 marked `visual_arabic`. Spaced letters are joined on any page.
- Columns are bound to periods by header text or by position against the previous page's periods, never by column index alone.
- Values stay as printed (sign applied, before scale); the statement records scale and currency. A missing currency is `XXX` with `currency_missing`; a missing scale is 1 with `scale_missing`.
- Never run anything against the `blind` or `model_test` pools.
- Commits: author and committer `noah-mclain <nadam.30032415@gmail.com>`; messages carry no trailers.

## Review Focus

- A statement page with two statement tables of the same type side by side, or a signature table under the statement, must not merge into the statement or break it (Task 13, signature table rejected in the Almarai golden test).
- An Arabic continuation page whose second value column has no date in its header must still bind that column to the prior year by position (Task 11, inherit test; Task 13, Almarai AR income).
- A row whose label ends in a note number, with no note column, must not keep the number in the label or lose it (Task 7, note split test).
- A cell that holds a date or a year in a data row must not be taken for an amount, and a note column of small integers must not be taken for a value column (Task 7).
- A statement whose currency appears only as a glyph must carry the document's currency with `currency_inferred`, not a wrong or empty one (Task 6, Task 13).

---

### Task 1: OCR scale setting and the Juhayna measurement

**Files:**
- Modify: `packages/ingest/src/fra_ingest/config.py`, `configs/ingest.toml`
- Modify: `packages/ingest/src/fra_ingest/converter.py` (`pipeline_options`)
- Modify: `packages/ingest/src/fra_ingest/convert.py` (`settings_hash`)
- Test: `packages/ingest/tests/test_config.py`, `packages/ingest/tests/test_convert.py`, `packages/ingest/tests/test_converter.py`
- Modify: `docs/blueprint/11-ingest-structure.md` (Measured before designing: results row)

**Interfaces:**
- Produces: `IngestConfig.ocr_scale: float = 3.0` (TOML `[convert] ocr_scale`), passed to `OcrMacOptions(scale=...)` and included in `settings_hash`.

- [ ] **Step 1: Write the failing tests**

Append to `packages/ingest/tests/test_config.py`:

```python
def test_ocr_scale_defaults_to_doclings_own(tmp_path: Path) -> None:
    assert IngestConfig().ocr_scale == 3.0
    assert load_config(write(tmp_path, "[convert]\nocr_scale = 4.0\n")).ocr_scale == 4.0
```

In `packages/ingest/tests/test_convert.py`, extend the parametrize list of `test_the_settings_hash_follows_settings_and_plans` with `{"ocr_scale": 4.0}`:

```python
@pytest.mark.parametrize(
    "change", [{"device": "cpu"}, {"do_cell_matching": False}, {"ocr_scale": 4.0}]
)
```

In `packages/ingest/tests/test_converter.py`, inside `test_options_follow_the_plan_and_the_settings`, change the config line and add one assertion after `assert options.ocr_options.mode.name == "FULL_PAGE"`:

```python
    config = IngestConfig(device="cpu", images_scale=1.5, batch_size=3, ocr_scale=4.0)
```

```python
    assert options.ocr_options.scale == 4.0
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_config.py packages/ingest/tests/test_convert.py -q`
Expected: FAIL (`AttributeError: ... 'ocr_scale'` / hash unchanged).

- [ ] **Step 3: Implement**

`config.py`: add to `_TOML_FIELDS` `("convert", "ocr_scale"): "ocr_scale",` and to `IngestConfig` after `images_scale`:

```python
    ocr_scale: float = Field(default=3.0, gt=0.0, le=6.0)
```

`configs/ingest.toml`, after `images_scale = 2.0`:

```toml
# Resolution docling's OCR renders pages at, as a multiple of 72 dpi. 3.0 is docling's own
# default (216 dpi).
ocr_scale = 3.0
```

`converter.py`, in `pipeline_options`, the OCR options line becomes:

```python
        options.ocr_options = OcrMacOptions(
            lang=[plan.ocr_language], mode=mode, scale=config.ocr_scale
        )
```

`convert.py`, in `settings_hash`, add `"ocr_scale": config.ocr_scale,` to the payload.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_config.py packages/ingest/tests/test_convert.py -q` then `uv run pytest packages/ingest/tests/test_converter.py -q -m "slow or not slow" -k options`
Expected: PASS.

- [ ] **Step 5: Measure Juhayna at OCR scale 3, 4, 5 and with cell matching off**

Write four temporary configs outside the repository and convert the two Juhayna consolidated filings with each, into a separate artifact root so the golden artifacts are untouched:

```bash
S=$(mktemp -d)
for v in s3 s4 s5 nomatch; do
  case $v in
    s3) sed 's/^ocr_scale = .*/ocr_scale = 3.0/' configs/ingest.toml ;;
    s4) sed 's/^ocr_scale = .*/ocr_scale = 4.0/' configs/ingest.toml ;;
    s5) sed 's/^ocr_scale = .*/ocr_scale = 5.0/' configs/ingest.toml ;;
    nomatch) sed 's/^do_cell_matching = .*/do_cell_matching = false/' configs/ingest.toml ;;
  esac > $S/$v.toml
  for doc in juhayna-2025-en-consolidated juhayna-2025-ar-consolidated; do
    uv run fra-ingest convert eval/golden/documents/$doc.pdf --config $S/$v.toml --artifacts $S/art-$v --json > $S/$v-$doc.json
  done
done
uv run python - "$S" <<'EOF'
import json, re, sys, pathlib
root = pathlib.Path(sys.argv[1])
digits = re.compile(r"[0-9٠-٩۰-۹]{3,}")
for v in ("s3", "s4", "s5", "nomatch"):
    for doc in ("juhayna-2025-en-consolidated", "juhayna-2025-ar-consolidated"):
        result = json.loads((root / f"{v}-{doc}.json").read_text())
        out = root / f"art-{v}" / result["sha256"]
        counts = {}
        for r in result["ranges"]:
            d = json.loads((out / r["docling_path"]).read_text())
            for t in d["tables"]:
                if t["prov"][0]["page_no"] == 5:
                    counts[5] = counts.get(5, 0) + sum(1 for c in t["data"]["table_cells"] if digits.search(c["text"]))
        print(f"{v:8} {doc:30} p5 numeric cells {counts.get(5, 0):3}  convert {result['timings']['convert']:.1f}s  peak {result['peak_footprint_gb']:.2f} GB")
EOF
```

Expected: eight lines. The baseline at scale 3 is about 13 (EN) and 17 (AR).

- [ ] **Step 6: Apply the decision rule**

- If one of scale 4 or 5 at least doubles the numeric cells on both pages and every peak stays at or under 3.5 GB, set `ocr_scale` to the lowest such value in both `configs/ingest.toml` and the `IngestConfig` default (update `test_ocr_scale_defaults_to_doclings_own` to that value), then run `make eval-convert FRESH=1` and confirm `PASS`; if it does not pass, revert to 3.0 and record why.
- If only cell matching off helps, leave the settings unchanged: that comparison belongs to 04, 1.9, and is recorded for the owner.
- Otherwise leave `ocr_scale = 3.0`.

Add a row to the "Measured before designing" table of `docs/blueprint/11-ingest-structure.md`:

```markdown
| Juhayna EN and AR p5, OCR scale test (task 1) | <EN and AR numeric cells at scale 3, 4, 5 and with cell matching off; convert time and peak of each> | <the decision taken and why> |
```

Fill both cells from Step 5's output; `make docs-check` must pass.

- [ ] **Step 7: Commit**

```bash
make test && make lint && make typecheck && make docs-check
git add packages/ingest/src/fra_ingest/config.py configs/ingest.toml packages/ingest/src/fra_ingest/converter.py packages/ingest/src/fra_ingest/convert.py packages/ingest/tests/test_config.py packages/ingest/tests/test_convert.py packages/ingest/tests/test_converter.py docs/blueprint/11-ingest-structure.md
git commit -m "Make docling's OCR scale a setting and record the Juhayna scale test"
```

---

### Task 2: docling JSON models

**Files:**
- Create: `packages/ingest/src/fra_ingest/docling_json.py`
- Modify: `packages/ingest/tests/support.py` (add `artifact_dir`)
- Test: `packages/ingest/tests/test_docling_json.py`

**Interfaces:**
- Produces: `DlBox` (`left, top, right, bottom` from JSON keys `l, t, r, b`; `coord_origin`; `to_bbox(page_height) -> BBox | None`), `DlProv(page_no, bbox)`, `DlCell`, `DlTableData`, `DlTable(self_ref, prov, data)`, `DlText(self_ref, label, text, prov)`, `DlDocument(tables, texts, pages)` with `page_size(page_no) -> DlSize` and `texts_on(page_no) -> list[DlText]`, and `load_docling_json(path: Path) -> DlDocument`. Test helper `artifact_dir(pdf: Path) -> Path`.

- [ ] **Step 1: Add `artifact_dir` to `support.py`**

Add `import hashlib` and:

```python
def artifact_dir(pdf: Path) -> Path:
    """Where Part 2 left this document's artifacts, under the repository's var/artifacts."""
    return REPO_ROOT / "var" / "artifacts" / hashlib.sha256(pdf.read_bytes()).hexdigest()
```

- [ ] **Step 2: Write the failing tests**

```python
"""The docling JSON fields structure reads (spec 11, Components; R31)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support import artifact_dir

from fra_ingest.docling_json import DlBox, DlDocument, load_docling_json


def test_a_top_left_box_is_kept() -> None:
    box = DlBox.model_validate({"l": 10, "t": 20, "r": 30, "b": 40, "coord_origin": "TOPLEFT"})
    bbox = box.to_bbox(page_height=800)
    assert bbox is not None
    assert (bbox.left, bbox.top, bbox.right, bbox.bottom) == (10, 20, 30, 40)


def test_a_bottom_left_box_is_flipped() -> None:
    box = DlBox.model_validate(
        {"l": 10, "t": 700, "r": 30, "b": 680, "coord_origin": "BOTTOMLEFT"}
    )
    bbox = box.to_bbox(page_height=800)
    assert bbox is not None
    assert (bbox.top, bbox.bottom) == (100, 120)


def test_a_degenerate_box_gives_none() -> None:
    box = DlBox.model_validate({"l": 10, "t": 20, "r": 10, "b": 40, "coord_origin": "TOPLEFT"})
    assert box.to_bbox(page_height=800) is None


def test_a_minimal_document_loads_and_ignores_other_fields() -> None:
    document = DlDocument.model_validate(
        {
            "schema_name": "DoclingDocument",
            "tables": [
                {
                    "self_ref": "#/tables/0",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 0, "t": 90, "r": 50, "b": 10, "coord_origin": "BOTTOMLEFT"},
                            "charspan": [0, 0],
                        }
                    ],
                    "data": {
                        "num_rows": 1,
                        "num_cols": 1,
                        "table_cells": [
                            {
                                "text": "7",
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1,
                                "start_col_offset_idx": 0,
                                "end_col_offset_idx": 1,
                                "fillable": False,
                            }
                        ],
                        "grid": [],
                    },
                }
            ],
            "texts": [
                {
                    "self_ref": "#/texts/0",
                    "label": "text",
                    "text": "As at 31 December 2025",
                    "prov": [
                        {
                            "page_no": 3,
                            "bbox": {"l": 0, "t": 99, "r": 50, "b": 95, "coord_origin": "BOTTOMLEFT"},
                            "charspan": [0, 22],
                        }
                    ],
                }
            ],
            "pages": {"3": {"page_no": 3, "size": {"width": 600, "height": 100}}},
        }
    )
    assert document.page_size(3).height == 100
    assert [t.text for t in document.texts_on(3)] == ["As at 31 December 2025"]
    assert document.texts_on(4) == []
    cell = document.tables[0].data.table_cells[0]
    assert (cell.text, cell.bbox, cell.column_header) == ("7", None, False)


@pytest.mark.golden
@pytest.mark.parametrize(
    ("name", "range_file", "tables"),
    [("almarai-2025-en-annualreport.pdf", "p155-164.json", 14)],
)
def test_real_docling_output_loads(
    golden: Callable[[str], Path], name: str, range_file: str, tables: int
) -> None:
    path = artifact_dir(golden(name)) / "docling" / range_file
    if not path.exists():
        pytest.skip("run make eval-convert first")
    document = load_docling_json(path)
    assert len(document.tables) == tables
    for table in document.tables:
        page = table.prov[0].page_no
        assert document.page_size(page).height > 0
        boxes = [c.bbox.to_bbox(document.page_size(page).height) for c in table.data.table_cells if c.bbox]
        assert boxes and all(b is not None for b in boxes)


@pytest.mark.golden
def test_scanned_arabic_docling_output_loads(golden: Callable[[str], Path]) -> None:
    path = artifact_dir(golden("juhayna-2025-ar-consolidated.pdf")) / "docling" / "p4-9.json"
    if not path.exists():
        pytest.skip("run make eval-convert first")
    document = load_docling_json(path)
    assert document.tables
    assert any(t.prov[0].page_no == 5 for t in document.tables)
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_docling_json.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.docling_json'`.

- [ ] **Step 4: Implement `docling_json.py`**

```python
"""The parts of docling's JSON that the structure stage reads.

Structure reads Part 2's output without importing docling or docling-core. Only the fields
used are modelled, everything else is ignored, and a golden test fails if docling's JSON stops
matching them (spec 11, R31).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import BBox


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class DlBox(_Model):
    left: float = Field(alias="l")
    top: float = Field(alias="t")
    right: float = Field(alias="r")
    bottom: float = Field(alias="b")
    coord_origin: Literal["TOPLEFT", "BOTTOMLEFT"]

    def to_bbox(self, page_height: float) -> BBox | None:
        """PDF points with a top-left origin, or None for a box with no area."""
        if self.coord_origin == "TOPLEFT":
            top, bottom = self.top, self.bottom
        else:
            top, bottom = page_height - self.top, page_height - self.bottom
        left, right = min(self.left, self.right), max(self.left, self.right)
        top, bottom = min(top, bottom), max(top, bottom)
        if right <= left or bottom <= top:
            return None
        return BBox(left=left, top=top, right=right, bottom=bottom)


class DlProv(_Model):
    page_no: int
    bbox: DlBox


class DlCell(_Model):
    text: str = ""
    bbox: DlBox | None = None
    row_span: int = 1
    col_span: int = 1
    start_row_offset_idx: int
    start_col_offset_idx: int
    column_header: bool = False
    row_header: bool = False
    row_section: bool = False


class DlTableData(_Model):
    num_rows: int
    num_cols: int
    table_cells: list[DlCell] = Field(default_factory=list)


class DlTable(_Model):
    self_ref: str
    prov: list[DlProv] = Field(min_length=1)
    data: DlTableData


class DlText(_Model):
    self_ref: str
    label: str
    text: str = ""
    prov: list[DlProv] = Field(default_factory=list)


class DlSize(_Model):
    width: float
    height: float


class DlPage(_Model):
    page_no: int
    size: DlSize


class DlDocument(_Model):
    tables: list[DlTable] = Field(default_factory=list)
    texts: list[DlText] = Field(default_factory=list)
    pages: dict[str, DlPage] = Field(default_factory=dict)

    def page_size(self, page_no: int) -> DlSize:
        return self.pages[str(page_no)].size

    def texts_on(self, page_no: int) -> list[DlText]:
        return [t for t in self.texts if t.prov and t.prov[0].page_no == page_no]


def load_docling_json(path: Path) -> DlDocument:
    return DlDocument.model_validate_json(path.read_bytes())
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_docling_json.py -q`
Expected: PASS (the golden tests pass where Part 2's artifacts exist, otherwise they skip with the stated reason).

- [ ] **Step 6: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/docling_json.py packages/ingest/tests/support.py packages/ingest/tests/test_docling_json.py
git commit -m "Read docling's JSON through models of the fields structure uses"
```

---

### Task 3: Shared types and label matching

**Files:**
- Create: `packages/core/src/fra_core/schemas/check.py`
- Modify: `packages/core/src/fra_core/schemas/__init__.py` (export `CheckResult`)
- Modify: `packages/ingest/src/fra_ingest/results.py` (add `TableDecision`, `StructureResult`)
- Create: `packages/ingest/src/fra_ingest/label_match.py`
- Modify: `packages/ingest/src/fra_ingest/config.py`, `configs/ingest.toml` (`[structure] min_confidence`)
- Test: `packages/core/tests/test_check_schema.py`, `packages/ingest/tests/test_label_match.py`, `packages/ingest/tests/test_structure_results.py`, `packages/ingest/tests/test_config.py`

**Interfaces:**
- Produces:
  - `fra_core.schemas.CheckResult(id, statement_id, kind: Literal["subtotal", "balance_identity"], period_key, status: Literal["pass", "fail", "skipped"], expected: Decimal | None = None, actual: Decimal | None = None, difference: Decimal | None = None, tolerance: Decimal | None = None, line_item_ids: list[str] = [], detail: str = "")`.
  - `fra_ingest.results.TableDecision(table_ref, docling_path, page_no, type: StatementType | None, confidence, statement_id: str | None, evidence: list[str])` and `StructureResult(version, sha256, convert_version, settings_hash, statements: list[Statement], tables: list[TableDecision], flags, timings)`.
  - `fra_ingest.label_match`: `squash(text) -> str`, `has_subtotal_cue(label) -> bool`, `class LabelIndex(taxonomy)` with `match(label, statement) -> CanonicalItem | None` and `hits(labels, statement) -> int`.
  - `IngestConfig.min_confidence: float = 0.5` (TOML `[structure] min_confidence`).

- [ ] **Step 1: Write the failing tests**

`packages/core/tests/test_check_schema.py`:

```python
"""Check results shared by structure and analytics."""

from decimal import Decimal

from fra_core.schemas import CheckResult


def test_a_check_result_round_trips_with_exact_decimals() -> None:
    check = CheckResult(
        id="s1:balance_identity:2025-12-31",
        statement_id="s1",
        kind="balance_identity",
        period_key="2025-12-31",
        status="fail",
        expected=Decimal("100.5"),
        actual=Decimal("99.5"),
        difference=Decimal("-1.0"),
        tolerance=Decimal("0.5"),
        line_item_ids=["a", "b"],
        detail="total assets against total liabilities and equity",
    )
    assert CheckResult.model_validate_json(check.model_dump_json()) == check
```

`packages/ingest/tests/test_label_match.py`:

```python
"""Label matching that ignores lost word boundaries (spec 11, Data flow step 3)."""

from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_match import LabelIndex, has_subtotal_cue, squash

BALANCE = StatementType.BALANCE


def test_squash_drops_spaces_after_normalizing() -> None:
    assert squash("Total  Assets") == "totalassets"
    assert squash("إجمالي الموجودات") == squash("اجماليالموجودات")


def test_labels_match_with_or_without_word_boundaries() -> None:
    index = LabelIndex(load_taxonomy())
    for label in ("Total Assets", "إجمالي الموجودات", "إجماليالموجودات", "إ ج م ا ل ي ا ل م و ج و د ا ت"):
        item = index.match(label, BALANCE)
        assert item is not None and item.id == "total_assets", label
    current = index.match("Total current assets", BALANCE)
    assert current is None or current.id != "total_assets"
    assert index.match("", BALANCE) is None


def test_hits_count_matched_labels() -> None:
    index = LabelIndex(load_taxonomy())
    assert index.hits(["Revenue", "Cost of sales", "Chairman"], StatementType.INCOME) == 2


def test_subtotal_cues_in_both_languages() -> None:
    assert has_subtotal_cue("Total current assets")
    assert has_subtotal_cue("إجمالي المطلوبات")
    assert has_subtotal_cue("مجموع حقوق الملكية")
    assert has_subtotal_cue("إجماليالموجودات")
    assert not has_subtotal_cue("Inventories")
```

`packages/ingest/tests/test_structure_results.py`:

```python
"""What structure writes to statements.raw.json (spec 11, Data model)."""

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
from fra_ingest.results import StructureResult, TableDecision

SHA = "e" * 64


def test_a_structure_result_round_trips() -> None:
    period = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
    cell = Cell(
        period_key=period.key,
        reported=Decimal("-15177063"),
        raw_text="(15,177,063)",
        provenance=Provenance(
            page_no=159,
            bbox=BBox(left=1, top=2, right=3, bottom=4),
            table_ref="#/tables/4",
            row=3,
            col=2,
        ),
    )
    statement = Statement(
        id="s1",
        document_sha256=SHA,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[period],
        line_items=[LineItem(id="p159-t4-r3", raw_label="Cost of Sales", cells=[cell])],
    )
    result = StructureResult(
        version="1",
        sha256=SHA,
        convert_version="1",
        settings_hash="h",
        statements=[statement],
        tables=[
            TableDecision(
                table_ref="#/tables/3",
                docling_path="docling/p155-164.json",
                page_no=158,
                type=None,
                confidence=0.0,
                statement_id=None,
                evidence=["no_period_header"],
            )
        ],
    )
    again = StructureResult.model_validate_json(result.model_dump_json())
    assert again == result
    assert again.statements[0].line_items[0].cells[0].reported == Decimal("-15177063")
```

Append to `packages/ingest/tests/test_config.py`:

```python
def test_structure_confidence_setting(tmp_path: Path) -> None:
    assert IngestConfig().min_confidence == 0.5
    assert load_config(write(tmp_path, "[structure]\nmin_confidence = 0.7\n")).min_confidence == 0.7
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/core/tests/test_check_schema.py packages/ingest/tests/test_label_match.py packages/ingest/tests/test_structure_results.py packages/ingest/tests/test_config.py -q`
Expected: FAIL with import errors for `CheckResult`, `fra_ingest.label_match`, `StructureResult`, and the missing `min_confidence`.

- [ ] **Step 3: Implement**

`packages/core/src/fra_core/schemas/check.py`:

```python
"""The outcome of one arithmetic check on a statement: a subtotal or an accounting identity."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    statement_id: str
    kind: Literal["subtotal", "balance_identity"]
    period_key: str
    status: Literal["pass", "fail", "skipped"]
    expected: Decimal | None = None
    actual: Decimal | None = None
    difference: Decimal | None = None
    tolerance: Decimal | None = None
    line_item_ids: list[str] = Field(default_factory=list)
    detail: str = ""
```

In `packages/core/src/fra_core/schemas/__init__.py`, import it (`from fra_core.schemas.check import CheckResult`) and add `"CheckResult"` to `__all__` in its alphabetical place.

Append to `packages/ingest/src/fra_ingest/results.py` (add `Statement` to its `fra_core.schemas` import):

```python
class TableDecision(BaseModel):
    """What structure decided about one docling table, and why."""

    model_config = ConfigDict(extra="forbid")

    table_ref: str
    docling_path: str
    page_no: int = Field(ge=1)
    type: StatementType | None
    confidence: float = Field(ge=0.0, le=1.0)
    statement_id: str | None = None
    evidence: list[str] = Field(default_factory=list)


class StructureResult(BaseModel):
    """Output of the structure stage, written to ``<artifact root>/<sha256>/statements.raw.json``."""

    model_config = ConfigDict(extra="forbid")

    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    convert_version: str
    settings_hash: str
    statements: list[Statement] = Field(default_factory=list)
    tables: list[TableDecision] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
```

`packages/ingest/src/fra_ingest/label_match.py`:

```python
"""Label comparison that ignores word boundaries.

Some Arabic text layers lose the spaces between words (spec 11, Almarai AR), so labels are
compared with every space removed after ``normalize_label``, on both sides.
"""

from __future__ import annotations

from collections.abc import Iterable

from fra_core.labels import normalize_label
from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import CanonicalItem, Taxonomy

SUBTOTAL_CUES = ("total", "اجمالي", "مجموع")


def squash(text: str) -> str:
    return normalize_label(text).replace(" ", "")


def has_subtotal_cue(label: str) -> bool:
    squashed = squash(label)
    return any(squashed.startswith(cue) for cue in SUBTOTAL_CUES)


class LabelIndex:
    """Every alias of every taxonomy item, keyed by statement and squashed spelling."""

    def __init__(self, taxonomy: Taxonomy) -> None:
        self._index: dict[StatementType, dict[str, CanonicalItem]] = {}
        for item in taxonomy.items:
            for language in ("en", "ar"):
                for alias in item.aliases_for(language):
                    self._index.setdefault(item.statement, {})[squash(alias)] = item

    def match(self, label: str, statement: StatementType) -> CanonicalItem | None:
        key = squash(label)
        return self._index.get(statement, {}).get(key) if key else None

    def hits(self, labels: Iterable[str], statement: StatementType) -> int:
        return sum(1 for label in labels if self.match(label, statement) is not None)
```

`config.py`: add `("structure", "min_confidence"): "min_confidence",` to `_TOML_FIELDS` and to `IngestConfig`:

```python
    min_confidence: float = Field(default=0.5, gt=0.0, le=1.0)
```

`configs/ingest.toml`, before `[artifacts]`:

```toml
[structure]
# A table is a statement when its best statement type reaches this confidence (spec 11).
min_confidence = 0.5
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/core/tests/test_check_schema.py packages/ingest/tests/test_label_match.py packages/ingest/tests/test_structure_results.py packages/ingest/tests/test_config.py -q`
Expected: PASS. Do not change the taxonomy in this task.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/core/src/fra_core/schemas/check.py packages/core/src/fra_core/schemas/__init__.py packages/core/tests/test_check_schema.py packages/ingest/src/fra_ingest/results.py packages/ingest/src/fra_ingest/label_match.py packages/ingest/src/fra_ingest/config.py configs/ingest.toml packages/ingest/tests/test_label_match.py packages/ingest/tests/test_structure_results.py packages/ingest/tests/test_config.py
git commit -m "Add check results, structure result models and space-free label matching"
```

---

### Task 4: Table grid

**Files:**
- Create: `packages/ingest/src/fra_ingest/table_grid.py`
- Test: `packages/ingest/tests/test_table_grid.py`

**Interfaces:**
- Consumes: `DlTable`, `DlDocument` (Task 2).
- Produces: `GridCell(text, row, col, row_span, col_span, bbox: BBox | None, is_column_header, is_row_header, is_row_section, page_no, flags: tuple[str, ...])`; `Grid(table_ref, docling_path, page_no, page_width, num_rows, num_cols, cells: tuple[GridCell, ...])` with `cell(row, col) -> GridCell | None` (covering spans), `text(row, col) -> str`, `table_index -> str` (`"t3"` for `#/tables/3`), `right_edge() -> float | None`; `build_grid(table: DlTable, document: DlDocument, docling_path: str) -> Grid`.

- [ ] **Step 1: Write the failing tests**

```python
"""docling tables to grids (spec 11, Data flow step 2)."""

from fra_ingest.docling_json import DlDocument
from fra_ingest.table_grid import build_grid


def document(cells: list[dict[str, object]]) -> DlDocument:
    return DlDocument.model_validate(
        {
            "tables": [
                {
                    "self_ref": "#/tables/3",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {"l": 0, "t": 90, "r": 500, "b": 10, "coord_origin": "BOTTOMLEFT"},
                        }
                    ],
                    "data": {"num_rows": 2, "num_cols": 3, "table_cells": cells},
                }
            ],
            "pages": {"7": {"page_no": 7, "size": {"width": 600, "height": 800}}},
        }
    )


def cell(text: str, row: int, col: int, **extra: object) -> dict[str, object]:
    return {
        "text": text,
        "start_row_offset_idx": row,
        "end_row_offset_idx": row + 1,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col + 1,
        "bbox": {"l": 100 * col + 10, "t": 20 * row + 5, "r": 100 * col + 90, "b": 20 * row + 15, "coord_origin": "TOPLEFT"},
        **extra,
    }


def test_cells_keep_position_header_flags_and_boxes() -> None:
    doc = document(
        [
            cell("  2025  ", 0, 1, column_header=True),
            cell("Revenue", 1, 0, row_header=True),
            cell("1,000", 1, 1),
        ]
    )
    grid = build_grid(doc.tables[0], doc, "docling/p7-7.json")
    assert (grid.page_no, grid.page_width, grid.num_rows, grid.num_cols) == (7, 600, 2, 3)
    assert grid.table_index == "t3"
    header = grid.cell(0, 1)
    assert header is not None and header.text == "2025" and header.is_column_header
    value = grid.cell(1, 1)
    assert value is not None and value.bbox is not None and value.bbox.left == 110
    assert grid.text(1, 0) == "Revenue"
    assert grid.cell(0, 0) is None and grid.text(0, 0) == ""
    assert grid.right_edge() == 190


def test_a_spanning_cell_covers_its_columns() -> None:
    doc = document([cell("For the year ended", 0, 1, col_span=2)])
    grid = build_grid(doc.tables[0], doc, "docling/p7-7.json")
    assert grid.text(0, 1) == grid.text(0, 2) == "For the year ended"


def test_a_cell_without_a_box_is_kept() -> None:
    raw = cell("12", 1, 2)
    raw.pop("bbox")
    doc = document([raw])
    grid = build_grid(doc.tables[0], doc, "docling/p7-7.json")
    kept = grid.cell(1, 2)
    assert kept is not None and kept.bbox is None
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_table_grid.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.table_grid'`.

- [ ] **Step 3: Implement `table_grid.py`**

```python
"""A docling table as a grid of cells with boxes in PDF points, top-left origin (spec 11)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from fra_core.schemas import BBox
from fra_ingest.docling_json import DlDocument, DlTable


class GridCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    bbox: BBox | None
    is_column_header: bool = False
    is_row_header: bool = False
    is_row_section: bool = False
    page_no: int
    flags: tuple[str, ...] = ()


class Grid(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_ref: str
    docling_path: str
    page_no: int
    page_width: float
    num_rows: int
    num_cols: int
    cells: tuple[GridCell, ...]

    def cell(self, row: int, col: int) -> GridCell | None:
        for c in self.cells:
            if c.row <= row < c.row + c.row_span and c.col <= col < c.col + c.col_span:
                return c
        return None

    def text(self, row: int, col: int) -> str:
        found = self.cell(row, col)
        return found.text if found else ""

    @property
    def table_index(self) -> str:
        return "t" + self.table_ref.rsplit("/", 1)[-1]

    def right_edge(self) -> float | None:
        rights = [c.bbox.right for c in self.cells if c.bbox is not None]
        return max(rights) if rights else None


def build_grid(table: DlTable, document: DlDocument, docling_path: str) -> Grid:
    page_no = table.prov[0].page_no
    size = document.page_size(page_no)
    cells = tuple(
        GridCell(
            text=" ".join(c.text.split()),
            row=c.start_row_offset_idx,
            col=c.start_col_offset_idx,
            row_span=max(1, c.row_span),
            col_span=max(1, c.col_span),
            bbox=c.bbox.to_bbox(size.height) if c.bbox is not None else None,
            is_column_header=c.column_header,
            is_row_header=c.row_header,
            is_row_section=c.row_section,
            page_no=page_no,
        )
        for c in table.data.table_cells
    )
    return Grid(
        table_ref=table.self_ref,
        docling_path=docling_path,
        page_no=page_no,
        page_width=size.width,
        num_rows=table.data.num_rows,
        num_cols=table.data.num_cols,
        cells=cells,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_table_grid.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/table_grid.py packages/ingest/tests/test_table_grid.py
git commit -m "Build grids of cells with top-left boxes from docling tables"
```

---

### Task 5: Visual-order repair

**Files:**
- Create: `packages/ingest/src/fra_ingest/visual_order.py`
- Test: `packages/ingest/tests/test_visual_order.py`

**Interfaces:**
- Consumes: `Grid`, `GridCell` (Task 4).
- Produces: `restore_digits(text) -> str`, `is_letter_spaced(text) -> bool`, `join_spaced_letters(text) -> str`, `repair_text(text, *, visual: bool) -> str`, `repair_grid(grid, *, visual: bool) -> Grid` (cells gain flags `digits_reversed`, `letters_spaced`).

- [ ] **Step 1: Write the failing tests**

```python
"""Visual-order Arabic repair (spec 11, Data flow step 3; measured on Almarai AR)."""

from fra_core.numbers import parse_number
from fra_core.periods import parse_period
from fra_ingest.table_grid import Grid, GridCell
from fra_ingest.visual_order import (
    is_letter_spaced,
    join_spaced_letters,
    repair_grid,
    repair_text,
    restore_digits,
)


def test_a_reversed_number_is_restored() -> None:
    assert parse_number(restore_digits("٢٤٣,٠٥٧,٢٢")).value == 22750342
    assert restore_digits("٠١") == "١٠"


def test_a_reversed_negative_with_spaced_brackets_is_restored() -> None:
    restored = restore_digits(") ٣٦٠,٧٧١,٥١ (")
    assert parse_number(restored).value == -15177063


def test_numbers_inside_text_are_restored_token_by_token() -> None:
    assert restore_digits("إ ي ر ا د ا ت ٣٣") == "إ ي ر ا د ا ت ٣٣"
    assert restore_digits("۱۳ د ي س م ب ر ٥٢٠٢") == "۳۱ د ي س م ب ر ٢٠٢٥"


def test_a_dash_is_left_alone() -> None:
    assert restore_digits("-") == "-"


def test_spaced_letters_are_joined_and_other_tokens_kept_apart() -> None:
    assert is_letter_spaced("إ ي ض ا ح ا ت")
    assert join_spaced_letters("إ ي ض ا ح ا ت") == "إيضاحات"
    assert join_spaced_letters("۳۱ د ي س م ب ر ٢٠٢٥ م ب آ لا ف X") == "۳۱ ديسمبر ٢٠٢٥ مبآلاف X"
    assert join_spaced_letters("ذ م م م د ي ن ة م ق د ًم ا") == "ذمممدينةمقدًما"
    assert not is_letter_spaced("Property, Plant and Equipment")
    assert join_spaced_letters("شركة جهينة للصناعات") == "شركة جهينة للصناعات"


def test_the_almarai_header_parses_after_repair() -> None:
    text = repair_text("۱۳ د ي س م ب ر ٥٢٠٢ م ب آ لا ف X", visual=True)
    period = parse_period(text)
    assert period is not None and period.key == "2025-12-31"


def test_digits_are_only_reversed_on_visual_pages() -> None:
    assert repair_text("٢٢,٧٥٠,٣٤٢", visual=False) == "٢٢,٧٥٠,٣٤٢"


def test_a_repaired_grid_flags_what_changed() -> None:
    grid = Grid(
        table_ref="#/tables/0",
        docling_path="docling/p156-156.json",
        page_no=156,
        page_width=793.7,
        num_rows=1,
        num_cols=2,
        cells=(
            GridCell(text="٢٤٣,٠٥٧,٢٢", row=0, col=0, bbox=None, page_no=156),
            GridCell(text="م و ج و د ا ت ح ي و ي ة", row=0, col=1, bbox=None, page_no=156),
        ),
    )
    repaired = repair_grid(grid, visual=True)
    number, label = repaired.cells
    assert number.text == "٢٢,٧٥٠,٣٤٢" and number.flags == ("digits_reversed",)
    assert label.text == "موجوداتحيوية" and label.flags == ("letters_spaced",)
    assert repair_grid(grid, visual=False).cells[0].flags == ()
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_visual_order.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.visual_order'`.

- [ ] **Step 3: Implement `visual_order.py`**

```python
"""Repair of Arabic text that docling returns in visual order (spec 11, Data flow step 3).

Measured on Almarai AR: numbers come out with their digits reversed, brackets included, and
letters come out in reading order but separated by single spaces. Digits are reversed back
only on pages Part 1 marked ``visual_arabic``; spaced letters are joined wherever they occur.
"""

from __future__ import annotations

import re

from fra_ingest.table_grid import Grid

_DIGIT = "0-9٠-٩۰-۹"
_SEPARATORS = r",.٫٬/"
# A cell made only of digits, separators, brackets, minus signs and spaces is reversed whole,
# which also puts reversed brackets back in order.
_NUMERIC_CELL = re.compile(rf"^[{_DIGIT}{_SEPARATORS}()\-\s]*[{_DIGIT}][{_DIGIT}{_SEPARATORS}()\-\s]*$")
_NUMBER_TOKEN = re.compile(rf"^[{_DIGIT}][{_DIGIT}{_SEPARATORS}]*$")
_DIACRITICS = frozenset("ًٌٍَُِّْٰ")
_ARABIC_LETTER = re.compile(r"^[ء-يٱ-ۓ]$")
_LAM_ALEF = frozenset({"لا", "لأ", "لإ", "لآ"})


def restore_digits(text: str) -> str:
    if _NUMERIC_CELL.match(text):
        return text[::-1]
    return " ".join(t[::-1] if _NUMBER_TOKEN.match(t) else t for t in text.split(" "))


def _is_letter(token: str) -> bool:
    core = "".join(c for c in token if c not in _DIACRITICS)
    return bool(_ARABIC_LETTER.match(core)) or core in _LAM_ALEF


def is_letter_spaced(text: str) -> bool:
    tokens = text.split()
    letters = sum(1 for t in tokens if _is_letter(t))
    return letters >= 3 and letters >= 0.6 * len(tokens)


def join_spaced_letters(text: str) -> str:
    if not is_letter_spaced(text):
        return text
    out: list[str] = []
    run: list[str] = []
    for token in text.split():
        if _is_letter(token):
            run.append(token)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(token)
    if run:
        out.append("".join(run))
    return " ".join(out)


def repair_text(text: str, *, visual: bool) -> str:
    return join_spaced_letters(restore_digits(text) if visual else text)


def repair_grid(grid: Grid, *, visual: bool) -> Grid:
    cells = []
    for cell in grid.cells:
        flags = list(cell.flags)
        text = restore_digits(cell.text) if visual else cell.text
        if text != cell.text:
            flags.append("digits_reversed")
        joined = join_spaced_letters(text)
        if joined != text:
            flags.append("letters_spaced")
        cells.append(cell.model_copy(update={"text": joined, "flags": tuple(flags)}))
    return grid.model_copy(update={"cells": tuple(cells)})
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_visual_order.py -q`
Expected: PASS. If `restore_digits("۱۳ د ي س م ب ر ٥٢٠٢")` gives a different digit set than the test expects (the two Arabic digit ranges are kept as they are, only reversed), keep the code and fix the test's expected string to the reversed input; the period test is the one that matters.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/visual_order.py packages/ingest/tests/test_visual_order.py
git commit -m "Restore reversed digits on visual Arabic pages and join spaced letters"
```

---

### Task 6: Scale, currency, entity and consolidation

**Files:**
- Create: `packages/ingest/src/fra_ingest/metadata.py`
- Test: `packages/ingest/tests/test_metadata.py`

**Interfaces:**
- Produces: `Metadata(scale: int | None, currency: str | None, entity_name: str | None, consolidated: bool | None, signals: list[str], conflict: bool, flags: list[str])`; `detect_metadata(*, header_text: str, context_texts: Sequence[str], document_texts: Sequence[str]) -> Metadata`. Flags: `scale_conflict`, `scale_missing`, `currency_conflict`, `currency_inferred`, `currency_missing`.

- [ ] **Step 1: Write the failing tests**

```python
"""Scale, currency, entity and consolidation (spec 11, Data flow step 6)."""

from fra_ingest.metadata import detect_metadata


def test_header_scale_and_currency() -> None:
    meta = detect_metadata(header_text="2025 EGP '000", context_texts=[], document_texts=[])
    assert (meta.scale, meta.currency, meta.flags) == (1000, "EGP", [])


def test_arabic_thousands_in_a_joined_run() -> None:
    meta = detect_metadata(
        header_text="31 ديسمبر 2025 مبآلاف X",
        context_texts=["بآلافالريالاتالسعودية"],
        document_texts=[],
    )
    assert (meta.scale, meta.currency) == (1000, "SAR")


def test_a_glyph_currency_is_inferred_from_the_document() -> None:
    meta = detect_metadata(
        header_text="31 December 2025 X '000",
        context_texts=["Statement of financial position"],
        document_texts=["Amounts in Saudi Riyals", "SAR 2,000", "paid in US dollars"],
    )
    assert meta.currency == "SAR"
    assert "currency_inferred" in meta.flags


def test_missing_values_are_flagged() -> None:
    meta = detect_metadata(header_text="2025", context_texts=[], document_texts=[])
    assert meta.scale is None and meta.currency is None
    assert {"scale_missing", "currency_missing"} <= set(meta.flags)


def test_disagreeing_signals_are_flagged_and_the_header_wins() -> None:
    meta = detect_metadata(
        header_text="EGP '000",
        context_texts=["In millions of Egyptian pounds"],
        document_texts=[],
    )
    assert meta.scale == 1000 and meta.conflict
    assert "scale_conflict" in meta.flags


def test_entity_and_consolidation_from_headings() -> None:
    meta = detect_metadata(
        header_text="",
        context_texts=["Juhayna Food Industries Company", "Consolidated statement of financial position"],
        document_texts=[],
    )
    assert meta.entity_name == "Juhayna Food Industries Company"
    assert meta.consolidated is True
    arabic = detect_metadata(
        header_text="", context_texts=["القوائمالماليةالموحدة"], document_texts=[]
    )
    assert arabic.consolidated is True
    standalone = detect_metadata(
        header_text="", context_texts=["Standalone statement of profit or loss"], document_texts=[]
    )
    assert standalone.consolidated is False
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_metadata.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.metadata'`.

- [ ] **Step 3: Implement `metadata.py`**

```python
"""Scale, currency, entity and consolidation for one statement (spec 11, Data flow step 6).

Each is read from the header first, then from the text around the table. Currency alone falls
back to the currency named most often in the document, flagged ``currency_inferred``, because
some filings print the currency as a font glyph (Almarai's riyal sign extracts as ``X``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from fra_core.units import detect_currency, detect_scale
from fra_ingest.label_match import squash

_CONSOLIDATED = ("consolidated", "المجمع", "الموحد")
_STANDALONE = ("standalone", "separate", "المستقل", "المنفصل")
_ENTITY = ("company", "corporation", "s.a.e", "plc", "limited", "ltd", "شركة")


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scale: int | None = None
    currency: str | None = None
    entity_name: str | None = None
    consolidated: bool | None = None
    signals: list[str] = Field(default_factory=list)
    conflict: bool = False
    flags: list[str] = Field(default_factory=list)


def detect_metadata(
    *, header_text: str, context_texts: Sequence[str], document_texts: Sequence[str]
) -> Metadata:
    meta = Metadata()

    header_scale = detect_scale(header_text)
    context_scale = next((s for t in context_texts if (s := detect_scale(t)) is not None), None)
    if header_scale is not None:
        meta.scale = header_scale.scale
        meta.signals.append("scale:header")
        if context_scale is not None and context_scale.scale != header_scale.scale:
            meta.conflict = True
            meta.flags.append("scale_conflict")
    elif context_scale is not None:
        meta.scale = context_scale.scale
        meta.signals.append("scale:context")
    else:
        meta.flags.append("scale_missing")

    header_currency = detect_currency(header_text)
    context_currency = next((c for t in context_texts if (c := detect_currency(t))), None)
    if header_currency is not None:
        meta.currency = header_currency
        meta.signals.append("currency:header")
        if context_currency is not None and context_currency != header_currency:
            meta.conflict = True
            meta.flags.append("currency_conflict")
    elif context_currency is not None:
        meta.currency = context_currency
        meta.signals.append("currency:context")
    else:
        counts = Counter(c for t in document_texts if (c := detect_currency(t)))
        if counts:
            meta.currency = counts.most_common(1)[0][0]
            meta.signals.append("currency:document")
            meta.flags.append("currency_inferred")
        else:
            meta.flags.append("currency_missing")

    for text in context_texts:
        lowered = text.casefold()
        if meta.entity_name is None and any(marker in lowered for marker in _ENTITY):
            meta.entity_name = " ".join(text.split())
        squashed = squash(text)
        if meta.consolidated is None and any(squash(m) in squashed for m in _CONSOLIDATED):
            meta.consolidated = True
        elif meta.consolidated is None and any(squash(m) in squashed for m in _STANDALONE):
            meta.consolidated = False
    return meta
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_metadata.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/metadata.py packages/ingest/tests/test_metadata.py
git commit -m "Read scale, currency, entity and consolidation for a statement"
```

---

### Task 7: Header rows bound to periods

**Files:**
- Create: `packages/ingest/src/fra_ingest/header.py`
- Test: `packages/ingest/tests/test_header.py`

**Interfaces:**
- Consumes: `Grid`, `GridCell` (Task 4), `squash` (Task 3).
- Produces: `is_amount(text) -> bool`, `is_note_ref(text) -> bool`, `split_note(label) -> tuple[str, str | None]`, `HeaderLayout(header_rows: list[int], label_col: int | None, note_col: int | None, value_cols: dict[int, Period], unbound_cols: list[int], header_texts: dict[int, str], evidence: list[str])` with `data_rows(grid) -> list[int]`, and `parse_header(grid, statement_type, date_hint: str | None) -> HeaderLayout`.

- [ ] **Step 1: Write the failing tests**

```python
"""Header rows bound to periods (spec 11, Data flow step 5)."""

from fra_core.schemas import StatementType
from fra_ingest.header import is_amount, is_note_ref, parse_header, split_note
from fra_ingest.table_grid import Grid, GridCell

BALANCE, INCOME = StatementType.BALANCE, StatementType.INCOME


def grid(rows: list[list[str]], header_rows: int = 0) -> Grid:
    cells = tuple(
        GridCell(text=text, row=r, col=c, bbox=None, page_no=1, is_column_header=r < header_rows)
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
        if text
    )
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p1-1.json",
        page_no=1,
        page_width=600,
        num_rows=len(rows),
        num_cols=max(len(r) for r in rows),
        cells=cells,
    )


def test_amounts_dates_years_and_note_references() -> None:
    assert is_amount("26,058,632") and is_amount("(15,177,063)") and is_amount("٢٢,٧٥٠,٣٤٢")
    assert is_amount("5") and is_amount("-")
    assert not is_amount("31 December 2025") and not is_amount("2025") and not is_amount("Notes")
    assert not is_amount("(13-2)") and not is_amount("")
    assert is_note_ref("7") and is_note_ref("(13-2)") and is_note_ref("١٠")
    assert not is_note_ref("26,058,632") and not is_note_ref("2025")


def test_a_trailing_note_reference_is_split_from_the_label() -> None:
    assert split_note("إيرادات ٣٣") == ("إيرادات", "٣٣")
    assert split_note("Zakat for 2025") == ("Zakat for 2025", None)
    assert split_note("Revenue") == ("Revenue", None)


def test_an_english_balance_sheet_header() -> None:
    layout = parse_header(
        grid(
            [
                ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
                ["ASSETS", "", "", ""],
                ["Property, Plant and Equipment", "7", "26,058,632", "22,750,342"],
                ["Investments", "12", "-", "3,256"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0]
    assert (layout.label_col, layout.note_col) == (0, 1)
    assert {c: p.key for c, p in layout.value_cols.items()} == {2: "2025-12-31", 3: "2024-12-31"}
    assert layout.unbound_cols == []


def test_a_two_row_header_is_joined_per_column() -> None:
    layout = parse_header(
        grid(
            [
                ["", "", "For the year ended", "For the year ended"],
                ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
                ["Revenue", "33", "22,064,876", "20,979,512"],
            ],
            header_rows=1,
        ),
        INCOME,
        None,
    )
    assert layout.header_rows == [0, 1]
    assert {c: p.key for c, p in layout.value_cols.items()} == {2: "FY2025", 3: "FY2024"}


def test_a_mirrored_arabic_table_finds_its_label_column_on_the_right() -> None:
    layout = parse_header(
        grid(
            [
                ["31 ديسمبر 2024 مبآلاف X", "31 ديسمبر 2025 مبآلاف X", "إيضاحات", ""],
                ["22,750,342", "26,058,632", "7", "ممتلكاتوآلاتومعدات"],
                ["525,391", "492,514", "8", "مصاريفمدفوعةمقدما"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert (layout.label_col, layout.note_col) == (3, 2)
    assert {c: p.key for c, p in layout.value_cols.items()} == {0: "2024-12-31", 1: "2025-12-31"}


def test_a_year_only_header_takes_day_and_month_from_the_date_line() -> None:
    layout = parse_header(
        grid([["", "2025", "2024"], ["Revenue", "100", "90"]], header_rows=1),
        INCOME,
        "For the year ended 30 June 2025",
    )
    assert {p.end_date.isoformat() for p in layout.value_cols.values()} == {
        "2025-06-30",
        "2024-06-30",
    }


def test_a_value_column_without_a_date_is_unbound() -> None:
    layout = parse_header(
        grid(
            [["", "31 December 2025", "بآلاف X"], ["Revenue", "100", "90"]],
            header_rows=1,
        ),
        INCOME,
        None,
    )
    assert list(layout.value_cols) == [1]
    assert layout.unbound_cols == [2]
    assert "period_unbound:2" in layout.evidence


def test_a_restated_column_keeps_its_marker() -> None:
    layout = parse_header(
        grid([["", "2025 EGP", "2024 (Restated) EGP"], ["Cash", "5", "4"]], header_rows=1),
        BALANCE,
        None,
    )
    assert [p.restated for p in layout.value_cols.values()] == [False, True]


def test_a_note_column_without_a_header_is_found_next_to_the_labels() -> None:
    layout = parse_header(
        grid(
            [
                ["", "", "2025", "2024"],
                ["Property, plant and equipment", "(14)", "5 175 562 196", "3 886 899 018"],
                ["Biological assets", "(16) - (17-1)", "574 493 531", "445 704 631"],
                ["Goodwill", "(35)", "97 092 890", "97 092 890"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.note_col == 1
    assert sorted(layout.value_cols) == [2, 3]


def test_small_values_stay_value_columns() -> None:
    layout = parse_header(
        grid([["", "Notes", "2025", "2024"], ["Inventories", "19", "10", "8"], ["Cash", "21", "5", "4"]], header_rows=1),
        BALANCE,
        None,
    )
    assert layout.note_col == 1 and sorted(layout.value_cols) == [2, 3]


def test_a_section_row_above_the_first_amount_is_not_a_header() -> None:
    layout = parse_header(
        grid(
            [
                ["", "Notes", "31 December 2025", "31 December 2024"],
                ["Non-Current Assets", "", "", ""],
                ["Property, plant and equipment", "7", "26,058,632", "22,750,342"],
            ],
            header_rows=1,
        ),
        BALANCE,
        None,
    )
    assert layout.header_rows == [0]


def test_a_table_with_no_amounts_has_no_value_columns() -> None:
    layout = parse_header(
        grid([["Danko Maras", "Fawaz Bin Mohammed", "Prince Naif"], ["CFO", "CEO", "Chairman"]]),
        BALANCE,
        None,
    )
    assert layout.value_cols == {} and layout.unbound_cols == []
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_header.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.header'`.

- [ ] **Step 3: Implement `header.py`**

```python
"""Header rows to periods, the label column and the note column (spec 11, Data flow step 5).

Columns are bound to periods by what their header says, never by position, so a mirrored
Arabic table needs no special case: the label column is simply the one holding the most text.
"""

from __future__ import annotations

import calendar
import re

from pydantic import BaseModel, ConfigDict, Field

from fra_core.numbers import normalize_digits, parse_number
from fra_core.periods import parse_period
from fra_core.schemas import Period, PeriodKind, StatementType
from fra_ingest.label_match import squash
from fra_ingest.table_grid import Grid

_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_NOTE = re.compile(
    r"^\(?\d{1,2}(\s*[-.]\s*\d{1,2})?\)?(\s*[-,]\s*\(?\d{1,2}(\s*[-.]\s*\d{1,2})?\)?)*$"
)
_NOTE_HEADERS = frozenset(squash(w) for w in ("note", "notes", "إيضاح", "إيضاحات"))
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_NOT_A_MONTH = re.compile(
    r"restated|audited|unaudited|\b[a-z]{3}\b|[\u0600-\u06ff]+|['()\s.,]", re.IGNORECASE
)
_DURATION_TYPES = frozenset(
    {
        StatementType.INCOME,
        StatementType.COMPREHENSIVE_INCOME,
        StatementType.CASH_FLOW,
        StatementType.EQUITY,
    }
)


def _plain(text: str) -> str:
    return normalize_digits(text)[0].strip()


def is_note_ref(text: str) -> bool:
    return bool(text) and _NOTE.match(_plain(text)) is not None


def is_amount(text: str) -> bool:
    """A value: parses as a number and is neither a date nor a bare year."""
    if not text or parse_period(text) is not None or _YEAR.fullmatch(_plain(text)):
        return False
    return parse_number(text).value is not None


def split_note(label: str) -> tuple[str, str | None]:
    """A label ending in a note reference, as Almarai AR prints them: (label, note)."""
    head, _, tail = label.rpartition(" ")
    if head and is_note_ref(tail) and not _YEAR.fullmatch(_plain(tail)):
        return head, tail
    return label, None


class HeaderLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header_rows: list[int] = Field(default_factory=list)
    label_col: int | None = None
    note_col: int | None = None
    value_cols: dict[int, Period] = Field(default_factory=dict)
    unbound_cols: list[int] = Field(default_factory=list)
    header_texts: dict[int, str] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)

    def data_rows(self, grid: Grid) -> list[int]:
        return [r for r in range(grid.num_rows) if r not in self.header_rows]


def _row_has_amount(grid: Grid, row: int) -> bool:
    return any(is_amount(grid.text(row, col)) for col in range(grid.num_cols))


def _is_header_text(text: str) -> bool:
    return parse_period(text) is not None or squash(text) in _NOTE_HEADERS


def _header_rows(grid: Grid) -> list[int]:
    """Rows docling flags as column headers, plus rows above the first amount that carry a
    date or a notes heading. A section row such as "Non-current assets" is not a header."""
    rows = {c.row for c in grid.cells if c.is_column_header}
    for row in range(grid.num_rows):
        if _row_has_amount(grid, row):
            break
        if any(_is_header_text(grid.text(row, col)) for col in range(grid.num_cols)):
            rows.add(row)
    return sorted(r for r in rows if not _row_has_amount(grid, r))


def _year_only(header: str) -> bool:
    return bool(_YEAR.search(_plain(header))) and not _LETTER.search(
        _NOT_A_MONTH.sub("", _YEAR.sub("", _plain(header)))
    )


def _with_date_hint(period: Period, header: str, hint: Period | None) -> Period:
    """A header naming only a year takes the day and month of the statement's date line."""
    if hint is None or not _year_only(header):
        return period
    month = calendar.month_name[hint.end_date.month]
    text = f"{hint.end_date.day} {month} {period.end_date.year}"
    if period.kind is PeriodKind.DURATION:
        text = f"For the year ended {text}"
    moved = parse_period(text, default_kind=period.kind)
    return moved.model_copy(update={"restated": period.restated}) if moved else period


def parse_header(grid: Grid, statement_type: StatementType, date_hint: str | None) -> HeaderLayout:
    layout = HeaderLayout(header_rows=_header_rows(grid))
    data = layout.data_rows(grid)
    kind = PeriodKind.DURATION if statement_type in _DURATION_TYPES else PeriodKind.INSTANT
    hint = parse_period(date_hint, default_kind=kind) if date_hint else None

    for col in range(grid.num_cols):
        parts = [grid.text(r, col) for r in layout.header_rows]
        joined = " ".join(p for i, p in enumerate(parts) if p and p not in parts[:i])
        if joined:
            layout.header_texts[col] = joined

    texts = {
        col: sum(1 for r in data if _LETTER.search(grid.text(r, col)) and not is_amount(grid.text(r, col)))
        for col in range(grid.num_cols)
    }
    amounts = {col: sum(1 for r in data if is_amount(grid.text(r, col))) for col in range(grid.num_cols)}
    if any(texts.values()):
        layout.label_col = max(texts, key=lambda c: (texts[c], -amounts[c]))

    others = [c for c in range(grid.num_cols) if c != layout.label_col]
    named = [c for c in others if squash(layout.header_texts.get(c, "")) in _NOTE_HEADERS]
    if named:
        layout.note_col = named[0]
    elif layout.label_col is not None:
        amount_cols = [c for c in others if amounts[c]]
        for col in (layout.label_col - 1, layout.label_col + 1):
            cells = [grid.text(r, col) for r in data if grid.text(r, col)]
            if len(cells) >= 2 and all(is_note_ref(t) for t in cells) and len(amount_cols) >= 3:
                layout.note_col = col
                break

    for col in others:
        if col == layout.note_col or amounts[col] == 0:
            continue
        header = layout.header_texts.get(col, "")
        period = parse_period(header, default_kind=kind) if header else None
        if period is None:
            layout.unbound_cols.append(col)
            layout.evidence.append(f"period_unbound:{col}")
            continue
        period = _with_date_hint(period, header, hint)
        if any(p.key == period.key for p in layout.value_cols.values()):
            layout.evidence.append(f"duplicate_period:{col}:{period.key}")
            continue
        layout.value_cols[col] = period
    return layout
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_header.py -q`
Expected: PASS. Two rules are easy to get wrong and are what the tests pin: a note column without a header is recognised only next to the label column and only when at least three other columns hold amounts (a note column and two periods), so small values never become notes; and a header row above the first amount must carry a date or a notes heading, so section rows stay data rows. A year-only header is moved to the date line's day and month by re-parsing a written-out date, so the period key follows `parse_period`'s own rules.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/header.py packages/ingest/tests/test_header.py
git commit -m "Bind header columns to periods and find the label and note columns"
```

---

### Task 8: Classification

**Files:**
- Create: `packages/ingest/src/fra_ingest/classify.py`
- Test: `packages/ingest/tests/test_classify.py`

**Interfaces:**
- Consumes: `Grid` (Task 4), `HeaderLayout` (Task 7), `LabelIndex` (Task 3).
- Produces: `TableContext(title_types: tuple[StatementType, ...], cue_types: tuple[StatementType, ...], heading_texts: tuple[str, ...], industry_flags: tuple[str, ...])`, `Classification(type: StatementType | None, confidence: float, evidence: list[str], industry_flags: list[str])`, `classify(grid, layout, context, index, min_confidence) -> Classification`.

- [ ] **Step 1: Write the failing tests**

```python
"""Tables to statement types (spec 11, Data flow step 4)."""

from fra_core.schemas import StatementType
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.classify import TableContext, classify
from fra_ingest.header import parse_header
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_grid import Grid, GridCell

INDEX = LabelIndex(load_taxonomy())
BALANCE, INCOME = StatementType.BALANCE, StatementType.INCOME


def grid(rows: list[list[str]]) -> Grid:
    cells = tuple(
        GridCell(text=t, row=r, col=c, bbox=None, page_no=1, is_column_header=r == 0)
        for r, row in enumerate(rows)
        for c, t in enumerate(row)
        if t
    )
    return Grid(table_ref="#/tables/0", docling_path="d", page_no=1, page_width=600, num_rows=len(rows), num_cols=4, cells=cells)


def run(rows: list[list[str]], context: TableContext) -> StatementType | None:
    g = grid(rows)
    return classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5).type


NO_TITLE = TableContext(title_types=(), cue_types=(), heading_texts=(), industry_flags=())
BALANCE_PAGE = TableContext(title_types=(BALANCE,), cue_types=(), heading_texts=(), industry_flags=())
HEADER = ["", "Notes", "2025", "2024"]


def test_an_income_statement_by_its_labels() -> None:
    rows = [HEADER, ["Revenue", "33", "100", "90"], ["Cost of sales", "27", "(60)", "(50)"], ["Gross profit", "", "40", "40"], ["Finance costs", "", "(5)", "(4)"]]
    assert run(rows, NO_TITLE) is INCOME


def test_a_balance_sheet_by_labels_and_page_title() -> None:
    rows = [HEADER, ["Inventories", "19", "5", "4"], ["Total assets", "", "50", "40"]]
    assert run(rows, BALANCE_PAGE) is BALANCE


def test_arabic_labels_without_word_boundaries_count() -> None:
    rows = [HEADER, ["الإيرادات", "", "100", "90"], ["تكلفةالمبيعات", "", "(60)", "(50)"], ["مجملالربح", "", "40", "40"], ["تكاليف التمويل", "", "(5)", "(4)"]]
    assert run(rows, NO_TITLE) is INCOME


def test_a_table_without_a_period_header_is_not_a_statement() -> None:
    rows = [["Danko Maras", "Fawaz", "Prince Naif", ""], ["CFO", "CEO", "Chairman", ""]]
    g = grid(rows)
    result = classify(g, parse_header(g, BALANCE, None), BALANCE_PAGE, INDEX, 0.5)
    assert result.type is None and "no_period_header" in result.evidence


def test_a_notes_table_under_a_note_heading_is_rejected() -> None:
    rows = [HEADER, ["Inventories", "", "5", "4"], ["Total assets", "", "50", "40"], ["Revenue", "", "1", "1"]]
    context = TableContext(title_types=(BALANCE,), cue_types=(), heading_texts=("12. Property, plant and equipment",), industry_flags=())
    g = grid(rows)
    note_heading = TableContext(title_types=(BALANCE,), cue_types=(), heading_texts=("Note 12 Property, plant and equipment",), industry_flags=())
    assert classify(g, parse_header(g, BALANCE, None), note_heading, INDEX, 0.5).type is None
    assert classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5).type is BALANCE


def test_industry_flags_pass_through() -> None:
    rows = [HEADER, ["Inventories", "19", "5", "4"], ["Total assets", "", "50", "40"]]
    context = TableContext(title_types=(BALANCE,), cue_types=(), heading_texts=(), industry_flags=("likely_bank",))
    g = grid(rows)
    assert classify(g, parse_header(g, BALANCE, None), context, INDEX, 0.5).industry_flags == ["likely_bank"]
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_classify.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.classify'`.

- [ ] **Step 3: Implement `classify.py`**

```python
"""Which statement a table is, if any (spec 11, Data flow step 4).

Evidence: row labels found in the taxonomy (space-free, so Arabic labels without word
boundaries count), the statement titles and cues locate found on the page, and whether the
header binds periods. A table under a note heading, or without a period header, is not a
statement. Every decision keeps its evidence.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import StatementType
from fra_ingest.header import HeaderLayout
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_grid import Grid

_NOTE_HEADING = re.compile(r"^\s*(note|notes|إيضاح|ايضاح)\s*[\d٠-٩]", re.IGNORECASE)
_LABEL_WEIGHT = 0.6
_LABEL_SATURATION = 6
_TITLE_WEIGHT = 0.3
_CUE_WEIGHT = 0.15
_PERIOD_WEIGHT = 0.1


class TableContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title_types: tuple[StatementType, ...] = ()
    cue_types: tuple[StatementType, ...] = ()
    heading_texts: tuple[str, ...] = ()
    industry_flags: tuple[str, ...] = ()


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StatementType | None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    industry_flags: list[str] = Field(default_factory=list)


def classify(
    grid: Grid,
    layout: HeaderLayout,
    context: TableContext,
    index: LabelIndex,
    min_confidence: float,
) -> Classification:
    flags = list(context.industry_flags)
    if not layout.value_cols:
        return Classification(type=None, evidence=["no_period_header"], industry_flags=flags)
    if any(_NOTE_HEADING.match(text) for text in context.heading_texts):
        return Classification(type=None, evidence=["note_heading"], industry_flags=flags)

    labels = (
        [grid.text(r, layout.label_col) for r in layout.data_rows(grid)]
        if layout.label_col is not None
        else []
    )
    scores: dict[StatementType, float] = {}
    evidence: list[str] = []
    for statement in StatementType:
        hits = index.hits(labels, statement)
        score = _LABEL_WEIGHT * min(hits, _LABEL_SATURATION) / _LABEL_SATURATION + _PERIOD_WEIGHT
        if statement in context.title_types:
            score += _TITLE_WEIGHT
        elif statement in context.cue_types:
            score += _CUE_WEIGHT
        scores[statement] = round(score, 3)
        evidence.append(f"{statement.value}:hits={hits}:score={score:.2f}")

    best = max(scores, key=lambda s: scores[s])
    if scores[best] < min_confidence:
        return Classification(type=None, confidence=scores[best], evidence=[*evidence, "below_confidence"], industry_flags=flags)
    return Classification(type=best, confidence=min(scores[best], 1.0), evidence=evidence, industry_flags=flags)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_classify.py -q`
Expected: PASS. The Arabic test relies on the taxonomy's Arabic aliases; if one of its labels is not an alias in `packages/core/src/fra_core/taxonomy/canonical_items.yaml`, replace it with an alias of the same income item from that file (joined without spaces), never by adding aliases in this task.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/classify.py packages/ingest/tests/test_classify.py
git commit -m "Classify tables into statement types from labels, titles and headers"
```

---

### Task 9: Light hierarchy

**Files:**
- Create: `packages/ingest/src/fra_ingest/hierarchy.py`
- Test: `packages/ingest/tests/test_hierarchy.py`

**Interfaces:**
- Produces: `RowInput(row: int, label: str, indent: float | None, has_values: bool, subtotal_hint: bool)`, `RowNode(row, depth, parent_row: int | None, is_subtotal, is_section)`, `infer_hierarchy(rows: Sequence[RowInput]) -> list[RowNode]`.

- [ ] **Step 1: Write the failing tests**

```python
"""Depth, sections and subtotal cues (spec 11, Data flow step 9)."""

from fra_ingest.hierarchy import RowInput, infer_hierarchy


def row(n: int, label: str, indent: float | None, values: bool = True, hint: bool = False) -> RowInput:
    return RowInput(row=n, label=label, indent=indent, has_values=values, subtotal_hint=hint)


def test_depth_from_indentation_within_tolerance() -> None:
    nodes = infer_hierarchy(
        [row(1, "Current assets", 50, values=False), row(2, "Inventories", 60), row(3, "Cash", 61.5), row(4, "Total current assets", 50)]
    )
    assert [n.depth for n in nodes] == [0, 1, 1, 0]


def test_sections_subtotals_and_parents() -> None:
    nodes = infer_hierarchy(
        [
            row(1, "Current assets", 50, values=False),
            row(2, "Inventories", 60),
            row(3, "Total current assets", 50),
            row(4, "إجماليالموجودات", 50),
            row(5, "Gross profit", 50, hint=True),
        ]
    )
    by_row = {n.row: n for n in nodes}
    assert by_row[1].is_section and not by_row[2].is_section
    assert by_row[2].parent_row == 1
    assert by_row[3].is_subtotal and by_row[4].is_subtotal and by_row[5].is_subtotal
    assert not by_row[2].is_subtotal


def test_rows_without_boxes_sit_at_depth_zero() -> None:
    nodes = infer_hierarchy([row(1, "Revenue", None), row(2, "Cost of sales", None)])
    assert [n.depth for n in nodes] == [0, 0]
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_hierarchy.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.hierarchy'`.

- [ ] **Step 3: Implement `hierarchy.py`**

```python
"""A light hierarchy: depth from indentation, sections, subtotal cues and parents (spec 11).

Sum-based inference, which settles subtotals the cues miss, is Part 3b.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from fra_ingest.label_match import has_subtotal_cue

_INDENT_TOLERANCE = 4.0


class RowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int
    label: str
    indent: float | None
    has_values: bool
    subtotal_hint: bool = False


class RowNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int
    depth: int
    parent_row: int | None
    is_subtotal: bool
    is_section: bool


def _levels(indents: Sequence[float]) -> list[float]:
    levels: list[float] = []
    for value in sorted(indents):
        if not levels or value - levels[-1] > _INDENT_TOLERANCE:
            levels.append(value)
    return levels


def _depth(indent: float | None, levels: Sequence[float]) -> int:
    if indent is None:
        return 0
    return max(i for i, level in enumerate(levels) if indent >= level - _INDENT_TOLERANCE)


def infer_hierarchy(rows: Sequence[RowInput]) -> list[RowNode]:
    levels = _levels([r.indent for r in rows if r.indent is not None])
    nodes: list[RowNode] = []
    sections: list[RowNode] = []
    for r in rows:
        depth = _depth(r.indent, levels)
        is_section = bool(r.label) and not r.has_values
        is_subtotal = r.has_values and (r.subtotal_hint or has_subtotal_cue(r.label))
        if is_section:
            parent = next((s.row for s in reversed(sections) if s.depth < depth), None)
        else:
            parent = next((s.row for s in reversed(sections) if s.depth <= depth), None)
        node = RowNode(row=r.row, depth=depth, parent_row=parent, is_subtotal=is_subtotal, is_section=is_section)
        nodes.append(node)
        if is_section:
            sections.append(node)
    return nodes
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_hierarchy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/hierarchy.py packages/ingest/tests/test_hierarchy.py
git commit -m "Infer depth, sections, subtotal cues and parents from indentation and labels"
```

---

### Task 10: Statement parts from grids

**Files:**
- Create: `packages/ingest/src/fra_ingest/parts.py`
- Test: `packages/ingest/tests/test_parts.py`

**Interfaces:**
- Consumes: `Grid` (Task 4), `HeaderLayout`, `split_note` (Task 7), `Classification` (Task 8).
- Produces: `PartialStatement(type, confidence, first_page, last_page, page_width, periods: list[Period], column_centres: dict[str, float], line_items: list[LineItem], indents: dict[str, float | None], header_text: str, table_refs: list[str], flags: list[str])`, `column_centre(grid, col, rows) -> float | None`, `build_part(grid, layout, classification, *, source: TextSource) -> PartialStatement`. Line item ids are `p<page>-<table index>-r<row>`.

- [ ] **Step 1: Write the failing tests**

```python
"""Grid rows to line items with provenance (spec 11, Data flow step 7)."""

from decimal import Decimal

from fra_core.schemas import BBox, StatementType, TextSource
from fra_ingest.classify import Classification
from fra_ingest.header import parse_header
from fra_ingest.parts import build_part, column_centre
from fra_ingest.table_grid import Grid, GridCell


def box(col: int, row: int, left_pad: float = 0) -> BBox:
    return BBox(left=100 * col + 10 + left_pad, top=20 * row + 5, right=100 * col + 90, bottom=20 * row + 15)


def grid(rows: list[list[str]], pads: dict[int, float] | None = None) -> Grid:
    cells = tuple(
        GridCell(text=t, row=r, col=c, bbox=box(c, r, (pads or {}).get(r, 0) if c == 0 else 0), page_no=156, is_column_header=r == 0)
        for r, row in enumerate(rows)
        for c, t in enumerate(row)
        if t
    )
    return Grid(table_ref="#/tables/0", docling_path="docling/p155-164.json", page_no=156, page_width=800, num_rows=len(rows), num_cols=4, cells=cells)


ROWS = [
    ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
    ["Non-Current Assets", "", "", ""],
    ["Property, Plant and Equipment", "7", "26,058,632", "22,750,342"],
    ["Investments", "12", "-", "3,256"],
    ["Biological Assets", "11", "", "1,838,353"],
    ["Basic earnings per share", "", "2.10", "1.90"],
]


def part():
    g = grid(ROWS, pads={2: 10, 3: 10, 4: 10})
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    return build_part(g, layout, classification, source=TextSource.TEXT)


def test_line_items_carry_values_notes_and_provenance() -> None:
    p = part()
    assert [i.raw_label for i in p.line_items] == [
        "Non-Current Assets",
        "Property, Plant and Equipment",
        "Investments",
        "Biological Assets",
        "Basic earnings per share",
    ]
    ppe = p.line_items[1]
    assert ppe.id == "p156-t0-r2" and ppe.note_ref == "7"
    cell = ppe.cells[0]
    assert (cell.period_key, cell.reported, cell.raw_text) == ("2025-12-31", Decimal("26058632"), "26,058,632")
    assert (cell.provenance.page_no, cell.provenance.table_ref, cell.provenance.row, cell.provenance.col) == (156, "#/tables/0", 2, 2)
    assert cell.provenance.source is TextSource.TEXT
    assert p.line_items[0].cells == []


def test_a_dash_is_zero_and_an_empty_cell_is_missing() -> None:
    p = part()
    investments = p.line_items[2].cells
    assert investments[0].reported == Decimal("0")
    biological = p.line_items[3].cells
    assert biological[0].reported is None and "numbers_missing" in biological[0].flags
    assert biological[0].provenance.bbox.left == 210


def test_per_share_rows_are_marked() -> None:
    eps = part().line_items[4]
    assert all("per_share" in c.flags for c in eps.cells)


def test_periods_centres_and_indents() -> None:
    p = part()
    assert [x.key for x in p.periods] == ["2025-12-31", "2024-12-31"]
    assert p.column_centres == {"2025-12-31": 250.0, "2024-12-31": 350.0}
    assert p.indents["p156-t0-r2"] == 20.0 and p.indents["p156-t0-r1"] == 10.0
    assert (p.first_page, p.last_page, p.table_refs) == (156, 156, ["docling/p155-164.json#/tables/0"])


def test_column_centre_is_the_mean_of_its_cells() -> None:
    g = grid(ROWS)
    assert column_centre(g, 2, [2, 3]) == 250.0
    assert column_centre(g, 2, [1]) is None
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_parts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.parts'`.

- [ ] **Step 3: Implement `parts.py`**

```python
"""One classified grid as part of a statement: line items with provenance (spec 11, step 7).

Values are kept as printed, sign applied, before scale. An empty value cell in a row that has
values elsewhere is kept with ``numbers_missing`` and a box synthesized from its row and
column, so every gap stays visible and traceable.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from fra_core.numbers import parse_number
from fra_core.schemas import BBox, Cell, LineItem, Period, Provenance, StatementType, TextSource
from fra_ingest.classify import Classification
from fra_ingest.header import HeaderLayout, split_note
from fra_ingest.label_match import squash
from fra_ingest.table_grid import Grid

_PER_SHARE = tuple(squash(w) for w in ("per share", "للسهم", "ربحية السهم"))


class PartialStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StatementType
    confidence: float
    first_page: int
    last_page: int
    page_width: float
    periods: list[Period]
    column_centres: dict[str, float] = Field(default_factory=dict)
    line_items: list[LineItem] = Field(default_factory=list)
    indents: dict[str, float | None] = Field(default_factory=dict)
    header_text: str = ""
    table_refs: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def column_centre(grid: Grid, col: int, rows: Sequence[int]) -> float | None:
    centres = []
    for row in rows:
        cell = grid.cell(row, col)
        if cell is not None and cell.text and cell.bbox is not None:
            centres.append((cell.bbox.left + cell.bbox.right) / 2)
    return sum(centres) / len(centres) if centres else None


def _band(grid: Grid, *, row: int | None = None, col: int | None = None) -> tuple[float, float] | None:
    boxes = [
        c.bbox
        for c in grid.cells
        if c.bbox is not None and (row is None or c.row == row) and (col is None or c.col == col)
    ]
    if not boxes:
        return None
    if row is not None:
        return min(b.top for b in boxes), max(b.bottom for b in boxes)
    return min(b.left for b in boxes), max(b.right for b in boxes)


def _synthesized_box(grid: Grid, row: int, col: int) -> BBox | None:
    rows, cols = _band(grid, row=row), _band(grid, col=col)
    if rows is None or cols is None:
        return None
    return BBox(left=cols[0], top=rows[0], right=cols[1], bottom=rows[1])


def build_part(
    grid: Grid,
    layout: HeaderLayout,
    classification: Classification,
    *,
    source: TextSource,
) -> PartialStatement:
    if classification.type is None:
        msg = "only a classified grid becomes part of a statement"
        raise ValueError(msg)
    data_rows = layout.data_rows(grid)
    periods = list(layout.value_cols.values())
    right = grid.right_edge()
    label_on_right = layout.label_col is not None and layout.label_col == max(
        (c for c in range(grid.num_cols) if c in layout.value_cols or c == layout.label_col), default=-1
    )
    part = PartialStatement(
        type=classification.type,
        confidence=classification.confidence,
        first_page=grid.page_no,
        last_page=grid.page_no,
        page_width=grid.page_width,
        periods=periods,
        header_text=" ".join(layout.header_texts.values()),
        table_refs=[f"{grid.docling_path}{grid.table_ref}"],
        flags=list(layout.evidence),
    )
    for col, period in layout.value_cols.items():
        centre = column_centre(grid, col, data_rows)
        if centre is not None:
            part.column_centres[period.key] = centre

    for row in data_rows:
        label_cell = grid.cell(row, layout.label_col) if layout.label_col is not None else None
        label = label_cell.text if label_cell is not None else ""
        note = grid.text(row, layout.note_col) if layout.note_col is not None else ""
        if not note:
            label, split = split_note(label)
            note = split or ""
        texts = {col: grid.text(row, col) for col in layout.value_cols}
        if not label and not any(texts.values()):
            continue
        item_id = f"p{grid.page_no}-{grid.table_index}-r{row}"
        per_share = any(cue in squash(label) for cue in _PER_SHARE)
        has_values = any(texts.values())
        cells = []
        for col, period in layout.value_cols.items():
            if not has_values:
                break
            grid_cell = grid.cell(row, col)
            text = texts[col]
            parsed = parse_number(text) if text else None
            flags = list(parsed.flags) if parsed else ["numbers_missing"]
            if grid_cell is not None:
                flags.extend(grid_cell.flags)
            if per_share:
                flags.append("per_share")
            bbox = grid_cell.bbox if grid_cell is not None and grid_cell.bbox is not None else _synthesized_box(grid, row, col)
            if bbox is None:
                flags.append("no_box")
                continue
            if grid_cell is None or grid_cell.bbox is None:
                flags.append("bbox_synthesized")
            cells.append(
                Cell(
                    period_key=period.key,
                    reported=parsed.value if parsed else None,
                    raw_text=text,
                    provenance=Provenance(page_no=grid.page_no, bbox=bbox, table_ref=grid.table_ref, row=row, col=col, source=source),
                    flags=flags,
                )
            )
        part.line_items.append(LineItem(id=item_id, raw_label=label, note_ref=note or None, cells=cells))
        indent = None
        if label_cell is not None and label_cell.bbox is not None:
            if label_on_right and right is not None:
                indent = right - label_cell.bbox.right
            else:
                indent = label_cell.bbox.left
        part.indents[item_id] = indent
    return part
```

The indent of a left-aligned label is its left edge; a right-aligned (Arabic) label's indent is its distance from the table's right edge. `infer_hierarchy` clusters these values, so only their differences matter.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_parts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/parts.py packages/ingest/tests/test_parts.py
git commit -m "Turn classified grids into statement parts with provenance on every value"
```

---

### Task 11: Continuation

**Files:**
- Create: `packages/ingest/src/fra_ingest/continuation.py`
- Test: `packages/ingest/tests/test_continuation.py`

**Interfaces:**
- Consumes: `Grid` (Task 4), `HeaderLayout` (Task 7), `PartialStatement`, `column_centre` (Task 10).
- Produces: `inherit_periods(layout, grid, previous: PartialStatement) -> HeaderLayout` (binds `unbound_cols` by position, evidence `inherited:<col>:<key>`), `merge_continuations(parts: Sequence[PartialStatement]) -> list[PartialStatement]`. Tolerance: 5% of page width.

- [ ] **Step 1: Write the failing tests**

```python
"""Statements continued across pages (spec 11, Data flow step 8)."""

from datetime import date

from fra_core.schemas import BBox, LineItem, Period, PeriodKind, StatementType
from fra_ingest.continuation import inherit_periods, merge_continuations
from fra_ingest.header import HeaderLayout
from fra_ingest.parts import PartialStatement
from fra_ingest.table_grid import Grid, GridCell

FY25 = Period(key="FY2025", end_date=date(2025, 12, 31), kind=PeriodKind.DURATION, months=12)
FY24 = Period(key="FY2024", end_date=date(2024, 12, 31), kind=PeriodKind.DURATION, months=12)
INCOME = StatementType.INCOME


def part(page: int, periods: list[Period], centres: dict[str, float], labels: list[str], kind: StatementType = INCOME) -> PartialStatement:
    return PartialStatement(
        type=kind,
        confidence=0.8,
        first_page=page,
        last_page=page,
        page_width=800,
        periods=periods,
        column_centres=centres,
        line_items=[LineItem(id=f"p{page}-t0-r{i}", raw_label=label) for i, label in enumerate(labels)],
        table_refs=[f"d#/tables/{page}"],
    )


def test_consecutive_parts_with_matching_columns_merge() -> None:
    merged = merge_continuations(
        [
            part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"]),
            part(160, [FY25, FY24], {"FY2025": 255, "FY2024": 348}, ["Profit"]),
        ]
    )
    assert len(merged) == 1
    assert (merged[0].first_page, merged[0].last_page) == (159, 160)
    assert [i.raw_label for i in merged[0].line_items] == ["Revenue", "Profit"]
    assert merged[0].table_refs == ["d#/tables/159", "d#/tables/160"]


def test_mismatched_periods_columns_types_or_pages_stay_apart() -> None:
    base = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    assert len(merge_continuations([base, part(160, [FY25], {"FY2025": 250}, ["x"])])) == 2
    assert len(merge_continuations([base, part(160, [FY25, FY24], {"FY2025": 400, "FY2024": 500}, ["x"])])) == 2
    assert len(merge_continuations([base, part(160, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"], StatementType.BALANCE)])) == 2
    assert len(merge_continuations([base, part(162, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["x"])])) == 2


def test_an_unbound_column_inherits_the_previous_pages_period_by_position() -> None:
    cells = tuple(
        GridCell(text=t, row=r, col=c, bbox=BBox(left=x - 40, top=20 * r + 5, right=x + 40, bottom=20 * r + 15), page_no=160)
        for r, row in enumerate([["", "31 December 2025", "thousands X"], ["Profit", "10", "9"]])
        for c, (t, x) in enumerate(zip(row, (100, 252, 351), strict=True))
        if t
    )
    grid = Grid(table_ref="#/tables/4", docling_path="d", page_no=160, page_width=800, num_rows=2, num_cols=3, cells=cells)
    layout = HeaderLayout(header_rows=[0], label_col=0, value_cols={1: FY25}, unbound_cols=[2], evidence=["period_unbound:2"])
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    inherited = inherit_periods(layout, grid, previous)
    assert {c: p.key for c, p in inherited.value_cols.items()} == {1: "FY2025", 2: "FY2024"}
    assert inherited.unbound_cols == []
    assert "inherited:2:FY2024" in inherited.evidence


def test_nothing_is_inherited_from_a_distant_column() -> None:
    cells = (GridCell(text="9", row=1, col=2, bbox=BBox(left=560, top=25, right=640, bottom=35), page_no=160),)
    grid = Grid(table_ref="#/tables/4", docling_path="d", page_no=160, page_width=800, num_rows=2, num_cols=3, cells=cells)
    layout = HeaderLayout(header_rows=[0], label_col=0, value_cols={}, unbound_cols=[2])
    previous = part(159, [FY25, FY24], {"FY2025": 250, "FY2024": 350}, ["Revenue"])
    assert inherit_periods(layout, grid, previous).unbound_cols == [2]
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_continuation.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.continuation'`.

- [ ] **Step 3: Implement `continuation.py`**

```python
"""Statements continued across pages (spec 11, Data flow step 8).

Parts of one type on consecutive pages merge when their periods match and their value
columns line up. A continuation page whose header leaves a value column without a date
(Almarai AR, p160) takes that column's period from the previous page, by position.
"""

from __future__ import annotations

from collections.abc import Sequence

from fra_ingest.header import HeaderLayout
from fra_ingest.parts import PartialStatement, column_centre
from fra_ingest.table_grid import Grid

COLUMN_TOLERANCE = 0.05


def inherit_periods(layout: HeaderLayout, grid: Grid, previous: PartialStatement) -> HeaderLayout:
    if not layout.unbound_cols:
        return layout
    tolerance = COLUMN_TOLERANCE * grid.page_width
    bound_keys = {p.key for p in layout.value_cols.values()}
    value_cols = dict(layout.value_cols)
    unbound: list[int] = []
    evidence = list(layout.evidence)
    rows = layout.data_rows(grid)
    for col in layout.unbound_cols:
        centre = column_centre(grid, col, rows)
        match = None
        if centre is not None:
            candidates = [
                p for p in previous.periods
                if p.key not in bound_keys and p.key in previous.column_centres
                and abs(previous.column_centres[p.key] - centre) <= tolerance
            ]
            match = min(candidates, key=lambda p: abs(previous.column_centres[p.key] - centre), default=None)
        if match is None:
            unbound.append(col)
            continue
        value_cols[col] = match
        bound_keys.add(match.key)
        evidence.append(f"inherited:{col}:{match.key}")
    return layout.model_copy(update={"value_cols": value_cols, "unbound_cols": unbound, "evidence": evidence})


def _continues(first: PartialStatement, second: PartialStatement) -> bool:
    if second.type is not first.type or second.first_page != first.last_page + 1:
        return False
    if [p.key for p in first.periods] != [p.key for p in second.periods]:
        return False
    tolerance = COLUMN_TOLERANCE * first.page_width
    for key, centre in second.column_centres.items():
        if key in first.column_centres and abs(first.column_centres[key] - centre) > tolerance:
            return False
    return True


def merge_continuations(parts: Sequence[PartialStatement]) -> list[PartialStatement]:
    ordered = sorted(parts, key=lambda p: (p.type.value, p.first_page))
    merged: list[PartialStatement] = []
    for part in ordered:
        if merged and _continues(merged[-1], part):
            head = merged[-1]
            merged[-1] = head.model_copy(
                update={
                    "last_page": part.last_page,
                    "line_items": [*head.line_items, *part.line_items],
                    "indents": {**head.indents, **part.indents},
                    "table_refs": [*head.table_refs, *part.table_refs],
                    "flags": [*head.flags, *part.flags],
                }
            )
        else:
            merged.append(part)
    return merged
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_continuation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/continuation.py packages/ingest/tests/test_continuation.py
git commit -m "Merge statements continued across pages and inherit a page's missing period"
```

---

### Task 12: Subtotal and identity checks

**Files:**
- Create: `packages/ingest/src/fra_ingest/table_checks.py`
- Test: `packages/ingest/tests/test_table_checks.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 3), `LabelIndex` (Task 3), `Statement` and `LineItem` with `is_subtotal` set.
- Produces: `check_subtotals(statement) -> list[CheckResult]`, `check_identity(statement, index) -> list[CheckResult]`, `run_checks(statement, index) -> tuple[Statement, list[CheckResult]]` (adds `subtotal_failed`, `identity_failed`, `identity_totals_not_found` to the statement's flags).

- [ ] **Step 1: Write the failing tests**

```python
"""Subtotal checks and the balance sheet identity (spec 11, Data flow step 10)."""

from datetime import date
from decimal import Decimal

from fra_core.schemas import BBox, Cell, LineItem, Period, PeriodKind, Provenance, Statement, StatementType
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_checks import check_identity, check_subtotals, run_checks

INDEX = LabelIndex(load_taxonomy())
P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, label: str, value: str | None, subtotal: bool = False) -> LineItem:
    cells = [] if value is None else [
        Cell(period_key=P.key, reported=Decimal(value) if value != "?" else None, raw_text=value, provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2))
    ]
    return LineItem(id=f"r{n}", raw_label=label, is_subtotal=subtotal, cells=cells)


def statement(items: list[LineItem], kind: StatementType = StatementType.BALANCE) -> Statement:
    return Statement(id="s", document_sha256="a" * 64, type=kind, currency="SAR", scale=1000, periods=[P], line_items=items)


def test_a_subtotal_over_its_rows_passes_within_the_d6_tolerance() -> None:
    checks = check_subtotals(statement([item(1, "Current assets", None), item(2, "Inventories", "10"), item(3, "Cash", "5"), item(4, "Total current assets", "16", subtotal=True)]))
    assert [c.status for c in checks] == ["pass"]
    assert checks[0].tolerance == Decimal("1.0") and checks[0].difference == Decimal("1")


def test_a_subtotal_that_misses_fails() -> None:
    checks = check_subtotals(statement([item(1, "Inventories", "10"), item(2, "Cash", "5"), item(3, "Total current assets", "20", subtotal=True)]))
    assert checks[0].status == "fail" and checks[0].expected == Decimal("15")


def test_a_running_total_counts_the_previous_subtotal() -> None:
    income = statement(
        [item(1, "Revenue", "100"), item(2, "Cost of sales", "-60"), item(3, "Gross profit", "40", subtotal=True), item(4, "Selling", "-10"), item(5, "Admin", "-5"), item(6, "Operating profit", "25", subtotal=True)],
        StatementType.INCOME,
    )
    assert [c.status for c in check_subtotals(income)] == ["pass", "pass"]


def test_a_subtotal_over_subtotals_is_skipped() -> None:
    checks = check_subtotals(statement([item(1, "Inventories", "10"), item(2, "Cash", "5"), item(3, "Total current assets", "15", subtotal=True), item(4, "Total assets", "15", subtotal=True)]))
    assert [c.status for c in checks] == ["pass", "skipped"]
    assert checks[1].detail == "subtotal_scope_unknown"


def test_identity_through_a_printed_liabilities_and_equity_total() -> None:
    s = statement([item(1, "Total assets", "100", True), item(2, "Total liabilities", "60", True), item(3, "Total equity", "40", True), item(4, "Total liabilities and equity", "100", True)])
    checks = check_identity(s, INDEX)
    assert [c.status for c in checks] == ["pass"]


def test_identity_through_the_two_totals_and_its_failure() -> None:
    s = statement([item(1, "Total assets", "100", True), item(2, "Total liabilities", "60", True), item(3, "Total equity", "30", True)])
    checks = check_identity(s, INDEX)
    assert checks[0].status == "fail" and checks[0].difference == Decimal("-10")


def test_identity_is_skipped_when_totals_are_missing() -> None:
    s = statement([item(1, "Total assets", "100", True)])
    checks = check_identity(s, INDEX)
    assert checks[0].status == "skipped" and checks[0].detail == "identity_totals_not_found"


def test_run_checks_flags_the_statement() -> None:
    s = statement([item(1, "Inventories", "10"), item(2, "Cash", "5"), item(3, "Total current assets", "20", True), item(4, "Total assets", "100", True)])
    checked, results = run_checks(s, INDEX)
    assert "subtotal_failed" in checked.flags and "identity_totals_not_found" in checked.flags
    assert results
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_table_checks.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.table_checks'`.

- [ ] **Step 3: Implement `table_checks.py`**

```python
"""Subtotal checks and the balance sheet identity (spec 11, Data flow step 10).

Tolerance is rounding-aware (D6): n x 0.5 reported units, n the number of addends. A subtotal
is checked only when it directly closes a run of two or more plain rows; it passes against
their sum, or against their sum plus the previous subtotal (a running total such as operating
profit after gross profit). Other subtotals are skipped for Part 3b's sum-based hierarchy.
"""

from __future__ import annotations

from decimal import Decimal

from fra_core.schemas import CheckResult, LineItem, Statement, StatementType
from fra_ingest.label_match import LabelIndex

_HALF = Decimal("0.5")


def _value(item: LineItem, period_key: str) -> Decimal | None:
    return item.value_for(period_key)


def _result(statement: Statement, kind: str, key: str, target: str, status: str, **fields: object) -> CheckResult:
    return CheckResult.model_validate(
        {"id": f"{statement.id}:{kind}:{target}:{key}", "statement_id": statement.id, "kind": kind, "period_key": key, "status": status, **fields}
    )


def check_subtotals(statement: Statement) -> list[CheckResult]:
    results: list[CheckResult] = []
    run: list[LineItem] = []
    previous: LineItem | None = None
    for item in statement.line_items:
        if not item.cells:
            run, previous = [], None
            continue
        if not item.is_subtotal:
            run.append(item)
            continue
        for period in statement.periods:
            key = period.key
            if len(run) < 2:
                results.append(_result(statement, "subtotal", key, item.id, "skipped", line_item_ids=[item.id], detail="subtotal_scope_unknown"))
                continue
            values = [_value(i, key) for i in run]
            actual = _value(item, key)
            if actual is None or any(v is None for v in values):
                results.append(_result(statement, "subtotal", key, item.id, "skipped", line_item_ids=[item.id], detail="missing_values"))
                continue
            plain = sum((v for v in values if v is not None), Decimal(0))
            candidates = [(plain, len(run), [i.id for i in run])]
            prior = _value(previous, key) if previous is not None else None
            if previous is not None and prior is not None:
                candidates.append((plain + prior, len(run) + 1, [previous.id, *(i.id for i in run)]))
            expected, addends, ids = min(candidates, key=lambda c: abs(actual - c[0]))
            tolerance = _HALF * addends
            difference = actual - expected
            status = "pass" if abs(difference) <= tolerance else "fail"
            results.append(_result(statement, "subtotal", key, item.id, status, expected=expected, actual=actual, difference=difference, tolerance=tolerance, line_item_ids=[*ids, item.id]))
        run, previous = [], item
    return results


def _find(statement: Statement, index: LabelIndex, canonical_id: str) -> LineItem | None:
    matches = [i for i in statement.line_items if i.cells and (m := index.match(i.raw_label, StatementType.BALANCE)) is not None and m.id == canonical_id]
    return matches[-1] if matches else None


def check_identity(statement: Statement, index: LabelIndex) -> list[CheckResult]:
    if statement.type is not StatementType.BALANCE:
        return []
    assets = _find(statement, index, "total_assets")
    both = _find(statement, index, "total_liabilities_and_equity")
    liabilities = _find(statement, index, "total_liabilities")
    equity = _find(statement, index, "total_equity")
    results: list[CheckResult] = []
    for period in statement.periods:
        key = period.key
        if assets is None or (both is None and (liabilities is None or equity is None)):
            results.append(_result(statement, "balance_identity", key, "balance", "skipped", detail="identity_totals_not_found"))
            continue
        actual = _value(assets, key)
        if both is not None:
            parts, ids = [_value(both, key)], [both.id]
        else:
            assert liabilities is not None and equity is not None
            parts, ids = [_value(liabilities, key), _value(equity, key)], [liabilities.id, equity.id]
        if actual is None or any(p is None for p in parts):
            results.append(_result(statement, "balance_identity", key, "balance", "skipped", detail="missing_values", line_item_ids=[assets.id, *ids]))
            continue
        expected = sum((p for p in parts if p is not None), Decimal(0))
        tolerance = _HALF * len(parts)
        difference = actual - expected
        status = "pass" if abs(difference) <= tolerance else "fail"
        results.append(_result(statement, "balance_identity", key, "balance", status, expected=expected, actual=actual, difference=difference, tolerance=tolerance, line_item_ids=[assets.id, *ids], detail="total assets against total liabilities and equity"))
    return results


def run_checks(statement: Statement, index: LabelIndex) -> tuple[Statement, list[CheckResult]]:
    results = check_subtotals(statement) + check_identity(statement, index)
    flags = list(statement.flags)
    if any(r.kind == "subtotal" and r.status == "fail" for r in results):
        flags.append("subtotal_failed")
    if any(r.kind == "balance_identity" and r.status == "fail" for r in results):
        flags.append("identity_failed")
    if any(r.kind == "balance_identity" and r.detail == "identity_totals_not_found" for r in results):
        flags.append("identity_totals_not_found")
    return statement.model_copy(update={"flags": flags}), results
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_table_checks.py -q`
Expected: PASS. If the taxonomy maps "Total liabilities and equity" only through a different spelling, add no alias: change the test label to one of the taxonomy's own `total_liabilities_and_equity` aliases (see `canonical_items.yaml`).

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/table_checks.py packages/ingest/tests/test_table_checks.py
git commit -m "Check subtotals and the balance sheet identity at the D6 tolerance"
```

---

### Task 13: The structure stage and command

**Files:**
- Create: `packages/ingest/src/fra_ingest/structure.py`
- Modify: `packages/ingest/src/fra_ingest/cli.py` (`structure` subcommand)
- Test: `packages/ingest/tests/test_structure.py`, `packages/ingest/tests/test_structure_golden.py`, `packages/ingest/tests/test_cli.py`

**Interfaces:**
- Consumes: every module of Tasks 2 to 12; `load_or_locate`, `read_pages`, `convert_in_child`, `ConvertResult`, `CONVERT_VERSION`.
- Produces: `STRUCTURE_VERSION = "1"`, `structure_pdf(pdf, config, ocr, *, use_cache=True, convert=convert_in_child) -> StructureResult` (writes `statements.raw.json` and `table_checks.json`), `structure_document(inputs: StructureInputs, config) -> tuple[StructureResult, list[CheckResult]]` (pure: no disk), `StructureInputs(sha256, language, industry_flags, page_modes: dict[int, PageMode], visual_pages: set[int], page_texts: dict[int, str], title_types: dict[int, tuple[StatementType, ...]], cue_types: dict[int, tuple[StatementType, ...]], documents: list[tuple[str, DlDocument]], convert: ConvertResult)`. CLI `fra-ingest structure <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]`: exit 0 when written, 2 on `IngestError`.

- [ ] **Step 1: Write the failing fast tests**

`packages/ingest/tests/test_structure.py` builds a small docling JSON by hand, so the whole pipeline runs without Part 2:

```python
"""The structure stage end to end on a hand-made docling document (spec 11, Data flow)."""

from __future__ import annotations

from decimal import Decimal

from fra_core.schemas import PageMode, StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.results import ConvertResult, RangeConversion
from fra_ingest.structure import StructureInputs, structure_document

SHA = "b" * 64


def cells(rows: list[list[str]], x0: float = 60) -> list[dict[str, object]]:
    out = []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if not text:
                continue
            left = x0 + 150 * c
            out.append(
                {
                    "text": text,
                    "start_row_offset_idx": r,
                    "end_row_offset_idx": r + 1,
                    "start_col_offset_idx": c,
                    "end_col_offset_idx": c + 1,
                    "column_header": r == 0,
                    "bbox": {"l": left, "t": 100 + 20 * r, "r": left + 120, "b": 112 + 20 * r, "coord_origin": "TOPLEFT"},
                }
            )
    return out


def table(ref: int, page: int, rows: list[list[str]]) -> dict[str, object]:
    return {
        "self_ref": f"#/tables/{ref}",
        "prov": [{"page_no": page, "bbox": {"l": 50, "t": 700, "r": 700, "b": 300, "coord_origin": "BOTTOMLEFT"}}],
        "data": {"num_rows": len(rows), "num_cols": 4, "table_cells": cells(rows)},
    }


HEADER = ["", "Notes", "31 December 2025 SAR '000", "31 December 2024 SAR '000"]
PAGE_1 = [HEADER, ["Inventories", "19", "10", "8"], ["Cash", "21", "5", "4"], ["Total current assets", "", "15", "12"], ["Total assets", "", "15", "12"]]
PAGE_2 = [HEADER, ["Trade payables", "", "6", "5"], ["Total liabilities", "", "6", "5"], ["Share capital", "", "9", "7"], ["Total equity", "", "9", "7"]]
SIGNATURES = [["Chief Financial Officer", "Chief Executive Officer", "Chairman", ""]]


def inputs(documents: list[tuple[str, DlDocument]]) -> StructureInputs:
    convert = ConvertResult(
        version="1",
        sha256=SHA,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[RangeConversion(first_page=1, last_page=2, ocr="pdf_aware", ocr_language="en-US", docling_path="docling/p1-2.json", status="ok")],
    )
    return StructureInputs(
        sha256=SHA,
        language="en",
        industry_flags=(),
        page_modes={1: PageMode.TEXT, 2: PageMode.TEXT},
        visual_pages=set(),
        page_texts={1: "Statement of financial position", 2: ""},
        title_types={1: (StatementType.BALANCE,), 2: (StatementType.BALANCE,)},
        cue_types={},
        documents=documents,
        convert=convert,
    )


def document() -> DlDocument:
    return DlDocument.model_validate(
        {
            "tables": [table(0, 1, PAGE_1), table(1, 2, PAGE_2), table(2, 2, SIGNATURES)],
            "texts": [],
            "pages": {"1": {"page_no": 1, "size": {"width": 800, "height": 1000}}, "2": {"page_no": 2, "size": {"width": 800, "height": 1000}}},
        }
    )


def test_a_balance_sheet_over_two_pages_becomes_one_checked_statement() -> None:
    result, checks = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    assert len(result.statements) == 1
    statement = result.statements[0]
    assert statement.type is StatementType.BALANCE
    assert (statement.currency, statement.scale) == ("SAR", 1000)
    assert statement.source_pages == [1, 2]
    assert [p.key for p in statement.periods] == ["2025-12-31", "2024-12-31"]
    labels = [i.raw_label for i in statement.line_items]
    assert labels[0] == "Inventories" and labels[-1] == "Total equity"
    assert statement.line_items[0].cells[0].reported == Decimal("10")
    identity = [c for c in checks if c.kind == "balance_identity"]
    assert {c.status for c in identity} == {"pass"}
    signature = [t for t in result.tables if t.table_ref == "#/tables/2"]
    assert signature and signature[0].type is None


def test_decisions_name_the_statement_each_table_became() -> None:
    result, _ = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    statement_id = result.statements[0].id
    kept = {t.table_ref: t.statement_id for t in result.tables}
    assert kept == {"#/tables/0": statement_id, "#/tables/1": statement_id, "#/tables/2": None}


def test_two_balance_sheets_on_one_page_are_flagged_ambiguous() -> None:
    doubled = DlDocument.model_validate(
        {
            "tables": [table(0, 1, PAGE_1), table(1, 1, PAGE_1)],
            "texts": [],
            "pages": {"1": {"page_no": 1, "size": {"width": 800, "height": 1000}}},
        }
    )
    result, _ = structure_document(inputs([("docling/p1-2.json", doubled)]), IngestConfig())
    balance = [s for s in result.statements if s.type is StatementType.BALANCE]
    assert len(balance) == 2
    assert all("ambiguous_statement:balance" in s.flags for s in balance)


def test_a_missing_enabled_type_is_flagged() -> None:
    result, _ = structure_document(inputs([("docling/p1-2.json", document())]), IngestConfig())
    assert "statement_not_extracted:income" in result.flags
```

Append to `packages/ingest/tests/test_cli.py` (import `from fra_ingest.results import StructureResult` and `from fra_ingest import cli` as the file already does for convert):

```python
def test_structure_writes_a_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf")

    def fake_structure(pdf_path: Path, config: object, ocr: object, **kwargs: object) -> StructureResult:
        return StructureResult(version="1", sha256="c" * 64, convert_version="1", settings_hash="h", flags=["statement_not_extracted:balance"])

    monkeypatch.setattr(cli, "structure_pdf", fake_structure)
    assert main(["structure", str(pdf), "--no-ocr", "--artifacts", str(tmp_path / "a"), "--json"]) == 0
    assert StructureResult.model_validate_json(capsys.readouterr().out).flags == ["statement_not_extracted:balance"]
```

- [ ] **Step 2: Write the failing golden tests**

`packages/ingest/tests/test_structure_golden.py` runs on Part 2's real artifacts and is the Gate A test:

```python
"""Almarai EN and AR through structure (spec 11, Done when)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from support import artifact_dir

from fra_core.schemas import Statement, StatementType
from fra_ingest.config import load_config
from fra_ingest.structure import structure_pdf

pytestmark = pytest.mark.golden


def statements(golden: Callable[[str], Path], name: str, tmp_path: Path) -> dict[StatementType, Statement]:
    pdf = golden(name)
    if not (artifact_dir(pdf) / "convert.json").exists():
        pytest.skip("run make eval-convert first")
    config = load_config()
    result = structure_pdf(pdf, config, None)
    found: dict[StatementType, Statement] = {}
    for s in result.statements:
        found.setdefault(s.type, s)
    return found


def value_rows(statement: Statement) -> Counter[tuple[tuple[str, Decimal], ...]]:
    rows: Counter[tuple[tuple[str, Decimal], ...]] = Counter()
    for item in statement.line_items:
        values = tuple(sorted((c.period_key, c.reported * statement.scale) for c in item.cells if c.reported is not None))
        if values:
            rows[values] += 1
    return rows


def test_almarai_en_balance_sheet(golden: Callable[[str], Path], tmp_path: Path) -> None:
    found = statements(golden, "almarai-2025-en-annualreport.pdf", tmp_path)
    balance = found[StatementType.BALANCE]
    assert balance.source_pages == [156, 157, 158]
    assert (balance.currency, balance.scale) == ("SAR", 1000)
    assert [p.key for p in balance.periods] == ["2025-12-31", "2024-12-31"]
    ppe = next(i for i in balance.line_items if i.raw_label.startswith("Property, Plant"))
    assert ppe.value_for("2025-12-31") == Decimal("26058632")
    assert "identity_failed" not in balance.flags


@pytest.mark.parametrize("statement_type", [StatementType.BALANCE, StatementType.INCOME, StatementType.COMPREHENSIVE_INCOME])
def test_almarai_english_and_arabic_figures_match(golden: Callable[[str], Path], tmp_path: Path, statement_type: StatementType) -> None:
    english = statements(golden, "almarai-2025-en-annualreport.pdf", tmp_path)[statement_type]
    arabic = statements(golden, "almarai-2025-ar-annualreport.pdf", tmp_path)[statement_type]
    en_rows, ar_rows = value_rows(english), value_rows(arabic)
    assert en_rows - ar_rows == Counter(), f"English rows without an Arabic counterpart: {en_rows - ar_rows}"
    assert ar_rows - en_rows == Counter(), f"Arabic rows without an English counterpart: {ar_rows - en_rows}"
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_structure.py packages/ingest/tests/test_structure_golden.py packages/ingest/tests/test_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.structure'`.

- [ ] **Step 4: Implement `structure.py`**

```python
"""The structure stage: docling tables to statements with provenance and checks (spec 11).

``structure_document`` is pure and does the work; ``structure_pdf`` gathers its inputs from
Parts 1 and 2, caches by settings, and writes ``statements.raw.json`` and
``table_checks.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from fra_core.periods import parse_period
from fra_core.schemas import CheckResult, LineItem, PageMode, Statement, StatementType, TextSource
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.child import convert_in_child
from fra_ingest.classify import Classification, TableContext, classify
from fra_ingest.config import IngestConfig
from fra_ingest.continuation import inherit_periods, merge_continuations
from fra_ingest.convert import CONVERT_VERSION
from fra_ingest.docling_json import DlDocument, load_docling_json
from fra_ingest.header import HeaderLayout, parse_header
from fra_ingest.hierarchy import RowInput, infer_hierarchy
from fra_ingest.label_match import LabelIndex
from fra_ingest.metadata import Metadata, detect_metadata
from fra_ingest.ocr import OcrEngine
from fra_ingest.pages import read_pages
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.results import ConvertResult, StructureResult, TableDecision
from fra_ingest.stage import load_or_locate
from fra_ingest.table_checks import run_checks
from fra_ingest.table_grid import Grid, build_grid
from fra_ingest.visual_order import repair_grid, repair_text

STRUCTURE_VERSION = "1"
NO_CURRENCY = "XXX"  # ISO 4217 code for "no currency"
_FINANCIAL = ("bank", "insurer", "other_financial")


class StructureInputs(BaseModel):
    """Everything structure needs from Parts 1 and 2, gathered so the work itself is pure."""

    model_config = ConfigDict(extra="forbid")

    sha256: str
    language: str
    industry_flags: tuple[str, ...]
    page_modes: dict[int, PageMode]
    visual_pages: set[int]
    page_texts: dict[int, str]
    title_types: dict[int, tuple[StatementType, ...]]
    cue_types: dict[int, tuple[StatementType, ...]]
    documents: list[tuple[str, DlDocument]]
    convert: ConvertResult


def _headings(document: DlDocument, grid: Grid, visual: bool) -> list[str]:
    """Texts above the table on its page, top to bottom, the last six, repaired like the table."""
    height = document.page_size(grid.page_no).height
    tops = [c.bbox.top for c in grid.cells if c.bbox is not None]
    table_top = min(tops) if tops else height
    above: list[tuple[float, str]] = []
    for text in document.texts_on(grid.page_no):
        box = text.prov[0].bbox.to_bbox(height)
        if box is not None and box.bottom <= table_top + 1:
            above.append((box.top, repair_text(text.text, visual=visual)))
    return [t for _, t in sorted(above)][-6:]


def _date_hint(headings: Sequence[str]) -> str | None:
    return next((h for h in reversed(headings) if parse_period(h) is not None), None)


def _statement(part: PartialStatement, meta: Metadata, inputs: StructureInputs, index: LabelIndex, number: int) -> Statement:
    rows = []
    for i, item in enumerate(part.line_items):
        matched = index.match(item.raw_label, part.type)
        rows.append(
            RowInput(
                row=i,
                label=item.raw_label,
                indent=part.indents.get(item.id),
                has_values=bool(item.cells),
                subtotal_hint=matched is not None and matched.subtotal,
            )
        )
    items: list[LineItem] = []
    for node, item in zip(infer_hierarchy(rows), part.line_items, strict=True):
        parent = part.line_items[node.parent_row].id if node.parent_row is not None else None
        items.append(item.model_copy(update={"depth": node.depth, "is_subtotal": node.is_subtotal, "parent_id": parent}))
    return Statement(
        id=f"{inputs.sha256[:12]}-{part.type.value}-{number}",
        document_sha256=inputs.sha256,
        type=part.type,
        entity_name=meta.entity_name,
        consolidated=meta.consolidated,
        currency=meta.currency or NO_CURRENCY,
        scale=meta.scale or 1,
        language=inputs.language,
        periods=part.periods,
        line_items=items,
        source_pages=list(range(part.first_page, part.last_page + 1)),
        flags=[*part.flags, *meta.flags],
    )


def structure_document(inputs: StructureInputs, config: IngestConfig) -> tuple[StructureResult, list[CheckResult]]:
    index = LabelIndex(load_taxonomy())
    decisions: list[TableDecision] = []
    classified: list[tuple[Grid, HeaderLayout, Classification, list[str]]] = []
    for path, document in inputs.documents:
        for table in document.tables:
            raw = build_grid(table, document, path)
            visual = raw.page_no in inputs.visual_pages
            grid = repair_grid(raw, visual=visual)
            headings = _headings(document, grid, visual)
            hint = _date_hint(headings)
            context = TableContext(
                title_types=inputs.title_types.get(grid.page_no, ()),
                cue_types=inputs.cue_types.get(grid.page_no, ()),
                heading_texts=tuple(headings),
                industry_flags=inputs.industry_flags,
            )
            result = classify(grid, parse_header(grid, StatementType.BALANCE, hint), context, index, config.min_confidence)
            decisions.append(
                TableDecision(
                    table_ref=grid.table_ref,
                    docling_path=path,
                    page_no=grid.page_no,
                    type=result.type,
                    confidence=min(result.confidence, 1.0),
                    evidence=result.evidence,
                )
            )
            if result.type is not None:
                classified.append((grid, parse_header(grid, result.type, hint), result, headings))

    docling_texts = [
        repair_text(t.text, visual=t.prov[0].page_no in inputs.visual_pages)
        for _, document in inputs.documents
        for t in document.texts
        if t.prov and t.text
    ]
    document_texts = [*docling_texts, *(t for t in inputs.page_texts.values() if t)]

    parts: list[PartialStatement] = []
    metas: dict[tuple[StatementType, int], Metadata] = {}
    for grid, layout, result, headings in sorted(classified, key=lambda x: (x[0].page_no, x[0].table_ref)):
        assert result.type is not None
        previous = next(
            (p for p in reversed(parts) if p.type is result.type and p.last_page in (grid.page_no, grid.page_no - 1)),
            None,
        )
        if previous is not None:
            layout = inherit_periods(layout, grid, previous)
        if not layout.value_cols:
            continue
        source = TextSource.OCR if inputs.page_modes.get(grid.page_no) is PageMode.IMAGE else TextSource.TEXT
        part = build_part(grid, layout, result, source=source)
        parts.append(part)
        metas.setdefault(
            (part.type, part.first_page),
            detect_metadata(
                header_text=part.header_text,
                context_texts=[*headings, inputs.page_texts.get(grid.page_no, "")],
                document_texts=document_texts,
            ),
        )

    merged = sorted(merge_continuations(parts), key=lambda p: (p.type.value, -p.confidence, p.first_page))
    per_type = Counter(p.type for p in merged)
    decision_by_ref = {f"{d.docling_path}{d.table_ref}": d for d in decisions}
    statements: list[Statement] = []
    checks: list[CheckResult] = []
    for number, part in enumerate(merged, start=1):
        if per_type[part.type] > 1:
            part = part.model_copy(update={"flags": [*part.flags, f"ambiguous_statement:{part.type.value}"]})
        statement, results = run_checks(_statement(part, metas[(part.type, part.first_page)], inputs, index, number), index)
        statements.append(statement)
        checks.extend(results)
        for ref in part.table_refs:
            if ref in decision_by_ref:
                decision_by_ref[ref].statement_id = statement.id

    found = {s.type for s in statements}
    flags = [f"statement_not_extracted:{t.value}" for t in config.enabled_types if t not in found]
    flags += [
        f"range_not_converted:{r.first_page}-{r.last_page}"
        for r in inputs.convert.ranges
        if r.status in ("failed", "skipped")
    ]
    result = StructureResult(
        version=STRUCTURE_VERSION,
        sha256=inputs.sha256,
        convert_version=inputs.convert.version,
        settings_hash="",
        statements=statements,
        tables=decisions,
        flags=flags,
    )
    return result, checks


def _settings_hash(config: IngestConfig, convert: ConvertResult) -> str:
    payload = {
        "structure": STRUCTURE_VERSION,
        "convert": convert.settings_hash,
        "min_confidence": config.min_confidence,
        "taxonomy": load_taxonomy().version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _convert(pdf: Path, config: IngestConfig) -> ConvertResult:
    return convert_in_child(pdf, config)


def _stored_convert(path: Path) -> ConvertResult | None:
    if not path.is_file():
        return None
    try:
        stored = ConvertResult.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError:
        return None
    return stored if stored.version == CONVERT_VERSION else None


def structure_pdf(
    pdf: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    use_cache: bool = True,
    convert: Callable[[Path, IngestConfig], ConvertResult] = _convert,
) -> StructureResult:
    started = time.perf_counter()
    located = load_or_locate(pdf, config, ocr)
    out_dir = config.artifact_root / located.document.sha256
    converted = _stored_convert(out_dir / "convert.json") or convert(pdf, config)

    digest = _settings_hash(config, converted)
    target = out_dir / "statements.raw.json"
    if use_cache and target.is_file():
        try:
            cached = StructureResult.model_validate_json(target.read_text(encoding="utf-8"))
        except ValidationError:
            cached = None
        if cached is not None and cached.version == STRUCTURE_VERSION and cached.settings_hash == digest:
            return cached

    pages = read_pages(pdf, config, ocr, cache_dir=out_dir)
    kind = located.industry.kind
    inputs = StructureInputs(
        sha256=located.document.sha256,
        language=located.document.language,
        industry_flags=(f"likely_{kind}",) if kind in _FINANCIAL else (),
        page_modes={p.page_no: p.mode for p in pages},
        visual_pages={p.page_no for p in pages if p.visual_arabic},
        page_texts={p.page_no: repair_text(p.text, visual=False) for p in pages},
        title_types={p.page_no: tuple(p.title_types) for p in located.pages},
        cue_types={p.page_no: tuple(p.cue_types) for p in located.pages},
        documents=[
            (r.docling_path, load_docling_json(out_dir / r.docling_path))
            for r in converted.ranges
            if r.docling_path
        ],
        convert=converted,
    )
    result, checks = structure_document(inputs, config)
    result = result.model_copy(update={"settings_hash": digest, "timings": {"structure": time.perf_counter() - started}})
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(target, result.model_dump_json(indent=2))
    _write(out_dir / "table_checks.json", json.dumps([c.model_dump(mode="json") for c in checks], indent=2, ensure_ascii=False))
    return result
```

Run `make fmt` after pasting: several lines above are longer than the 100-character limit and ruff wraps them.

- [ ] **Step 5: Add the CLI subcommand**

In `cli.py`, add `"structure"` to the subcommand loop with help `"structure the converted statements"`, import `from fra_ingest.structure import structure_pdf` and `StructureResult`, and dispatch before the locate branch:

```python
        if args.command == "structure":
            result_s = structure_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
            print(result_s.model_dump_json(indent=2) if args.json else _structure_summary(result_s))
            return 0
```

```python
def _structure_summary(result: StructureResult) -> str:
    lines = [f"{result.sha256[:12]}  structure {result.version}"]
    for s in result.statements:
        periods = ", ".join(p.key for p in s.periods)
        lines.append(
            f"  {s.type.value:22} pp. {s.source_pages[0]}-{s.source_pages[-1]}  {len(s.line_items)} lines  "
            f"{s.currency} x{s.scale}  [{periods}]  {', '.join(s.flags) or 'ok'}"
        )
    rejected = sum(1 for t in result.tables if t.type is None)
    lines.append(f"tables  {len(result.tables)} ({rejected} not statements)")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    return "\n".join(lines)
```

Update the module docstring's command list with the `structure` line.

- [ ] **Step 6: Run the tests and iterate on the golden ones**

Run: `uv run pytest packages/ingest/tests/test_structure.py packages/ingest/tests/test_cli.py -q`
Expected: PASS.

Run: `uv run pytest packages/ingest/tests/test_structure_golden.py -q`
Expected: PASS. This is where the real documents are met; when a golden assertion fails, print the statement (`uv run fra-ingest structure eval/golden/documents/almarai-2025-ar-annualreport.pdf --json`), find which step produced the wrong value (grid, repair, header, continuation), and fix that step with a new fast test in its own test file first. Never special-case a document. Record each such fix in the task report.

- [ ] **Step 7: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/structure.py packages/ingest/src/fra_ingest/cli.py packages/ingest/tests/test_structure.py packages/ingest/tests/test_structure_golden.py packages/ingest/tests/test_cli.py
git commit -m "Add fra-ingest structure: statements with provenance, continuation and checks"
```

---

### Task 14: Golden structure eval and results

**Files:**
- Create: `eval/harness/structure.py`
- Create: `tests/eval/test_structure_harness.py`
- Modify: `Makefile` (`eval-structure`)
- Modify: `docs/blueprint/11-ingest-structure.md` (Results section)

**Interfaces:**
- Consumes: `structure_pdf` (Task 13), the golden manifest.
- Produces: `value_rows(statement) -> Counter[tuple[tuple[str, Decimal], ...]]`, `pair_misses(a, b) -> tuple[int, int]`, `metadata_ok(statement, expected_scale, expected_currency) -> bool`, `main(argv=None) -> int`. Writes `var/eval/structure-golden.json`.

- [ ] **Step 1: Write the failing tests**

```python
"""The structure eval's arithmetic (spec 11, Scoring)."""

from datetime import date
from decimal import Decimal

from harness.structure import metadata_ok, pair_misses, value_rows

from fra_core.schemas import BBox, Cell, LineItem, Period, PeriodKind, Provenance, Statement, StatementType

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def statement(values: list[str | None], scale: int = 1000, currency: str = "SAR", flags: list[str] | None = None) -> Statement:
    items = [
        LineItem(id=f"r{i}", raw_label=f"row {i}", cells=[] if v is None else [Cell(period_key=P.key, reported=Decimal(v), raw_text=v, provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=i, col=1))])
        for i, v in enumerate(values)
    ]
    return Statement(id="s", document_sha256="a" * 64, type=StatementType.BALANCE, currency=currency, scale=scale, periods=[P], line_items=items, flags=flags or [])


def test_rows_compare_after_scale_and_ignore_rows_without_values() -> None:
    assert value_rows(statement(["10", None, "5"])) == value_rows(statement(["5000", "10000"], scale=1))


def test_pair_misses_count_each_side() -> None:
    assert pair_misses(statement(["10", "5"]), statement(["10", "6"])) == (1, 1)
    assert pair_misses(statement(["10", "5"]), statement(["5", "10"])) == (0, 0)


def test_metadata_is_correct_or_flagged() -> None:
    assert metadata_ok(statement(["1"]), 1000, "SAR")
    assert not metadata_ok(statement(["1"], currency="EGP"), 1000, "SAR")
    assert metadata_ok(statement(["1"], currency="XXX", flags=["currency_missing"]), 1000, "SAR")
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/eval/test_structure_harness.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness.structure'`.

- [ ] **Step 3: Implement `eval/harness/structure.py`**

```python
"""Score the structure stage over the golden set (spec 11, Scoring).

    uv run python eval/harness/structure.py

Per document: statements per enabled type, scale and currency against the manifest, identity
status and flags. Per language pair: numeric rows without a counterpart. Only the Almarai pair
is gated. Writes var/eval/structure-golden.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from typing import Any

import yaml

from fra_core.schemas import Statement, StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.structure import structure_pdf

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
PAIRS = (
    ("almarai-2025-en", "almarai-2025-ar", True),
    ("juhayna-2025-en-consolidated", "juhayna-2025-ar-consolidated", False),
    ("juhayna-2024-en-consolidated", "juhayna-2024-ar-consolidated", False),
    ("edita-2025-en-consolidated-eas", "edita-2025-ar-consolidated", False),
)
_FLAGGED = {"scale_missing", "scale_conflict", "currency_missing", "currency_conflict"}


def value_rows(statement: Statement) -> Counter[tuple[tuple[str, Decimal], ...]]:
    rows: Counter[tuple[tuple[str, Decimal], ...]] = Counter()
    for item in statement.line_items:
        values = tuple(sorted((c.period_key, c.reported * statement.scale) for c in item.cells if c.reported is not None))
        if values:
            rows[values] += 1
    return rows


def pair_misses(a: Statement, b: Statement) -> tuple[int, int]:
    left, right = value_rows(a), value_rows(b)
    return sum((left - right).values()), sum((right - left).values())


def metadata_ok(statement: Statement, expected_scale: int, expected_currency: str) -> bool:
    if statement.scale == expected_scale and statement.currency == expected_currency:
        return True
    return bool(_FLAGGED & set(statement.flags))


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="eval/harness/structure.py").parse_args(argv)
    config = load_config()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[StatementType, Statement]] = {}
    reasons: list[str] = []
    for entry in documents:
        pdf = MANIFEST.parent / entry["file"]
        print(f"{entry['id']} ...", file=sys.stderr, flush=True)
        try:
            result = structure_pdf(pdf, config, None)
        except IngestError as exc:
            rows.append({"id": entry["id"], "error": f"{exc.reason} {exc.detail}".strip()})
            reasons.append(f"{entry['id']}: {exc.reason}")
            continue
        found: dict[StatementType, Statement] = {}
        for s in result.statements:
            found.setdefault(s.type, s)
        by_id[entry["id"]] = found
        row: dict[str, Any] = {"id": entry["id"], "flags": result.flags, "statements": {}}
        for statement_type in config.enabled_types:
            s = found.get(statement_type)
            if s is None:
                row["statements"][statement_type.value] = None
                continue
            ok = metadata_ok(s, int(entry["scale"]), str(entry["currency"]))
            identity = "failed" if "identity_failed" in s.flags else "skipped" if "identity_totals_not_found" in s.flags else "ok"
            row["statements"][statement_type.value] = {"pages": s.source_pages, "lines": len(s.line_items), "scale": s.scale, "currency": s.currency, "metadata_ok": ok, "identity": identity, "flags": s.flags}
            if not ok:
                reasons.append(f"{entry['id']} {statement_type.value}: scale/currency {s.scale} {s.currency} against {entry['scale']} {entry['currency']}")
            if statement_type is StatementType.BALANCE and identity == "failed" and not {"numbers_missing"} & {f for i in s.line_items for c in i.cells for f in c.flags}:
                reasons.append(f"{entry['id']}: identity failed with every value present")
        rows.append(row)
        print(f"{entry['id']:34} " + "  ".join(f"{k} {'-' if v is None else str(v['lines']) + ' lines ' + v['identity']}" for k, v in row["statements"].items()))

    pairs = []
    for left, right, gated in PAIRS:
        for statement_type in config.enabled_types:
            a, b = by_id.get(left, {}).get(statement_type), by_id.get(right, {}).get(statement_type)
            misses = pair_misses(a, b) if a and b else None
            pairs.append({"pair": f"{left}/{right}", "type": statement_type.value, "gated": gated, "misses": misses})
            print(f"pair {left:32} {right:32} {statement_type.value:22} {misses if misses is not None else 'missing statement'}")
            if gated and misses != (0, 0):
                reasons.append(f"{left}/{right} {statement_type.value}: {misses}")
    print("PASS" if not reasons else "FAIL\n  " + "\n  ".join(reasons))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "structure-golden.json").write_text(json.dumps({"documents": rows, "pairs": pairs, "reasons": reasons}, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests and add the Makefile target**

Run: `uv run pytest tests/eval/test_structure_harness.py -q`
Expected: PASS.

Add `eval-structure` to `.PHONY` and after `eval-convert`:

```makefile
eval-structure: ## Structure the golden set: statements, scale and currency, identity, language pairs
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/structure.py
```

- [ ] **Step 5: Commit the harness**

```bash
make test && make lint && make typecheck
git add eval/harness/structure.py tests/eval/test_structure_harness.py Makefile
git commit -m "Add the golden structure eval with the language pair check"
```

- [ ] **Step 6: Run it and record the results**

Run: `make eval-structure 2>&1 | tee var/eval/structure-golden.log`
Expected: `PASS`. On `FAIL`, name the cause of each reason from the document's `statements.raw.json` and report it; fixes are separate changes agreed with the owner, and never special-case a document.

Append to `docs/blueprint/11-ingest-structure.md`:

```markdown
## Results

Measured <date> on the <branch> branch, `make eval-structure`.

| Document | Balance | Income | Comprehensive income | Scale and currency | Identity |
|----------|---------|--------|----------------------|--------------------|----------|
| <one row per golden document from var/eval/structure-golden.json: lines per statement or "not extracted", metadata ok, identity ok / failed / skipped> |

| Pair | Balance | Income | Comprehensive income |
|------|---------|--------|----------------------|
| <one row per language pair: unmatched numeric rows each side> |

| Measure | Target | Result |
|---------|--------|--------|
| Almarai EN against AR | every numeric row matched | <result> |
| Scale and currency, golden set | 12 of 12 correct or flagged | <result> |
| Balance sheet identity, golden set | holds, or flagged with the correct reason | <result> |
| Resolution test (task 1) | recorded | <the decision from task 1> |
```

Fill every `<...>` with measured values; `make docs-check` must pass. Then:

```bash
make docs-check
git add docs/blueprint/11-ingest-structure.md
git commit -m "Record the golden structure results for ingest part 3a"
```

- [ ] **Step 7: Final checks**

```bash
make test && make lint && make typecheck
uv run pytest -m golden packages/ingest/tests/test_structure_golden.py packages/ingest/tests/test_docling_json.py -q
git log origin/main..HEAD --format='%an <%ae> | %cn <%ce>' | sort -u
```

Expected: all pass, and one identity line, `noah-mclain <nadam.30032415@gmail.com> | noah-mclain <nadam.30032415@gmail.com>`.
