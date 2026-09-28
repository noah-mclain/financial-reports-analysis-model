# Ingest Part 1 (Locate): Implementation Plan

Steps use checkbox (`- [ ]`) syntax for tracking. Work in the `ingest-locate` worktree.

**Goal:** Build `packages/ingest` far enough to find the statement pages of any English or
Arabic PDF (digital or scanned), attach an industry signal, and score both against the golden
set and the corpus pools.

**Architecture:** `read_pages` turns a PDF into cached per-page text (pypdfium2 text layer, or
Apple Vision OCR for image pages). Pure functions then score each page for five statement
types, group pages into ranges, pick the ranges docling will convert, and read the industry
from the statement pages. A CLI writes `locate.json`; a labelling tool and an eval harness
measure it.

**Tech Stack:** Python 3.12, uv workspace, pydantic 2, pypdfium2 5.13, ocrmac 1.0.1 (macOS
only, optional), PyYAML, Pillow, pytest, mypy strict, ruff.

**Spec:** `docs/blueprint/09-ingest-locate.md`

## Global Constraints

- Python `>=3.12,<3.13`; line length 100; `make lint`, `make typecheck` and `make test` pass
  before every commit.
- Tests first for anything that parses or computes (repository rule).
- `fra_ingest` imports on Linux: `ocrmac` is imported lazily and only through `VisionOcr`.
- Vision OCR runs in accurate mode only. Fast mode is never used.
- `statement_pages` in `eval/golden/manifest.yaml` is an answer key, never an input to code.
- Nobody develops against `blind`; the harness refuses it. `model_test` runs only with
  `--checkpoint`.
- No PDFs committed outside `eval/golden/documents/`. Test PDFs are generated at test time.
- Commits are authored as the owner; messages in the owner's plain style, no trailers.
- Repository hygiene rules in the root instructions file apply to every commit and document.

## Review Focus

1. **A PDF with zero pages** should end in `IngestError("empty_pdf")` or `unreadable_pdf`,
   never an index error. Test added in Task 3.
2. **A cache written without OCR, then a run with OCR available** must re-read the image
   pages; and a cache that has OCR must be reused by a later run without an engine rather than
   overwritten with empty pages. Tests added in Task 3.
3. **A fully scanned document on a machine with no OCR engine** must end with
   `image_pages_not_read:<n>`, `no_statements_found` and an `unknown` industry, not a
   corporate verdict on zero text. Test added in Task 7.
4. **Arabic-Indic digits with space thousands separators** (`٨ ٨٦٤ ٣٨٣ ٢٤٤`), years and
   note references glued to a value must be counted correctly by the numeric density. Tests
   added in Task 4.
5. **Arabic text in pypdfium2's extraction order.** 77 of 82 non-blind Arabic PDFs come out
   with each line's words in reverse order, and Almarai AR also reverses the letters of every
   word; lam-alef ligatures come out swapped (`اآلخر` for `الآخر`). Titles and cues must match
   in all three cases. Variant and folding tests in Task 4, golden checks in Task 3.

## File Map

| Path | Task | Responsibility |
|------|------|----------------|
| `packages/ingest/pyproject.toml` | 1 | Package metadata, `mac` extra, `fra-ingest` script |
| `packages/ingest/src/fra_ingest/__init__.py`, `py.typed` | 1 | Package marker |
| `packages/ingest/src/fra_ingest/config.py` | 1 | `IngestConfig`, `load_config` |
| `configs/ingest.toml` | 1 | Default settings |
| `packages/ingest/src/fra_ingest/ocr.py` | 2 | `OcrLine`, `OcrEngine`, `VisionOcr`, `default_engine` |
| `packages/ingest/src/fra_ingest/errors.py` | 3 | `IngestError` |
| `packages/ingest/src/fra_ingest/results.py` | 3-7 | Result models, one added per task |
| `packages/ingest/src/fra_ingest/text_match.py` | 3-4 | Visual-Arabic detection, reading variants, `PhraseIndex` |
| `packages/ingest/src/fra_ingest/pages.py` | 3 | `read_pages`, page cache, `profiles`, `document_language` |
| `packages/ingest/src/fra_ingest/data/statement_titles.yaml` | 4 | Titles, body cues, structural words, negatives |
| `packages/ingest/src/fra_ingest/locate.py` | 4, 5, 7 | `score_page`, `find_ranges`, `plan_conversion`, `locate` |
| `packages/ingest/src/fra_ingest/data/industry_cues.yaml` | 6 | Sector cues and exclusions |
| `packages/ingest/src/fra_ingest/industry.py` | 6 | `detect_industry` |
| `packages/ingest/src/fra_ingest/stage.py` | 7 | `locate_pdf`: read, locate, time, write `locate.json` |
| `packages/ingest/src/fra_ingest/cli.py` | 8 | `fra-ingest locate` |
| `eval/corpus/candidates.yaml`, `scripts/corpus.py` | 9 | `sector`, `subsector` on negative controls |
| `scripts/label_statement_pages.py` | 10 | Blind labelling sheet, reconcile, apply |
| `eval/harness/locate.py` | 11 | Scoring over golden set and pools |
| `packages/ingest/tests/*` | all | Tests; `support.py` holds `FakeOcr`, `make_blank_pdf`, `text_page`, `numbers_block`; `conftest.py` the `golden` fixture |

---

### Task 1: Package skeleton and settings

**Files:**
- Create: `packages/ingest/pyproject.toml`, `packages/ingest/src/fra_ingest/__init__.py`,
  `packages/ingest/src/fra_ingest/py.typed`, `packages/ingest/src/fra_ingest/config.py`,
  `configs/ingest.toml`, `packages/ingest/tests/test_config.py`
- Modify: `pyproject.toml` (root), `Makefile` (none yet)

**Interfaces:**
- Produces: `IngestConfig` (frozen pydantic model) with fields `enabled_types`,
  `optional_types`, `min_text_chars`, `header_fraction`, `ocr_dpi`, `ocr_languages`,
  `pad_pages`, `low_selectivity_share`, `artifact_root`; `load_config(path: Path | None = None)
  -> IngestConfig`; `REPO_ROOT: Path`.

- [ ] **Step 1: Create the package metadata**

`packages/ingest/pyproject.toml`:

```toml
[project]
name = "fra-ingest"
version = "0.1.0"
description = "Ingest: per-page text and OCR, statement page location, conversion and structuring."
requires-python = ">=3.12,<3.13"
dependencies = [
  "fra-core",
  "pillow>=11.0",
  "pydantic>=2.11",
  "pypdfium2>=5.13",
  "pyyaml>=6.0",
]

[project.optional-dependencies]
# Apple Vision OCR. Linux and Docker run without it until the week 2 engines land.
mac = ["ocrmac>=1.0.1; sys_platform == 'darwin'"]

[project.scripts]
fra-ingest = "fra_ingest.cli:main"

[tool.uv.sources]
fra-core = { workspace = true }

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fra_ingest"]
```

`packages/ingest/src/fra_ingest/__init__.py`:

```python
"""Ingest: from a PDF to located statement pages, and later to structured statements.

The only package that reads PDFs. It depends on fra-core and nothing else in the workspace.
"""
```

Create an empty `packages/ingest/src/fra_ingest/py.typed`.

- [ ] **Step 2: Register the member in the root `pyproject.toml`**

Change these parts of the root `pyproject.toml`:

```toml
dependencies = [
  "fra-core",
  "fra-ingest[mac]",
]
```

```toml
members = [
  "packages/core",
  "packages/ingest",
]

[tool.uv.sources]
fra-core = { workspace = true }
fra-ingest = { workspace = true }
```

In `[tool.pytest.ini_options]`:

```toml
pythonpath = ["packages/core/src", "packages/ingest/src", "scripts", "training/sources", "eval"]
```

In `[tool.mypy]`, let mypy find the ingest test helpers (`make typecheck` checks
`packages`, tests included):

```toml
mypy_path = "packages/core/src:packages/ingest/src:packages/ingest/tests:packages/analytics/src:packages/modeling/src"
```

In the mypy override list add `"ocrmac.*"` and `"ocrmac"`:

```toml
module = ["docling.*", "docling_core.*", "mlx.*", "mlx_lm.*", "pypdfium2.*", "matplotlib.*", "ocrmac", "ocrmac.*"]
```

Update the workspace comment above `members` so it no longer says ingest lands on day 3:

```toml
# Members are added on the day their package lands, so `uv sync` stays green at every commit.
# Still to come (see docs/blueprint/08-revised-plan.md): packages/analytics,
# packages/modeling, apps/api, apps/worker
```

Run: `make setup`
Expected: `environment ready: .venv`, and `uv run python -c "import ocrmac, fra_ingest"` exits 0.

- [ ] **Step 3: Write the failing tests**

`packages/ingest/tests/test_config.py`:

```python
"""Settings for the ingest stages, read from configs/ingest.toml."""

from pathlib import Path

import pytest

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, IngestConfig, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ingest.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_config_matches_the_defaults() -> None:
    assert load_config() == IngestConfig()


def test_defaults_convert_balance_income_and_comprehensive_income() -> None:
    config = IngestConfig()
    assert config.enabled_types == (
        StatementType.BALANCE,
        StatementType.INCOME,
        StatementType.COMPREHENSIVE_INCOME,
    )
    assert config.optional_types == (StatementType.CASH_FLOW, StatementType.EQUITY)


def test_enabling_cash_flow_is_a_config_change(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            '[statements]\nenabled = ["balance", "income", "cash_flow"]\noptional = ["equity"]\n',
        )
    )
    assert StatementType.CASH_FLOW in config.enabled_types
    assert config.optional_types == (StatementType.EQUITY,)


def test_a_type_cannot_be_both_enabled_and_optional(tmp_path: Path) -> None:
    path = write(tmp_path, '[statements]\nenabled = ["balance"]\noptional = ["balance"]\n')
    with pytest.raises(ValueError, match="both enabled and optional"):
        load_config(path)


def test_unknown_settings_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"unknown setting ocr\.mode"):
        load_config(write(tmp_path, '[ocr]\nmode = "fast"\n'))


def test_relative_artifact_root_resolves_against_the_repository(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, '[artifacts]\nroot = "var/elsewhere"\n'))
    assert config.artifact_root == REPO_ROOT / "var" / "elsewhere"
```

- [ ] **Step 4: Run the tests to see them fail**

Run: `uv run pytest packages/ingest/tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.config'`.

- [ ] **Step 5: Write the settings file and the loader**

`configs/ingest.toml`:

```toml
# Ingest settings. Every key maps to a field of fra_ingest.config.IngestConfig.

[statements]
# Detected always. Converted only when enabled. Move a type from optional to enabled to add it.
enabled = ["balance", "income", "comprehensive_income"]
optional = ["cash_flow", "equity"]

[text]
# Fewer non-whitespace characters than this in the text layer makes a page an image page.
min_text_chars = 50
# Top share of the page treated as its header, where statement titles are printed.
header_fraction = 0.35

[ocr]
# Accurate mode only. 72 dpi reads a scanned page in about 0.25 s once the model is loaded.
dpi = 72
languages = ["ar-SA", "en-US"]

[locate]
pad_pages = 1
# Candidate pages above this share of the document raise the low_selectivity flag.
low_selectivity_share = 0.25

[artifacts]
root = "var/artifacts"
```

`packages/ingest/src/fra_ingest/config.py`:

```python
"""Ingest settings, read from ``configs/ingest.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas import StatementType

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "ingest.toml"

# (section, key) in the TOML file, to the field it sets.
_TOML_FIELDS: dict[tuple[str, str], str] = {
    ("statements", "enabled"): "enabled_types",
    ("statements", "optional"): "optional_types",
    ("text", "min_text_chars"): "min_text_chars",
    ("text", "header_fraction"): "header_fraction",
    ("ocr", "dpi"): "ocr_dpi",
    ("ocr", "languages"): "ocr_languages",
    ("locate", "pad_pages"): "pad_pages",
    ("locate", "low_selectivity_share"): "low_selectivity_share",
    ("artifacts", "root"): "artifact_root",
}


class IngestConfig(BaseModel):
    """Every statement type is detected; only ``enabled_types`` are converted and gate recall."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled_types: tuple[StatementType, ...] = (
        StatementType.BALANCE,
        StatementType.INCOME,
        StatementType.COMPREHENSIVE_INCOME,
    )
    optional_types: tuple[StatementType, ...] = (StatementType.CASH_FLOW, StatementType.EQUITY)
    min_text_chars: int = Field(default=50, ge=0)
    header_fraction: float = Field(default=0.35, gt=0.0, lt=1.0)
    ocr_dpi: int = Field(default=72, ge=36, le=300)
    ocr_languages: tuple[str, ...] = ("ar-SA", "en-US")
    pad_pages: int = Field(default=1, ge=0)
    low_selectivity_share: float = Field(default=0.25, gt=0.0, le=1.0)
    artifact_root: Path = REPO_ROOT / "var" / "artifacts"

    @model_validator(mode="after")
    def _types_are_disjoint(self) -> IngestConfig:
        both = set(self.enabled_types) & set(self.optional_types)
        if both:
            msg = f"statement types both enabled and optional: {sorted(both)}"
            raise ValueError(msg)
        return self


def load_config(path: Path | None = None) -> IngestConfig:
    """Read settings, rejecting any key the model does not know."""
    source = path or DEFAULT_CONFIG_PATH
    with source.open("rb") as handle:
        data = tomllib.load(handle)

    values: dict[str, Any] = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            msg = f"{source}: [{section}] must be a table"
            raise ValueError(msg)
        for key, value in table.items():
            field_name = _TOML_FIELDS.get((section, key))
            if field_name is None:
                msg = f"{source}: unknown setting {section}.{key}"
                raise ValueError(msg)
            values[field_name] = value

    if "artifact_root" in values:
        root = Path(values["artifact_root"])
        values["artifact_root"] = root if root.is_absolute() else REPO_ROOT / root
    return IngestConfig.model_validate(values)
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `uv run pytest packages/ingest/tests/test_config.py -q`
Expected: 6 passed.

- [ ] **Step 7: Check and commit**

Run: `make lint typecheck test`
Expected: all pass.

```bash
git add packages/ingest configs/ingest.toml pyproject.toml uv.lock
git commit -m "Add the ingest package and its settings file"
```

---

### Task 2: OCR interface, and the language-order measurement (R21)

**Files:**
- Create: `packages/ingest/src/fra_ingest/ocr.py`, `packages/ingest/tests/support.py`,
  `packages/ingest/tests/conftest.py`, `packages/ingest/tests/test_ocr.py`
- Modify: `docs/blueprint/09-ingest-locate.md` (R21 row, after the measurement)

**Interfaces:**
- Produces: `OcrLine(text: str, confidence: float, left: float, top: float, width: float,
  height: float)` frozen dataclass with property `bottom`, all positions as fractions of the
  page with a top-left origin; `OcrEngine` protocol with `name: str` and
  `recognize(image: PIL.Image.Image, languages: Sequence[str]) -> list[OcrLine]`;
  `OcrUnavailableError`; `VisionOcr`; `default_engine() -> OcrEngine | None`;
  `line_from_vision(text, confidence, bbox) -> OcrLine`; `sort_lines(lines) -> list[OcrLine]`.
- Test helpers: `support.py` holds `GOLDEN_DIR: Path`, class `FakeOcr` and
  `make_blank_pdf(path, pages=1) -> Path`; `conftest.py` holds the fixture
  `golden(name) -> Path`, which skips when the file is missing. Tests import helpers with
  `from support import ...` (a name no other test directory uses).

- [ ] **Step 1: Write the shared test helpers**

`packages/ingest/tests/conftest.py`:

```python
"""Fixtures for the ingest tests. Plain helpers live in support.py."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from support import GOLDEN_DIR


@pytest.fixture
def golden() -> Callable[[str], Path]:
    def resolve(name: str) -> Path:
        path = GOLDEN_DIR / name
        if not path.exists():
            pytest.skip(f"golden document {name} is not present")
        return path

    return resolve
```

`packages/ingest/tests/support.py`:

```python
"""Helpers shared by the ingest tests."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from fra_ingest.ocr import OcrLine

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "eval" / "golden" / "documents"


class FakeOcr:
    """Returns fixed lines and counts calls. ``fail`` makes every call raise."""

    name = "fake"

    def __init__(self, lines: Sequence[OcrLine] = (), *, fail: bool = False) -> None:
        self.lines = list(lines)
        self.fail = fail
        self.calls = 0

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        self.calls += 1
        if self.fail:
            msg = "vision request failed"
            raise RuntimeError(msg)
        return list(self.lines)


def make_blank_pdf(path: Path, pages: int = 1) -> Path:
    """A PDF whose pages have no text layer, so every page is an image page."""
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)
    pdf.save(path)
    pdf.close()
    return path
```

- [ ] **Step 2: Write the failing tests**

`packages/ingest/tests/test_ocr.py`:

```python
"""OCR engine interface. Vision itself is exercised only in the slow tests."""

import sys
from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from fra_ingest.ocr import OcrLine, default_engine, line_from_vision, sort_lines


def test_vision_boxes_become_top_left_page_fractions() -> None:
    # Vision reports (x, y, width, height) with the origin at the bottom left.
    line = line_from_vision("Total assets", 0.9, (0.1, 0.7, 0.3, 0.05))
    assert line.top == pytest.approx(0.25)
    assert line.bottom == pytest.approx(0.30)
    assert line.left == pytest.approx(0.1)
    assert line.confidence == pytest.approx(0.9)


def test_lines_read_top_to_bottom_then_left_to_right() -> None:
    lines = [
        OcrLine("b", 1.0, left=0.5, top=0.201, width=0.1, height=0.02),
        OcrLine("c", 1.0, left=0.1, top=0.6, width=0.1, height=0.02),
        OcrLine("a", 1.0, left=0.1, top=0.2, width=0.1, height=0.02),
    ]
    assert [line.text for line in sort_lines(lines)] == ["a", "b", "c"]


def test_no_engine_without_ocrmac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ocrmac", None)
    assert default_engine() is None


@pytest.mark.slow
@pytest.mark.golden
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("juhayna-2025-en-consolidated.pdf", "KPMG"),
        ("juhayna-2025-ar-consolidated.pdf", "الرأي"),
    ],
)
def test_vision_reads_a_scanned_page(
    golden: Callable[[str], Path], name: str, expected: str
) -> None:
    engine = default_engine()
    if engine is None:
        pytest.skip("Apple Vision OCR needs macOS with ocrmac installed")
    pdf = pdfium.PdfDocument(golden(name))
    image = pdf[3].render(scale=1.0).to_pil()
    lines = engine.recognize(image, ("ar-SA", "en-US"))
    assert any(expected in line.text for line in lines)
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_ocr.py -q -m "not slow"`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.ocr'`.

- [ ] **Step 4: Implement `ocr.py`**

```python
"""OCR engines behind one interface.

Apple Vision is the only engine in week 1. Tesseract and RapidOCR join in week 2 behind the
same protocol, so nothing that calls ``recognize`` changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image


@dataclass(frozen=True)
class OcrLine:
    """One recognised line. Positions are fractions of the page, origin at the top left."""

    text: str
    confidence: float
    left: float
    top: float
    width: float
    height: float

    @property
    def bottom(self) -> float:
        return self.top + self.height


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]: ...


class OcrUnavailableError(RuntimeError):
    """The engine cannot run on this machine."""


def line_from_vision(
    text: str, confidence: float, bbox: tuple[float, float, float, float]
) -> OcrLine:
    """Vision boxes are (x, y, width, height) with the origin at the bottom left."""
    x, y, width, height = bbox
    return OcrLine(
        text=text,
        confidence=float(confidence),
        left=float(x),
        top=1.0 - float(y) - float(height),
        width=float(width),
        height=float(height),
    )


def sort_lines(lines: Iterable[OcrLine]) -> list[OcrLine]:
    """Reading order for grouping into header and body: rows first, then left to right."""
    return sorted(lines, key=lambda line: (round(line.top, 2), line.left))


class VisionOcr:
    """Apple Vision through ocrmac, accurate mode only: fast mode garbles English and
    rejects Arabic."""

    name = "vision"

    def __init__(self) -> None:
        try:
            from ocrmac import ocrmac
        except ImportError as exc:
            msg = "Apple Vision OCR needs macOS and the ocrmac package"
            raise OcrUnavailableError(msg) from exc
        self._ocrmac: Any = ocrmac

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        request = self._ocrmac.OCR(
            image, recognition_level="accurate", language_preference=list(languages)
        )
        return sort_lines(
            line_from_vision(text, confidence, bbox)
            for text, confidence, bbox in request.recognize()
        )


def default_engine() -> OcrEngine | None:
    """The engine for this machine, or None when there is none."""
    try:
        return VisionOcr()
    except OcrUnavailableError:
        return None
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `uv run pytest packages/ingest/tests/test_ocr.py -q`
Expected: 5 passed on the Mac (the two slow ones take about a minute: model load).

- [ ] **Step 6: Measure the language order (R21)**

Save this in the session scratchpad, not in the repository, and run it from the worktree root
with `uv run python <scratchpad>/lang_order.py`:

```python
import difflib

import pypdfium2 as pdfium

from fra_ingest.ocr import VisionOcr

GOLDEN = "eval/golden/documents/"
CASES = [
    ("juhayna-2025-en-consolidated.pdf", ["en-US"]),
    ("edita-2025-en-consolidated-eas.pdf", ["en-US"]),
    ("juhayna-2025-ar-consolidated.pdf", ["ar-SA"]),
    ("edita-2025-ar-consolidated-eas.pdf", ["ar-SA"]),
]
engine = VisionOcr()
for name, single in CASES:
    pdf = pdfium.PdfDocument(GOLDEN + name)
    for index in range(2, 8):
        image = pdf[index].render(scale=1.0).to_pil()
        reference = "\n".join(line.text for line in engine.recognize(image, single))
        for order in (["ar-SA", "en-US"], ["en-US", "ar-SA"]):
            text = "\n".join(line.text for line in engine.recognize(image, order))
            ratio = difflib.SequenceMatcher(None, reference, text).ratio()
            print(f"{name[:28]:28} p{index + 1:<3} {'+'.join(order):12} {ratio:.3f}")
```

Decision rule: if every `ar-SA+en-US` ratio is 0.95 or higher, the configured order stays and
R21 is closed. Otherwise stop and bring the table to the owner: the fallback in the spec (a
36 dpi pass to guess the script) becomes its own task before Task 3.

- [ ] **Step 7: Record the result in the spec and commit**

Replace the R21 mitigation cell in `docs/blueprint/09-ingest-locate.md` with the measured
outcome, for example: `Measured 2026-09-27 on 24 pages of Juhayna and Edita scans: ar-SA
first scores 0.97 to 1.00 against a single-language read in both scripts, so one pass with
both languages stays.` Use the real numbers.

```bash
git add packages/ingest docs/blueprint/09-ingest-locate.md
git commit -m "Add the OCR engine interface with Apple Vision, and record the language order check"
```

---

### Task 3: Per-page text, visual-order Arabic detection and the page cache

**Files:**
- Create: `packages/ingest/src/fra_ingest/errors.py`, `packages/ingest/src/fra_ingest/results.py`,
  `packages/ingest/src/fra_ingest/text_match.py` (detector only in this task),
  `packages/ingest/src/fra_ingest/pages.py`, `packages/ingest/tests/test_pages.py`,
  `packages/ingest/tests/test_pages_golden.py`

**Interfaces:**
- Consumes: `IngestConfig`; `OcrEngine`, `OcrLine`; `fra_core.numbers.strip_bidi`;
  `fra_core.schemas.PageMode`, `PageProfile`, `TextSource`.
- Produces:
  - `IngestError(reason: Literal["unreadable_pdf", "encrypted_pdf", "empty_pdf"], detail: str = "")`
    with `.reason`.
  - `PageText` (pydantic) with `page_no`, `mode: PageMode`, `source: TextSource | None`,
    `header_text`, `body_text`, `char_count`, `width_pt`, `height_pt`, `arabic_chars`,
    `latin_chars`, `visual_arabic: bool`, `ocr_seconds: float`, `flags: list[str]`, and the
    property `text` (header, newline, body).
  - `is_visual_arabic(text: str) -> bool` in `text_match.py`.
  - `sha256_file(path) -> str`, `read_pages(pdf_path, config, ocr, *, cache_dir=None) ->
    list[PageText]`, `profiles(pages) -> list[PageProfile]`,
    `document_language(pages) -> str`, `PAGES_STAGE_VERSION = "1"` in `pages.py`.

- [ ] **Step 1: Write the failing unit tests**

`packages/ingest/tests/test_pages.py`:

```python
"""Per-page text: text layer where there is one, OCR where there is not, cached per document."""

from pathlib import Path

import pypdfium2 as pdfium
import pytest

from support import FakeOcr, make_blank_pdf
from fra_core.schemas import PageMode, TextSource
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrLine
from fra_ingest.pages import document_language, profiles, read_pages
from fra_ingest.text_match import is_visual_arabic

TITLE = OcrLine("Statement of financial position", 0.98, left=0.2, top=0.05, width=0.6, height=0.03)
ROW = OcrLine("Total assets 1,234 1,100", 0.97, left=0.1, top=0.7, width=0.8, height=0.02)


def test_image_pages_are_read_by_ocr_and_split_at_the_header(tmp_path: Path) -> None:
    ocr = FakeOcr([TITLE, ROW])
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf", pages=2), IngestConfig(), ocr)

    assert ocr.calls == 2
    assert [p.page_no for p in pages] == [1, 2]
    assert pages[0].mode is PageMode.IMAGE
    assert pages[0].source is TextSource.OCR
    assert pages[0].header_text == "Statement of financial position"
    assert pages[0].body_text == "Total assets 1,234 1,100"
    assert pages[0].latin_chars > 0


def test_without_an_engine_image_pages_are_flagged_not_blank(tmp_path: Path) -> None:
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf"), IngestConfig(), None)
    assert pages[0].mode is PageMode.IMAGE
    assert pages[0].source is None
    assert pages[0].flags == ["ocr_unavailable"]


def test_an_ocr_failure_is_flagged_and_the_run_continues(tmp_path: Path) -> None:
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf", 2), IngestConfig(), FakeOcr(fail=True))
    assert [p.flags for p in pages] == [["ocr_failed"], ["ocr_failed"]]


def test_the_cache_is_reused(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    again = FakeOcr(fail=True)
    pages = read_pages(pdf, IngestConfig(), again, cache_dir=tmp_path / "cache")
    assert again.calls == 0
    assert pages[0].header_text == TITLE.text


def test_a_cache_without_ocr_is_not_reused_once_an_engine_exists(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), None, cache_dir=tmp_path / "cache")
    ocr = FakeOcr([TITLE])
    pages = read_pages(pdf, IngestConfig(), ocr, cache_dir=tmp_path / "cache")
    assert ocr.calls == 1
    assert pages[0].source is TextSource.OCR


def test_a_cache_with_ocr_serves_a_run_without_an_engine(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    pages = read_pages(pdf, IngestConfig(), None, cache_dir=tmp_path / "cache")
    assert pages[0].source is TextSource.OCR
    assert pages[0].flags == []


def test_changing_the_ocr_resolution_invalidates_the_cache(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    ocr = FakeOcr([TITLE])
    read_pages(pdf, IngestConfig(ocr_dpi=100), ocr, cache_dir=tmp_path / "cache")
    assert ocr.calls == 1


def test_a_truncated_file_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 truncated")
    with pytest.raises(IngestError) as caught:
        read_pages(path, IngestConfig(), None)
    assert caught.value.reason == "unreadable_pdf"


def test_a_password_protected_file_is_reported_as_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise pdfium.PdfiumError("Failed to load document (PDFium: Incorrect password error).", err_code=4)

    monkeypatch.setattr(pdfium, "PdfDocument", refuse)
    with pytest.raises(IngestError) as caught:
        read_pages(tmp_path / "locked.pdf", IngestConfig(), None)
    assert caught.value.reason == "encrypted_pdf"


def test_a_pdf_without_pages_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    pdf = pdfium.PdfDocument.new()
    pdf.save(path)
    pdf.close()
    with pytest.raises(IngestError) as caught:
        read_pages(path, IngestConfig(), None)
    # pdfium 5.13 refuses to open a zero-page file, so this arrives as unreadable_pdf.
    assert caught.value.reason in {"unreadable_pdf", "empty_pdf"}


def test_profiles_and_language(tmp_path: Path) -> None:
    arabic = OcrLine("قائمة المركز المالي", 0.9, left=0.3, top=0.05, width=0.4, height=0.03)
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf"), IngestConfig(), FakeOcr([arabic]))
    assert profiles(pages)[0].mode is PageMode.IMAGE
    assert profiles(pages)[0].width_pt == pytest.approx(595)
    assert document_language(pages) == "ar"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ةدحوملا يلاملا زكرملا ةمئاق\nتادوجوملا ةلوادتملا ريغ تادوجوملا", True),
        ("قائمة المركز المالي الموحدة\nالموجودات غير المتداولة الموجودات", False),
        ("Statement of financial position", False),
        ("", False),
    ],
)
def test_visual_order_arabic_is_detected(text: str, expected: bool) -> None:
    assert is_visual_arabic(text) is expected
```

- [ ] **Step 2: Write the failing golden tests**

`packages/ingest/tests/test_pages_golden.py`:

```python
"""read_pages on real golden documents."""

from collections.abc import Callable
from pathlib import Path

import pytest

from support import FakeOcr
from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.pages import read_pages

pytestmark = pytest.mark.golden

BIDI_CONTROLS = set("‎‏‪‫‬‭‮⁦⁧⁨⁩")


def test_text_layer_is_decided_page_by_page(golden: Callable[[str], Path]) -> None:
    ocr = FakeOcr()
    pages = read_pages(golden("juhayna-2025-en-standalone.pdf"), IngestConfig(), ocr)
    image_pages = [p.page_no for p in pages if p.mode is PageMode.IMAGE]
    assert image_pages == [3, 4, 5]
    assert ocr.calls == 3


def test_most_arabic_text_layers_reverse_word_order_only(golden: Callable[[str], Path]) -> None:
    pages = read_pages(golden("juhayna-2025-ar-standalone.pdf"), IngestConfig(), FakeOcr())
    assert not any(page.visual_arabic for page in pages)  # scanned: OCR gives logical order


def test_arabic_text_layer_has_no_bidi_controls_and_is_marked_visual(
    golden: Callable[[str], Path],
) -> None:
    pages = read_pages(golden("almarai-2025-ar-annualreport.pdf"), IngestConfig(), FakeOcr())
    balance = pages[155]
    assert not BIDI_CONTROLS & set(balance.text)
    assert balance.visual_arabic
    assert "ةمئاق" in balance.header_text


def test_english_statement_header_holds_the_title(golden: Callable[[str], Path]) -> None:
    pages = read_pages(golden("almarai-2025-en-annualreport.pdf"), IngestConfig(), FakeOcr())
    assert "Consolidated Statement of Financial Position" in pages[155].header_text
    assert "Consolidated Statement of Profit or Loss" in pages[158].header_text
    assert not pages[155].visual_arabic
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_pages.py packages/ingest/tests/test_pages_golden.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.errors'`.

- [ ] **Step 4: Implement `errors.py`**

```python
"""Failures that stop ingest for a document, each with a reason the UI can show."""

from __future__ import annotations

from typing import Literal

IngestErrorReason = Literal["unreadable_pdf", "encrypted_pdf", "empty_pdf"]


class IngestError(Exception):
    def __init__(self, reason: IngestErrorReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: IngestErrorReason = reason
        self.detail = detail
```

- [ ] **Step 5: Implement `results.py` with `PageText`**

```python
"""What the locate stage produces. Everything here is serialised to the artifact store."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import PageMode, TextSource


class PageText(BaseModel):
    """The text of one page, split into the header region and the rest."""

    model_config = ConfigDict(extra="forbid")

    page_no: int = Field(ge=1)
    mode: PageMode
    source: TextSource | None = Field(
        description="None when an image page could not be read because no OCR engine ran"
    )
    header_text: str = ""
    body_text: str = ""
    char_count: int = Field(ge=0, description="Non-whitespace characters in the text layer")
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)
    arabic_chars: int = Field(default=0, ge=0)
    latin_chars: int = Field(default=0, ge=0)
    visual_arabic: bool = Field(
        default=False,
        description="Arabic words extracted with their letters reversed (Almarai AR)",
    )
    ocr_seconds: float = Field(default=0.0, ge=0.0)
    flags: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.header_text}\n{self.body_text}"
```

- [ ] **Step 6: Implement the detector in `text_match.py`**

```python
"""Text matching shared by the locator and the industry signal.

Some PDF generators store Arabic in visual order, so the text layer yields every Arabic word
with its letters reversed (Almarai's Arabic annual report: ``ةمئاق`` for ``قائمة``). A word
never starts with ta marbuta or alef maqsura and never ends with the article, so counting
those shapes tells the two orders apart.
"""

from __future__ import annotations

import re

_ARABIC_WORD = re.compile(r"[ء-يٱ-ۓ]+")
_MIN_EVIDENCE = 3


def is_visual_arabic(text: str) -> bool:
    """Whether the Arabic words in ``text`` read backwards."""
    backwards = forwards = 0
    for word in _ARABIC_WORD.findall(text):
        if len(word) <= 2:
            continue
        if word.startswith(("ة", "ى")) or word.endswith("لا"):
            backwards += 1
        if word.endswith(("ة", "ى")) or word.startswith("ال"):
            forwards += 1
    return backwards >= _MIN_EVIDENCE and backwards > 2 * forwards
```

- [ ] **Step 7: Implement `pages.py`**

```python
"""Per-page text for the locator: the text layer where there is one, OCR where there is not.

Only OCR is expensive, so only this stage is cached. The cache is keyed by document, stage
version and every setting that changes what is read (ADR 0005).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from fra_core.numbers import strip_bidi
from fra_core.schemas import PageMode, PageProfile, TextSource
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngine
from fra_ingest.results import PageText
from fra_ingest.text_match import is_visual_arabic

PAGES_STAGE_VERSION = "1"

_FPDF_ERR_PASSWORD = 4
_TEXT_SETTINGS = ("min_text_chars", "header_fraction", "ocr_dpi", "ocr_languages")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_pages(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    cache_dir: Path | None = None,
) -> list[PageText]:
    """Read every page. ``cache_dir`` holds ``pages.v<N>.json``; None disables the cache."""
    settings = _settings(config, ocr)
    cache_file = cache_dir / f"pages.v{PAGES_STAGE_VERSION}.json" if cache_dir else None
    if cache_file is not None:
        cached = _load_cache(cache_file, settings)
        if cached is not None:
            return cached

    pdf = _open(pdf_path)
    try:
        if len(pdf) == 0:
            raise IngestError("empty_pdf", str(pdf_path))
        pages = [_read_page(pdf[index], index + 1, config, ocr) for index in range(len(pdf))]
    finally:
        pdf.close()

    if cache_file is not None:
        _write_cache(cache_file, settings, pages)
    return pages


def profiles(pages: Sequence[PageText]) -> list[PageProfile]:
    return [
        PageProfile(
            page_no=page.page_no,
            mode=page.mode,
            char_count=page.char_count,
            width_pt=page.width_pt,
            height_pt=page.height_pt,
        )
        for page in pages
    ]


def document_language(pages: Sequence[PageText]) -> str:
    arabic = sum(page.arabic_chars for page in pages)
    latin = sum(page.latin_chars for page in pages)
    return "ar" if arabic > latin else "en"


def _open(path: Path) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == _FPDF_ERR_PASSWORD:
            raise IngestError("encrypted_pdf", str(path)) from exc
        raise IngestError("unreadable_pdf", f"{path}: {exc}") from exc


def _read_page(
    page: pdfium.PdfPage, page_no: int, config: IngestConfig, ocr: OcrEngine | None
) -> PageText:
    try:
        width, height = page.get_size()
        textpage = page.get_textpage()
        try:
            full = strip_bidi(textpage.get_text_range())
            split_y = height * (1.0 - config.header_fraction)
            header = strip_bidi(textpage.get_text_bounded(0, split_y, width, height))
            body = strip_bidi(textpage.get_text_bounded(0, 0, width, split_y))
        finally:
            textpage.close()

        char_count = sum(1 for char in full if not char.isspace())
        if char_count >= config.min_text_chars:
            return _page(page_no, PageMode.TEXT, TextSource.TEXT, header, body, char_count,
                         width, height)
        if ocr is None:
            return _page(page_no, PageMode.IMAGE, None, "", "", char_count, width, height,
                         flags=["ocr_unavailable"])
        return _ocr_page(page, page_no, config, ocr, char_count, width, height)
    finally:
        page.close()


def _ocr_page(
    page: pdfium.PdfPage,
    page_no: int,
    config: IngestConfig,
    ocr: OcrEngine,
    char_count: int,
    width: float,
    height: float,
) -> PageText:
    image = page.render(scale=config.ocr_dpi / 72).to_pil()
    started = time.perf_counter()
    try:
        lines = ocr.recognize(image, config.ocr_languages)
    except Exception:  # Vision failures surface as assorted Objective-C bridge errors.
        return _page(page_no, PageMode.IMAGE, TextSource.OCR, "", "", char_count, width, height,
                     flags=["ocr_failed"])
    elapsed = time.perf_counter() - started
    header = "\n".join(line.text for line in lines if line.top < config.header_fraction)
    body = "\n".join(line.text for line in lines if line.top >= config.header_fraction)
    return _page(page_no, PageMode.IMAGE, TextSource.OCR, header, body, char_count, width,
                 height, ocr_seconds=elapsed)


def _page(
    page_no: int,
    mode: PageMode,
    source: TextSource | None,
    header: str,
    body: str,
    char_count: int,
    width: float,
    height: float,
    *,
    flags: list[str] | None = None,
    ocr_seconds: float = 0.0,
) -> PageText:
    text = f"{header}\n{body}"
    return PageText(
        page_no=page_no,
        mode=mode,
        source=source,
        header_text=header,
        body_text=body,
        char_count=char_count,
        width_pt=width,
        height_pt=height,
        arabic_chars=sum(1 for char in text if "؀" <= char <= "ۿ"),
        latin_chars=sum(1 for char in text if char.isascii() and char.isalpha()),
        visual_arabic=mode is PageMode.TEXT and is_visual_arabic(text),
        ocr_seconds=ocr_seconds,
        flags=flags or [],
    )


def _settings(config: IngestConfig, ocr: OcrEngine | None) -> dict[str, Any]:
    return {
        "min_text_chars": config.min_text_chars,
        "header_fraction": config.header_fraction,
        "ocr_dpi": config.ocr_dpi,
        "ocr_languages": list(config.ocr_languages),
        "ocr_engine": ocr.name if ocr is not None else None,
    }


def _usable(payload: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Same text settings, and OCR at least as good as this run's: a cache with OCR also
    serves a run without an engine, never the other way round."""
    if payload.get("version") != PAGES_STAGE_VERSION:
        return False
    cached = payload.get("settings", {})
    if any(cached.get(key) != settings[key] for key in _TEXT_SETTINGS):
        return False
    engine, cached_engine = settings["ocr_engine"], cached.get("ocr_engine")
    return cached_engine == engine or (engine is None and cached_engine is not None)


def _load_cache(path: Path, settings: dict[str, Any]) -> list[PageText] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _usable(payload, settings):
        return None
    return [PageText.model_validate(page) for page in payload["pages"]]


def _write_cache(path: Path, settings: dict[str, Any], pages: Sequence[PageText]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PAGES_STAGE_VERSION,
        "settings": settings,
        "pages": [page.model_dump(mode="json") for page in pages],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
```

Run `uv run ruff format packages/ingest` after pasting; it will reflow the wrapped calls.

- [ ] **Step 8: Run the tests to see them pass**

Run: `uv run pytest packages/ingest -q -m "not slow"`
Expected: all pass. The golden tests read three full documents and take a few seconds.

- [ ] **Step 9: Check and commit**

Run: `make lint typecheck test`

```bash
git add packages/ingest
git commit -m "Read page text from the text layer or OCR, with a per-document cache"
```

---

### Task 4: Page scoring

**Files:**
- Modify: `packages/ingest/src/fra_ingest/text_match.py`, `packages/ingest/src/fra_ingest/results.py`
- Create: `packages/ingest/src/fra_ingest/data/statement_titles.yaml`,
  `packages/ingest/src/fra_ingest/locate.py`, `packages/ingest/tests/test_text_match.py`,
  `packages/ingest/tests/test_score_page.py`
- Modify: `packages/ingest/tests/support.py` (add `text_page`, `numbers_block`)

**Interfaces:**
- Consumes: `PageText`, `is_visual_arabic`; `fra_core.labels.normalize_label`;
  `fra_core.numbers.normalize_digits`; `fra_core.units.detect_scale`;
  `fra_core.schemas.StatementType`.
- Produces:
  - `text_match.reading_variants(text: str, visual: bool) -> list[str]`
  - `text_match.PhraseIndex.build(groups: Mapping[str, Iterable[str]]) -> PhraseIndex` and
    `PhraseIndex.find(texts: Sequence[str]) -> dict[str, frozenset[str]]` (normalized phrase to
    the group names it belongs to)
  - `results.PageScore` with `page_no`, `type_scores: dict[StatementType, float]`,
    `title_types`, `cue_types` (lists of `StatementType`), `title_hits: list[str]`,
    `numeric_tokens: int`, `structure: list[str]`, `negatives: list[str]`,
    `continuation: bool`, and method `is_candidate(type) -> bool`
  - `locate.TitleBook`, `locate.load_title_book(path: Path | None = None) -> TitleBook`,
    `locate.count_numeric_tokens(text: str) -> int`,
    `locate.score_page(page: PageText, book: TitleBook) -> PageScore`, and the weight
    constants `TITLE_WEIGHT`, `CUE_WEIGHT`, `NUMERIC_WEIGHT`, `NUMERIC_FULL_AT`,
    `STRUCTURE_WEIGHT`, `NEGATIVE_WEIGHT`, `CANDIDATE_THRESHOLD`, `CONTINUATION_MIN_NUMBERS`

- [ ] **Step 1: Add page builders to `support.py`**

Append:

```python
from fra_core.schemas import PageMode, TextSource  # noqa: E402
from fra_ingest.results import PageText  # noqa: E402


def text_page(header: str, body: str = "", *, page_no: int = 1, visual: bool = False) -> PageText:
    return PageText(
        page_no=page_no,
        mode=PageMode.TEXT,
        source=TextSource.TEXT,
        header_text=header,
        body_text=body,
        char_count=len((header + body).replace(" ", "")),
        width_pt=595,
        height_pt=842,
        visual_arabic=visual,
    )


def numbers_block(rows: int = 25, label: str = "Line") -> str:
    """Statement-like rows with two period columns of values in the thousands."""
    return "\n".join(
        f"{label} {i} {1000 + i * 37:,} {900 + i * 29:,}" for i in range(rows)
    )
```

Move the new imports to the top of `support.py` with the others and drop the `noqa` comments.

- [ ] **Step 2: Write the failing text matching tests**

`packages/ingest/tests/test_text_match.py`:

```python
"""Phrase matching on normalized text, and reading visual-order Arabic."""

from fra_ingest.text_match import PhraseIndex, reading_variants


def test_phrases_match_whole_words_after_normalization() -> None:
    index = PhraseIndex.build({"balance": ["Statement of Financial Position", "إجمالي الأصول"]})
    assert index.find(["CONSOLIDATED STATEMENT OF FINANCIAL POSITION"]) == {
        "statement of financial position": frozenset({"balance"})
    }
    # Keys are canonical forms: normalize_label, then lam-alef folded.
    assert index.find(["اجمالي الأصول"])  # hamza forms fold together
    assert not index.find(["statement of financial positions"])


def test_the_longest_phrase_wins_and_consumes_its_words() -> None:
    index = PhraseIndex.build(
        {"income": ["قائمة الدخل"], "comprehensive_income": ["قائمة الدخل الشامل"]}
    )
    assert set(index.find(["قائمة الدخل الشامل الموحدة"]).values()) == {
        frozenset({"comprehensive_income"})
    }


def test_a_phrase_listed_under_two_groups_reports_both() -> None:
    combined = "statement of profit or loss and other comprehensive income"
    index = PhraseIndex.build({"income": [combined], "comprehensive_income": [combined]})
    assert index.find([combined]) == {combined: frozenset({"income", "comprehensive_income"})}


def test_visual_arabic_is_read_in_both_word_orders() -> None:
    variants = reading_variants("ةدحوملا يلاملا زكرملا ةمئاق\nتاحاضيإ ۳۱ ربمسيد م٢٠٢٥", True)
    assert "قائمة المركز المالي الموحدة" in variants[1]
    assert "إيضاحات ۳۱ ديسمبر م٢٠٢٥" in variants[0]


def test_arabic_lines_are_also_read_with_their_words_reversed() -> None:
    variants = reading_variants("المالي المركز قائمة\n2025 2024", False)
    assert variants[0] == "المالي المركز قائمة\n2025 2024"
    assert variants[1].startswith("قائمة المركز المالي")
    index = PhraseIndex.build({"balance": ["قائمة المركز المالي"]})
    assert index.find(variants)


def test_text_without_arabic_has_one_variant() -> None:
    assert reading_variants("Statement of financial position", False) == [
        "Statement of financial position"
    ]


def test_swapped_lam_alef_ligatures_still_match() -> None:
    index = PhraseIndex.build({"comprehensive_income": ["الدخل الشامل الآخر"]})
    assert index.find(["الدخل الشامل اآلخر"])  # ligature extracted as alef-madda, lam
```

- [ ] **Step 3: Write the failing scoring tests**

`packages/ingest/tests/test_score_page.py`:

```python
"""Which pages hold which statement, from their text alone."""

import pytest

from support import numbers_block, text_page
from fra_core.schemas import StatementType
from fra_ingest.locate import count_numeric_tokens, load_title_book, score_page

BOOK = load_title_book()
BALANCE = StatementType.BALANCE
INCOME = StatementType.INCOME
COMPREHENSIVE = StatementType.COMPREHENSIVE_INCOME

EN_HEADER = "Consolidated Statement of Financial Position\nNotes 31 December 2025 31 December 2024\nSAR '000 SAR '000"


def test_an_english_balance_sheet_is_a_candidate() -> None:
    score = score_page(text_page(EN_HEADER, "ASSETS\n" + numbers_block() + "\nTotal assets 9,999,999"), BOOK)
    assert score.is_candidate(BALANCE)
    assert not score.is_candidate(INCOME)
    assert score.structure == ["period", "note_column", "scale"]


def test_an_egyptian_arabic_balance_sheet_is_a_candidate() -> None:
    header = "قائمة المركز المالي المجمعة\nفي ٣١ ديسمبر ٢٠٢٥\nإيضاح ٢٠٢٥ ٢٠٢٤\n(جميع المبالغ بالألف جنيه مصري)"
    body = "\n".join(f"بند {i} ٨ ٨٦٤ ٣٨٣ ٢٤٤ ٧ ٩١٢ ٠٠٠ ١١١" for i in range(20))
    assert score_page(text_page(header, body + "\nإجمالي الأصول"), BOOK).is_candidate(BALANCE)


def test_a_gulf_arabic_balance_sheet_is_a_candidate() -> None:
    header = "قائمة المركز المالي الموحدة\nإيضاحات ٣١ ديسمبر ٢٠٢٥م ٣١ ديسمبر ٢٠٢٤م\nبالآلاف"
    body = "الموجودات\n" + numbers_block(label="بند") + "\nمجموع الموجودات"
    assert score_page(text_page(header, body), BOOK).is_candidate(BALANCE)


def test_reversed_arabic_from_a_visual_text_layer_is_still_found() -> None:
    header = "ةدحوملا يلاملا زكرملا ةمئاق\nتاحاضيإ ۳۱ ربمسيد م٢٠٢٥ ۳۱ ربمسيد م۲۰۲٤\nفالآب X"
    score = score_page(text_page(header, numbers_block(label="دنب"), visual=True), BOOK)
    assert score.is_candidate(BALANCE)


def test_an_auditors_report_quoting_the_titles_is_not_a_candidate() -> None:
    header = "Independent auditor's report to the shareholders of Example Company"
    body = (
        "We have audited the consolidated statement of financial position as at 31 December 2025 "
        "and the consolidated statement of profit or loss for the year then ended."
    )
    score = score_page(text_page(header, body), BOOK)
    assert "auditor_report" in score.negatives
    assert not any(score.is_candidate(t) for t in StatementType)


def test_an_auditors_report_page_with_titles_in_its_header_is_not_a_candidate() -> None:
    header = (
        "the consolidated statement of financial position as at 31 December 2025 and the "
        "consolidated statement of profit or loss for the year then ended"
    )
    score = score_page(text_page(header, "In our opinion the financial statements present fairly."), BOOK)
    assert not any(score.is_candidate(t) for t in StatementType)


def test_a_contents_page_is_not_a_candidate() -> None:
    header = "Contents\nConsolidated statement of financial position 5\nConsolidated statement of profit or loss 6"
    assert not score_page(text_page(header), BOOK).is_candidate(BALANCE)


def test_a_notes_page_with_a_table_is_not_a_candidate() -> None:
    header = "Notes to the consolidated financial statements\nFor the year ended 31 December 2025\nSAR '000 2025 2024"
    body = "Property, plant and equipment\nTotal assets\n" + numbers_block()
    assert not score_page(text_page(header, body), BOOK).is_candidate(BALANCE)


def test_a_continuation_page_is_marked() -> None:
    header = "Consolidated statement of financial position (continued)\n2025 2024"
    assert score_page(text_page(header, numbers_block()), BOOK).continuation


def test_a_garbled_title_is_rescued_by_cues_and_structure() -> None:
    header = "Consolidated statment of flnancial pos1tion\nNote 31 December 2025 2024\nEGP '000"
    body = numbers_block() + "\nTotal assets 12,345,678 11,234,567"
    score = score_page(text_page(header, body), BOOK)
    assert score.title_types == []
    assert score.is_candidate(BALANCE)


def test_a_page_with_no_title_at_all_is_found_by_structure() -> None:
    header = "Note 2025 2024\nEGP thousands"
    body = numbers_block() + "\nTotal current liabilities 1,234,567 1,111,111\nTotal assets 9,876,543 9,000,000"
    assert score_page(text_page(header, body), BOOK).is_candidate(BALANCE)


def test_a_combined_statement_counts_for_income_and_comprehensive_income() -> None:
    header = "Statement of profit or loss and other comprehensive income\nNote 2025 2024\nEGP '000"
    score = score_page(text_page(header, numbers_block()), BOOK)
    assert score.is_candidate(INCOME)
    assert score.is_candidate(COMPREHENSIVE)


def test_a_separate_comprehensive_income_statement_is_not_income() -> None:
    header = "قائمة الدخل الشامل المستقلة\nإيضاح ٢٠٢٥ ٢٠٢٤\nبالألف جنيه مصري"
    score = score_page(text_page(header, numbers_block(label="بند")), BOOK)
    assert score.is_candidate(COMPREHENSIVE)
    assert INCOME not in score.title_types


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("٨ ٨٦٤ ٣٨٣ ٢٤٤ ٧ ٩١٢ ٠٠٠ ١١١", 2),
        ("22,064,876 20,979,51233", 2),
        ("31 December 2025 2024", 0),
        ("(509,663) (538,024)", 2),
        ("12 45 7", 0),
        ("١٠,٠٠٠,٠٠٠ ٢,٩٦٦,١٦٥", 2),
    ],
)
def test_numeric_tokens(text: str, expected: int) -> None:
    assert count_numeric_tokens(text) == expected
```

- [ ] **Step 4: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_text_match.py packages/ingest/tests/test_score_page.py -q`
Expected: FAIL with `ImportError: cannot import name 'PhraseIndex'`.

- [ ] **Step 5: Add `reading_variants` and `PhraseIndex` to `text_match.py`**

Append to `text_match.py` (add `from collections.abc import Iterable, Mapping, Sequence`,
`from dataclasses import dataclass` and `from fra_core.labels import normalize_label` to the
imports):

```python
def reading_variants(text: str, visual: bool) -> list[str]:
    """Texts to search for phrases.

    pypdfium2 returns most Arabic lines with their words in reverse order (77 of 82 Arabic
    corpus PDFs), and a line can mix that with left-to-right runs such as dates. So any text
    holding Arabic is searched as extracted and with each line's words reversed. Visual-order
    text (Almarai AR) first has the letters of each Arabic word restored.
    """
    if not _ARABIC_WORD.search(text):
        return [text]
    lines = [line.split() for line in text.splitlines()]
    if visual:
        lines = [
            [_ARABIC_WORD.sub(lambda match: match.group(0)[::-1], word) for word in words]
            for words in lines
        ]
    same_order = "\n".join(" ".join(words) for words in lines)
    reversed_order = "\n".join(" ".join(reversed(words)) for words in lines)
    return [same_order, reversed_order]


def canonical(text: str) -> str:
    """``normalize_label``, then lam-alef folded to alef-lam. Text layers often split the
    lam-alef ligature in the wrong order (``اآلخر`` for ``الآخر``, ``المعامالت`` for
    ``المعاملات``); folding both sides the same way makes the two spellings meet."""
    return normalize_label(text).replace("لا", "ال")


@dataclass(frozen=True)
class PhraseIndex:
    """Phrases in normalized form, longest first, each with the groups it belongs to."""

    entries: tuple[tuple[str, frozenset[str]], ...]

    @classmethod
    def build(cls, groups: Mapping[str, Iterable[str]]) -> PhraseIndex:
        owners: dict[str, set[str]] = {}
        for group, phrases in groups.items():
            for phrase in phrases:
                normalized = canonical(phrase)
                if normalized:
                    owners.setdefault(normalized, set()).add(group)
        ordered = sorted(owners.items(), key=lambda item: len(item[0]), reverse=True)
        return cls(tuple((phrase, frozenset(names)) for phrase, names in ordered))

    def find(self, texts: Sequence[str]) -> dict[str, frozenset[str]]:
        """Phrases present in any of ``texts`` as whole words. A match consumes its words, so
        a shorter phrase inside a longer one that matched is not reported again."""
        found: dict[str, frozenset[str]] = {}
        for text in texts:
            haystack = f" {canonical(text)} "
            for phrase, groups in self.entries:
                needle = f" {phrase} "
                if needle in haystack:
                    found[phrase] = groups
                    haystack = haystack.replace(needle, " | ")
        return found
```

- [ ] **Step 6: Add `PageScore` to `results.py`**

Append (add `from fra_core.schemas import StatementType` to the imports):

```python
class PageScore(BaseModel):
    """Evidence that a page holds each statement type, kept for review and tuning."""

    model_config = ConfigDict(extra="forbid")

    page_no: int = Field(ge=1)
    type_scores: dict[StatementType, float]
    title_types: list[StatementType] = Field(default_factory=list)
    cue_types: list[StatementType] = Field(
        default_factory=list, description="Only filled when the header carries no title"
    )
    title_hits: list[str] = Field(default_factory=list)
    numeric_tokens: int = Field(default=0, ge=0)
    structure: list[str] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)
    continuation: bool = False

    def is_candidate(self, statement_type: StatementType, threshold: float = 4.5) -> bool:
        named = statement_type in self.title_types or statement_type in self.cue_types
        return named and self.type_scores.get(statement_type, 0.0) >= threshold
```

- [ ] **Step 7: Write `statement_titles.yaml`**

`packages/ingest/src/fra_ingest/data/statement_titles.yaml`:

```yaml
# Phrases for the statement page locator. Compared after fra_core.labels.normalize_label, so
# case, punctuation, hamza forms, ta marbuta and diacritics do not matter. Titles are matched
# in the page header only; body cues anywhere on the page.

titles:
  balance:
    - statement of financial position
    - balance sheet
    - قائمة المركز المالي
    - بيان المركز المالي
    - الميزانية العمومية
  income:
    - statement of profit or loss
    - statement of profit and loss
    - income statement
    - statement of income
    - statement of profit or loss and other comprehensive income
    - قائمة الدخل
    - قائمة الأرباح أو الخسائر
    - قائمة الربح أو الخسارة
    - بيان الربح أو الخسارة
    - بيان الدخل
    - قائمة الربح أو الخسارة والدخل الشامل الآخر
  comprehensive_income:
    - statement of comprehensive income
    - statement of profit or loss and other comprehensive income
    - قائمة الدخل الشامل
    - بيان الدخل الشامل
    - قائمة الربح أو الخسارة والدخل الشامل الآخر
  cash_flow:
    - statement of cash flows
    - cash flow statement
    - قائمة التدفقات النقدية
    - بيان التدفقات النقدية
  equity:
    - statement of changes in equity
    - statement of changes in shareholders equity
    - قائمة التغيرات في حقوق الملكية
    - قائمة التغير في حقوق الملكية
    - قائمة التغيرات في حقوق المساهمين
    - بيان التغيرات في حقوق الملكية

# Line items that show the type of a page whose title is missing or unreadable.
body_cues:
  balance:
    - total assets
    - total equity and liabilities
    - total liabilities and equity
    - total current assets
    - total current liabilities
    - إجمالي الأصول
    - مجموع الأصول
    - إجمالي الموجودات
    - مجموع الموجودات
    - إجمالي حقوق الملكية والالتزامات
    - إجمالي المطلوبات وحقوق الملكية
    - الأصول المتداولة
    - الموجودات المتداولة
  income:
    - gross profit
    - cost of sales
    - cost of revenue
    - earnings per share
    - profit for the year
    - profit for the period
    - مجمل الربح
    - تكلفة المبيعات
    - تكلفة الإيرادات
    - ربحية السهم
    - نصيب السهم في الأرباح
    - ربح السنة
    - ربح الفترة
  comprehensive_income:
    - other comprehensive income
    - total comprehensive income
    - الدخل الشامل الآخر
    - إجمالي الدخل الشامل
  cash_flow:
    - cash flows from operating activities
    - net cash from operating activities
    - investing activities
    - financing activities
    - التدفقات النقدية من الأنشطة التشغيلية
    - الأنشطة الاستثمارية
    - الأنشطة التمويلية
  equity:
    - balance at
    - transfer to statutory reserve
    - الرصيد في
    - المحول إلى الاحتياطي النظامي

continuation:
  - continued
  - cont'd
  - تابع

note_headers:
  - note
  - notes
  - إيضاح
  - إيضاحات

period_words:
  - december
  - june
  - march
  - september
  - ديسمبر
  - يونيو
  - مارس
  - سبتمبر

# Header wording of pages that quote statement titles without being statements.
negatives:
  auditor_report:
    - independent auditor's report
    - independent auditors' report
    - report on the audit of the
    - basis for opinion
    - key audit matters
    - تقرير مراجع الحسابات المستقل
    - تقرير المراجع المستقل
    - تقرير مراقب الحسابات
    - تقرير مراجعي الحسابات
    - أساس الرأي
    - أمور المراجعة الرئيسية
  contents:
    - contents
    - table of contents
    - المحتويات
    - الفهرس
  notes:
    - notes to the consolidated financial statements
    - notes to the financial statements
    - notes to the interim condensed
    - إيضاحات حول القوائم المالية
    - الإيضاحات المتممة
    - إيضاحات متممة
```

- [ ] **Step 8: Implement `score_page` in `locate.py`**

```python
"""Find the pages that hold each statement.

Pure functions over page text. A page scores for a type from its title in the header, line
item cues when the title is missing, numeric density, and structural cues every statement
page carries (period header, note column, scale line). Header wording of auditor's reports,
contents pages and notes subtracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from fra_core.numbers import normalize_digits
from fra_core.schemas import StatementType
from fra_core.units import detect_scale
from fra_ingest.results import PageScore, PageText
from fra_ingest.text_match import PhraseIndex, reading_variants

TITLE_WEIGHT = 3.0
CUE_WEIGHT = 1.5
NUMERIC_WEIGHT = 2.0
NUMERIC_FULL_AT = 30
STRUCTURE_WEIGHT = 1.0
NEGATIVE_WEIGHT = 4.0
CANDIDATE_THRESHOLD = 4.5
CONTINUATION_MIN_NUMBERS = 15

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_NUMBER = re.compile(r"\d{1,3}(?:[,٬. ]\d{3})+|\d{3,}")


@dataclass(frozen=True)
class TitleBook:
    titles: PhraseIndex
    body_cues: PhraseIndex
    continuation: PhraseIndex
    note_headers: PhraseIndex
    period_words: PhraseIndex
    negatives: PhraseIndex


def load_title_book(path: Path | None = None) -> TitleBook:
    if path is None:
        raw = resources.files("fra_ingest.data").joinpath("statement_titles.yaml").read_text("utf-8")
    else:
        raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    return TitleBook(
        titles=PhraseIndex.build(data["titles"]),
        body_cues=PhraseIndex.build(data["body_cues"]),
        continuation=PhraseIndex.build({"continued": data["continuation"]}),
        note_headers=PhraseIndex.build({"note": data["note_headers"]}),
        period_words=PhraseIndex.build({"period": data["period_words"]}),
        negatives=PhraseIndex.build(data["negatives"]),
    )


def count_numeric_tokens(text: str) -> int:
    """Printed amounts with three or more digits. Years are not amounts."""
    digits, _ = normalize_digits(text)
    return sum(1 for match in _NUMBER.finditer(digits) if not _YEAR.fullmatch(match.group(0)))


def score_page(page: PageText, book: TitleBook) -> PageScore:
    header = reading_variants(page.header_text, page.visual_arabic)
    whole = reading_variants(page.text, page.visual_arabic)

    title_hits = book.titles.find(header)
    title_types = _types(title_hits.values())
    cue_types = [] if title_types else _types(book.body_cues.find(whole).values())
    negatives = sorted({kind for kinds in book.negatives.find(header).values() for kind in kinds})
    numeric = count_numeric_tokens(page.text)
    structure = _structure(page, header, book)

    shared = (
        NUMERIC_WEIGHT * min(numeric / NUMERIC_FULL_AT, 1.0)
        + STRUCTURE_WEIGHT * len(structure)
        - NEGATIVE_WEIGHT * len(negatives)
    )
    type_scores = {
        statement_type: shared
        + (TITLE_WEIGHT if statement_type in title_types else 0.0)
        + (CUE_WEIGHT if statement_type in cue_types else 0.0)
        for statement_type in StatementType
    }
    return PageScore(
        page_no=page.page_no,
        type_scores=type_scores,
        title_types=title_types,
        cue_types=cue_types,
        title_hits=sorted(title_hits),
        numeric_tokens=numeric,
        structure=structure,
        negatives=negatives,
        continuation=bool(book.continuation.find(header)),
    )


def _types(groups: Any) -> list[StatementType]:
    names = {name for group in groups for name in group}
    return [statement_type for statement_type in StatementType if statement_type.value in names]


def _structure(page: PageText, header: list[str], book: TitleBook) -> list[str]:
    found: list[str] = []
    digits, _ = normalize_digits(page.header_text)
    years = set(_YEAR.findall(digits))
    if len(years) >= 2 or (years and book.period_words.find(header)):
        found.append("period")
    if book.note_headers.find(header):
        found.append("note_column")
    if any(detect_scale(variant) for variant in header):
        found.append("scale")
    return found
```

Add an empty `packages/ingest/src/fra_ingest/data/__init__.py` so `resources.files`
resolves the package.

- [ ] **Step 9: Run the tests to see them pass**

Run: `uv run pytest packages/ingest/tests/test_text_match.py packages/ingest/tests/test_score_page.py -q`
Expected: all pass. If a trap test fails, adjust phrases in the YAML, not the weights; the
weights are tuned against measured pages in Task 12, and every change there is recorded.

- [ ] **Step 10: Check and commit**

Run: `make lint typecheck test`

```bash
git add packages/ingest
git commit -m "Score pages for five statement types from titles, cues and structure"
```

---

### Task 5: Ranges and the pages to convert

**Files:**
- Modify: `packages/ingest/src/fra_ingest/results.py`, `packages/ingest/src/fra_ingest/locate.py`
- Create: `packages/ingest/tests/test_ranges.py`

**Interfaces:**
- Consumes: `PageScore`, `CANDIDATE_THRESHOLD`, `CONTINUATION_MIN_NUMBERS`.
- Produces:
  - `results.StatementRange(type: StatementType, first_page: int, last_page: int, score: float, rank: int)`
    with method `padded(pad: int, page_count: int) -> tuple[int, int]`
  - `locate.find_ranges(scores: Sequence[PageScore]) -> list[StatementRange]`, sorted by type
    order then rank
  - `locate.plan_conversion(ranges, enabled: Sequence[StatementType], page_count: int, pad: int) -> list[tuple[int, int]]`

- [ ] **Step 1: Write the failing tests**

`packages/ingest/tests/test_ranges.py`:

```python
"""Grouping candidate pages into statement ranges, and choosing what docling converts."""

from collections.abc import Iterable

from fra_core.schemas import StatementType
from fra_ingest.locate import find_ranges, plan_conversion
from fra_ingest.results import PageScore, StatementRange

B, I, C, E = (
    StatementType.BALANCE,
    StatementType.INCOME,
    StatementType.CASH_FLOW,
    StatementType.EQUITY,
)


def score(
    page_no: int,
    titled: Iterable[StatementType] = (),
    *,
    value: float = 8.0,
    numbers: int = 40,
    continuation: bool = False,
) -> PageScore:
    names = list(titled)
    return PageScore(
        page_no=page_no,
        type_scores={t: (value if t in names else 0.0) for t in StatementType},
        title_types=names,
        numeric_tokens=numbers,
        continuation=continuation,
    )


def spans(ranges: list[StatementRange], statement_type: StatementType) -> list[tuple[int, int]]:
    return [(r.first_page, r.last_page) for r in ranges if r.type is statement_type]


def test_consecutive_pages_of_one_type_form_one_range() -> None:
    ranges = find_ranges([score(1), score(2, [B]), score(3, [B]), score(4)])
    assert spans(ranges, B) == [(2, 4)]  # page 4: numeric, untitled, continues the range


def test_an_untitled_page_with_few_numbers_does_not_continue() -> None:
    ranges = find_ranges([score(2, [B]), score(3, numbers=3)])
    assert spans(ranges, B) == [(2, 2)]


def test_a_page_titled_as_another_type_ends_the_range() -> None:
    ranges = find_ranges([score(5, [B]), score(6, [I]), score(7)])
    assert spans(ranges, B) == [(5, 5)]
    assert spans(ranges, I) == [(6, 7)]


def test_separate_candidates_are_all_kept_and_ranked_by_score() -> None:
    ranges = find_ranges([score(3, [B], value=6.0), score(4, numbers=0), score(9, [B], value=9.0)])
    balance = [r for r in ranges if r.type is B]
    assert [(r.first_page, r.rank) for r in balance] == [(9, 1), (3, 2)]


def test_padding_is_clamped_to_the_document() -> None:
    first = StatementRange(type=B, first_page=1, last_page=2, score=8.0, rank=1)
    last = StatementRange(type=B, first_page=9, last_page=10, score=8.0, rank=1)
    assert first.padded(1, 10) == (1, 3)
    assert last.padded(1, 10) == (8, 10)


def test_conversion_merges_enabled_ranges_and_skips_optional_ones() -> None:
    ranges = [
        StatementRange(type=B, first_page=5, last_page=5, score=8.0, rank=1),
        StatementRange(type=I, first_page=7, last_page=7, score=8.0, rank=1),
        StatementRange(type=C, first_page=12, last_page=13, score=8.0, rank=1),
        StatementRange(type=I, first_page=30, last_page=30, score=5.0, rank=2),
    ]
    assert plan_conversion(ranges, [B, I], page_count=40, pad=1) == [(4, 8), (29, 31)]
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_ranges.py -q`
Expected: FAIL with `ImportError: cannot import name 'find_ranges'`.

- [ ] **Step 3: Add `StatementRange` to `results.py`**

```python
class StatementRange(BaseModel):
    """Consecutive pages holding one statement, before padding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: StatementType
    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    score: float
    rank: int = Field(ge=1, description="1 is the best candidate for this type")

    def padded(self, pad: int, page_count: int) -> tuple[int, int]:
        return max(1, self.first_page - pad), min(page_count, self.last_page + pad)
```

- [ ] **Step 4: Add `find_ranges` and `plan_conversion` to `locate.py`**

Add `from collections.abc import Sequence` and `StatementRange` to the imports, then:

```python
def find_ranges(scores: Sequence[PageScore]) -> list[StatementRange]:
    """Group candidate pages per type. An untitled numeric page directly after a range
    continues it, which covers statements printed over several pages."""
    ordered = sorted(scores, key=lambda score: score.page_no)
    ranges: list[StatementRange] = []
    for statement_type in StatementType:
        groups: list[list[PageScore]] = []
        open_group: list[PageScore] | None = None
        for score in ordered:
            adjacent = open_group is not None and score.page_no == open_group[-1].page_no + 1
            if score.is_candidate(statement_type, CANDIDATE_THRESHOLD):
                if open_group is not None and adjacent:
                    open_group.append(score)
                else:
                    open_group = [score]
                    groups.append(open_group)
            elif open_group is not None and adjacent and _continues(score):
                open_group.append(score)
            else:
                open_group = None
        found = [(max(page.type_scores[statement_type] for page in group), group) for group in groups]
        found.sort(key=lambda item: item[0], reverse=True)
        ranges.extend(
            StatementRange(
                type=statement_type,
                first_page=group[0].page_no,
                last_page=group[-1].page_no,
                score=best,
                rank=rank,
            )
            for rank, (best, group) in enumerate(found, start=1)
        )
    return ranges


def plan_conversion(
    ranges: Sequence[StatementRange],
    enabled: Sequence[StatementType],
    page_count: int,
    pad: int,
) -> list[tuple[int, int]]:
    """Padded ranges of the enabled types, merged where they touch or overlap."""
    spans = sorted(r.padded(pad, page_count) for r in ranges if r.type in enabled)
    merged: list[tuple[int, int]] = []
    for first, last in spans:
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _continues(score: PageScore) -> bool:
    if score.title_types:
        return False
    return score.continuation or score.numeric_tokens >= CONTINUATION_MIN_NUMBERS
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `uv run pytest packages/ingest/tests/test_ranges.py -q`
Expected: 6 passed.

- [ ] **Step 6: Check and commit**

Run: `make lint typecheck test`

```bash
git add packages/ingest
git commit -m "Group candidate pages into ranges and plan the pages to convert"
```

---

### Task 6: Industry signal

**Files:**
- Create: `packages/ingest/src/fra_ingest/data/industry_cues.yaml`,
  `packages/ingest/src/fra_ingest/industry.py`, `packages/ingest/tests/test_industry.py`
- Modify: `packages/ingest/src/fra_ingest/results.py`

**Interfaces:**
- Consumes: `PageText`, `StatementRange`, `PhraseIndex`, `reading_variants`.
- Produces:
  - `results.IndustrySignal(kind: Literal["corporate", "bank", "insurer", "other_financial", "unknown"], subkind: Literal["investment_holding", "brokerage", "exchange_operator", "consumer_finance", "asset_manager", "other"] | None, score: float, evidence: list[tuple[int, str]])`
  - `industry.IndustryBook`, `industry.load_industry_book(path: Path | None = None)`,
    `industry.detect_industry(pages, ranges, book) -> IndustrySignal`, `VERDICT_THRESHOLD`

- [ ] **Step 1: Write the failing tests**

`packages/ingest/tests/test_industry.py`:

```python
"""Bank, insurer and other financial companies, read from their statement pages."""

from support import numbers_block, text_page
from fra_core.schemas import StatementType
from fra_ingest.industry import detect_industry, load_industry_book
from fra_ingest.results import PageText, StatementRange

BOOK = load_industry_book()


def on_statement_pages(*bodies: str) -> tuple[list[PageText], list[StatementRange]]:
    pages = [text_page("Statement of financial position", body + "\n" + numbers_block(), page_no=n)
             for n, body in enumerate(bodies, start=1)]
    ranges = [StatementRange(type=StatementType.BALANCE, first_page=1, last_page=len(pages),
                             score=8.0, rank=1)]
    return pages, ranges


def test_a_bank() -> None:
    pages, ranges = on_statement_pages(
        "Cash and balances with the central bank\nLoans and advances to customers\nDeposits from customers"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert signal.kind == "bank"
    assert signal.subkind is None
    assert (1, "deposits from customers") in signal.evidence


def test_an_arabic_bank() -> None:
    pages, ranges = on_statement_pages("أرصدة لدى البنك المركزي\nقروض وسلف للعملاء\nودائع العملاء")
    assert detect_industry(pages, ranges, BOOK).kind == "bank"


def test_an_insurer() -> None:
    pages, ranges = on_statement_pages(
        "Insurance contract liabilities\nReinsurance contract assets\nInsurance revenue"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "insurer"


def test_an_arabic_insurer() -> None:
    pages, ranges = on_statement_pages("التزامات عقود التأمين\nموجودات عقود إعادة التأمين\nإيرادات التأمين")
    assert detect_industry(pages, ranges, BOOK).kind == "insurer"


def test_an_exchange_operator() -> None:
    pages, ranges = on_statement_pages("Trading commission income\nListing fees\nClearing and settlement fees")
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "exchange_operator")


def test_a_brokerage() -> None:
    pages, ranges = on_statement_pages("Brokerage commission income\nMargin lending to clients\nClients' money")
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "brokerage")


def test_an_investment_holding() -> None:
    pages, ranges = on_statement_pages(
        "Net gains on investments at fair value through profit or loss\nDividend income from investees\nPrivate equity investments"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "investment_holding")


def test_a_consumer_finance_company() -> None:
    pages, ranges = on_statement_pages(
        "Income from Islamic financing contracts\nNet investment in finance receivables\nConsumer finance receivables"
    )
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "consumer_finance")


def test_an_arabic_consumer_finance_company_with_reversed_word_order() -> None:
    # As pypdfium2 extracts most Arabic filings: words spelled right, line order reversed.
    pages, ranges = on_statement_pages("اإلسالمي التمويل عقود من إيرادات\nاالستهالكي التمويل مدينو")
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "consumer_finance")


def test_an_asset_manager() -> None:
    pages, ranges = on_statement_pages("Fund management fees\nAssets under management\nSubscription fees")
    signal = detect_industry(pages, ranges, BOOK)
    assert (signal.kind, signal.subkind) == ("other_financial", "asset_manager")


def test_ordinary_company_wording_stays_corporate() -> None:
    pages, ranges = on_statement_pages(
        "Cash and bank balances\nPrepaid insurance\nBank borrowings\nInvestments in associates"
    )
    assert detect_industry(pages, ranges, BOOK).kind == "corporate"


def test_cues_outside_statement_pages_do_not_decide_when_statements_were_found() -> None:
    pages, ranges = on_statement_pages("Revenue\nCost of sales")
    pages.append(text_page("Treasury", "Deposits from customers\nNet interest income", page_no=2))
    assert detect_industry(pages, ranges, BOOK).kind == "corporate"


def test_no_readable_text_is_unknown() -> None:
    pages = [text_page("", "", page_no=1)]
    assert detect_industry(pages, [], BOOK).kind == "unknown"
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_industry.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.industry'`.

- [ ] **Step 3: Add `IndustrySignal` to `results.py`**

Add `from typing import Literal` to the imports, then:

```python
IndustryKind = Literal["corporate", "bank", "insurer", "other_financial", "unknown"]
IndustrySubkind = Literal[
    "investment_holding", "brokerage", "exchange_operator", "consumer_finance", "asset_manager", "other"
]


class IndustrySignal(BaseModel):
    """What kind of company issued the document. Stored now; the decline rule is week 2."""

    model_config = ConfigDict(extra="forbid")

    kind: IndustryKind
    subkind: IndustrySubkind | None = Field(
        default=None, description="Set only when kind is other_financial"
    )
    score: float = 0.0
    evidence: list[tuple[int, str]] = Field(default_factory=list)
```

- [ ] **Step 4: Write `industry_cues.yaml`**

```yaml
# Cues for the industry signal, with weights. Groups are "<kind>" or "other_financial/<subkind>".
# Compared after fra_core.labels.normalize_label. Exclusions are removed from the text first,
# since ordinary companies print them too.

exclude:
  - cash and bank balances
  - bank balances
  - balances with banks
  - cash at banks
  - bank borrowings
  - bank overdrafts
  - prepaid insurance
  - insurance expense
  - أرصدة لدى البنوك
  - النقد لدى البنوك
  - قروض بنكية
  - تسهيلات بنكية
  - تأمين مدفوع مقدما

cues:
  bank:
    deposits from customers: 3
    customers' deposits: 3
    due to customers: 2
    loans and advances to customers: 3
    financing and advances: 3
    balances with the central bank: 3
    balances with central banks: 3
    statutory deposit with: 2
    net interest income: 2
    net special commission income: 3
    ودائع العملاء: 3
    قروض وسلف للعملاء: 3
    تمويل وسلف: 3
    أرصدة لدى البنك المركزي: 3
    وديعة نظامية: 2
    صافي دخل العمولات الخاصة: 3
    صافي دخل الفوائد: 2
  insurer:
    insurance contract liabilities: 3
    reinsurance contract assets: 3
    insurance revenue: 3
    insurance service result: 3
    gross written premiums: 3
    net claims incurred: 2
    التزامات عقود التأمين: 3
    موجودات عقود إعادة التأمين: 3
    إيرادات التأمين: 3
    نتيجة خدمات التأمين: 3
    إجمالي أقساط التأمين المكتتبة: 3
    صافي المطالبات المتكبدة: 2
  other_financial/exchange_operator:
    listing fees: 3
    clearing and settlement: 3
    trading commission income: 2
    trading fees: 2
    depository services: 2
    post trade: 2
    رسوم الإدراج: 3
    المقاصة والتسوية: 3
    عمولات التداول: 2
    رسوم التداول: 2
    خدمات الإيداع: 2
  other_financial/brokerage:
    brokerage commission: 3
    brokerage fees: 3
    margin lending: 2
    clients' money: 3
    client money: 3
    عمولات الوساطة: 3
    أموال العملاء: 2
    تمويل الهامش: 2
  other_financial/investment_holding:
    net gains on investments at fair value through profit or loss: 2
    dividend income from investees: 2
    private equity: 2
    أرباح الاستثمارات: 2
    إيرادات توزيعات من الشركات المستثمر فيها: 2
    الاستثمارات المباشرة: 2
  other_financial/consumer_finance:
    income from islamic financing contracts: 3
    consumer finance: 3
    net investment in finance receivables: 2
    finance lease receivables: 2
    instalment sales receivables: 2
    إيرادات من عقود التمويل الإسلامي: 3
    صافي الدخل من أنشطة التمويل الإسلامي: 3
    التمويل الاستهلاكي: 3
    مدينو التمويل: 2
    مدينو عقود التأجير التمويلي: 2
  other_financial/asset_manager:
    assets under management: 3
    fund management fees: 3
    asset management fees: 3
    management fees from funds: 2
    subscription fees: 2
    الأصول تحت الإدارة: 3
    أتعاب إدارة الصناديق: 3
    أتعاب إدارة الأصول: 3
    رسوم الاشتراك: 2
```

- [ ] **Step 5: Implement `industry.py`**

```python
"""Whether the issuer is a bank, an insurer or another financial company.

Line items show the kind of business, so cues are read from the balance sheet and income
statement pages when the locator found them, and from every page otherwise. The verdict is
stored on the locate result; declining is decided in week 2.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast

import yaml

from fra_core.schemas import StatementType
from fra_ingest.results import IndustryKind, IndustrySignal, IndustrySubkind, PageText, StatementRange
from fra_ingest.text_match import PhraseIndex, canonical, reading_variants

VERDICT_THRESHOLD = 6.0
_EVIDENCE_LIMIT = 12
_STATEMENT_TYPES = (StatementType.BALANCE, StatementType.INCOME)


@dataclass(frozen=True)
class IndustryBook:
    exclude: PhraseIndex
    cues: PhraseIndex
    weights: dict[tuple[str, str], float]  # (group, normalized phrase) -> weight


def load_industry_book(path: Path | None = None) -> IndustryBook:
    if path is None:
        raw = resources.files("fra_ingest.data").joinpath("industry_cues.yaml").read_text("utf-8")
    else:
        raw = path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)
    groups: dict[str, dict[str, float]] = data["cues"]
    return IndustryBook(
        exclude=PhraseIndex.build({"exclude": data["exclude"]}),
        cues=PhraseIndex.build({group: list(cues) for group, cues in groups.items()}),
        weights={
            (group, canonical(phrase)): float(weight)
            for group, cues in groups.items()
            for phrase, weight in cues.items()
        },
    )


def detect_industry(
    pages: list[PageText], ranges: list[StatementRange], book: IndustryBook
) -> IndustrySignal:
    readable = [page for page in pages if page.text.strip()]
    if not readable:
        return IndustrySignal(kind="unknown")

    statement_pages = {
        page_no
        for r in ranges
        if r.type in _STATEMENT_TYPES
        for page_no in range(r.first_page, r.last_page + 1)
    }
    evidence_pages = [p for p in readable if p.page_no in statement_pages] or readable

    totals: dict[str, float] = defaultdict(float)
    evidence: list[tuple[int, str]] = []
    for page in evidence_pages:
        variants = [_without(book, text) for text in reading_variants(page.text, page.visual_arabic)]
        for phrase, groups in book.cues.find(variants).items():
            for group in groups:
                totals[group] += book.weights[(group, phrase)]
            evidence.append((page.page_no, phrase))

    if not totals:
        return IndustrySignal(kind="corporate")
    group, score = max(totals.items(), key=lambda item: item[1])
    if score < VERDICT_THRESHOLD:
        return IndustrySignal(kind="corporate", score=score, evidence=evidence[:_EVIDENCE_LIMIT])
    kind, _, subkind = group.partition("/")
    return IndustrySignal(
        kind=cast(IndustryKind, kind),
        subkind=cast(IndustrySubkind, subkind) if subkind else None,
        score=score,
        evidence=evidence[:_EVIDENCE_LIMIT],
    )


def _without(book: IndustryBook, text: str) -> str:
    """The text with excluded phrases blanked, in normalized form."""
    normalized = f" {canonical(text)} "
    for phrase, _ in book.exclude.entries:
        # A placeholder word, so the words on either side cannot join into a cue.
        normalized = normalized.replace(f" {phrase} ", " excluded ")
    return normalized
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `uv run pytest packages/ingest/tests/test_industry.py -q`
Expected: 13 passed.

- [ ] **Step 7: Check and commit**

Run: `make lint typecheck test`

```bash
git add packages/ingest
git commit -m "Read an industry signal from statement pages, including other financial sub-kinds"
```

---

### Task 7: Assemble the locate result and write `locate.json`

**Files:**
- Modify: `packages/ingest/src/fra_ingest/results.py`, `packages/ingest/src/fra_ingest/locate.py`
- Create: `packages/ingest/src/fra_ingest/stage.py`, `packages/ingest/tests/test_locate.py`

**Interfaces:**
- Consumes: everything from Tasks 1 to 6.
- Produces:
  - `results.LocateResult(version, document: Document, pages: list[PageScore], ranges, convert_ranges: list[tuple[int, int]], industry: IndustrySignal, flags: list[str], timings: dict[str, float])`
    with property `candidate_share: float`
  - `locate.LOCATE_VERSION = "1"`
  - `locate.locate(pages, config, *, sha256, filename, book=None, industry_book=None, timings=None) -> LocateResult`
  - `locate.locate_flags(pages, ranges, convert, signal, config) -> list[str]`
  - `stage.locate_pdf(pdf_path: Path, config: IngestConfig, ocr: OcrEngine | None, *, use_cache: bool = True) -> LocateResult`
    which writes `<artifact_root>/<sha256>/locate.json`

- [ ] **Step 1: Write the failing tests**

`packages/ingest/tests/test_locate.py`:

```python
"""The assembled locate result and its flags."""

from pathlib import Path

from support import FakeOcr, make_blank_pdf, numbers_block, text_page
from fra_ingest.config import IngestConfig
from fra_ingest.locate import locate
from fra_ingest.results import LocateResult, PageText
from fra_ingest.stage import locate_pdf

SHA = "0" * 64
BALANCE_HEADER = "Statement of financial position\nNote 2025 2024\nEGP '000"
INCOME_HEADER = "Statement of profit or loss\nNote 2025 2024\nEGP '000"


def filler(page_no: int) -> PageText:
    return text_page("Chairman's letter", "We had a good year. " * 20, page_no=page_no)


def document(*pages: PageText) -> list[PageText]:
    return list(pages)


def test_a_simple_filing() -> None:
    pages = [filler(1), filler(2), text_page(BALANCE_HEADER, numbers_block(), page_no=3),
             text_page(INCOME_HEADER, numbers_block(), page_no=4)] + [filler(n) for n in range(5, 21)]
    result = locate(pages, IngestConfig(), sha256=SHA, filename="x.pdf")
    assert result.convert_ranges == [(2, 5)]
    assert result.flags == ["statement_not_found:comprehensive_income"]
    assert result.document.page_count == 20
    assert result.industry.kind == "corporate"
    assert result.candidate_share == 4 / 20


def test_nothing_found() -> None:
    result = locate([filler(n) for n in range(1, 6)], IngestConfig(), sha256=SHA, filename="x.pdf")
    assert result.convert_ranges == []
    assert "no_statements_found" in result.flags
    assert "statement_not_found:balance" in result.flags


def test_low_selectivity() -> None:
    pages = [text_page(BALANCE_HEADER, numbers_block(), page_no=n) for n in (1, 3, 5, 7)]
    pages += [filler(n) for n in (2, 4, 6, 8)]
    result = locate(sorted(pages, key=lambda p: p.page_no), IngestConfig(), sha256=SHA, filename="x.pdf")
    assert "low_selectivity" in result.flags


def test_a_scanned_filing_without_an_engine_is_visibly_unread(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    result = locate_pdf(make_blank_pdf(tmp_path / "scan.pdf", pages=4), config, None)
    assert "image_pages_not_read:4" in result.flags
    assert "no_statements_found" in result.flags
    assert result.industry.kind == "unknown"


def test_locate_json_round_trips(tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path / "artifacts")
    result = locate_pdf(make_blank_pdf(tmp_path / "scan.pdf", pages=2), config, FakeOcr())
    written = tmp_path / "artifacts" / result.document.sha256 / "locate.json"
    assert LocateResult.model_validate_json(written.read_text(encoding="utf-8")) == result
    assert {"read", "ocr", "score"} <= result.timings.keys()


def test_a_bank_is_flagged() -> None:
    body = numbers_block() + "\nDeposits from customers\nLoans and advances to customers\nBalances with the central bank"
    pages = [text_page(BALANCE_HEADER, body, page_no=1), text_page(INCOME_HEADER, numbers_block(), page_no=2)]
    result = locate(pages, IngestConfig(), sha256=SHA, filename="bank.pdf")
    assert "likely_bank" in result.flags
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_locate.py -q`
Expected: FAIL with `ImportError: cannot import name 'locate'`.

- [ ] **Step 3: Add `LocateResult` to `results.py`**

Add `from fra_core.schemas import Document` to the imports, then:

```python
class LocateResult(BaseModel):
    """Output of the locate stage, written to ``<artifact root>/<sha256>/locate.json``."""

    model_config = ConfigDict(extra="forbid")

    version: str
    document: Document
    pages: list[PageScore]
    ranges: list[StatementRange]
    convert_ranges: list[tuple[int, int]] = Field(
        description="Padded, merged ranges of the enabled types: the pages docling converts"
    )
    industry: IndustrySignal
    flags: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

    @property
    def candidate_share(self) -> float:
        pages = sum(last - first + 1 for first, last in self.convert_ranges)
        return pages / self.document.page_count
```

- [ ] **Step 4: Add `locate` and `locate_flags` to `locate.py`**

Add imports: `import time`, `from fra_core.schemas import Document`,
`from fra_ingest.config import IngestConfig`,
`from fra_ingest.industry import IndustryBook, detect_industry, load_industry_book`,
`from fra_ingest.pages import document_language, profiles`, and `IndustrySignal`,
`LocateResult`, `StatementRange` from results. Then:

```python
LOCATE_VERSION = "1"


def locate(
    pages: Sequence[PageText],
    config: IngestConfig,
    *,
    sha256: str,
    filename: str,
    book: TitleBook | None = None,
    industry_book: IndustryBook | None = None,
    timings: dict[str, float] | None = None,
) -> LocateResult:
    started = time.perf_counter()
    book = book or load_title_book()
    industry_book = industry_book or load_industry_book()

    scores = [score_page(page, book) for page in pages]
    ranges = find_ranges(scores)
    convert = plan_conversion(ranges, config.enabled_types, len(pages), config.pad_pages)
    signal = detect_industry(list(pages), ranges, industry_book)
    flags = locate_flags(pages, ranges, convert, signal, config)

    return LocateResult(
        version=LOCATE_VERSION,
        document=Document(
            sha256=sha256,
            filename=filename,
            page_count=len(pages),
            pages=profiles(pages),
            language=document_language(pages),
        ),
        pages=scores,
        ranges=ranges,
        convert_ranges=convert,
        industry=signal,
        flags=flags,
        timings={**(timings or {}), "score": time.perf_counter() - started},
    )


def locate_flags(
    pages: Sequence[PageText],
    ranges: Sequence[StatementRange],
    convert: Sequence[tuple[int, int]],
    signal: IndustrySignal,
    config: IngestConfig,
) -> list[str]:
    flags: list[str] = []
    unread = sum(1 for page in pages if "ocr_unavailable" in page.flags)
    if unread:
        flags.append(f"image_pages_not_read:{unread}")
    found = {r.type for r in ranges}
    flags.extend(f"statement_not_found:{t.value}" for t in config.enabled_types if t not in found)
    if not convert:
        flags.append("no_statements_found")
    elif pages:
        share = sum(last - first + 1 for first, last in convert) / len(pages)
        if share > config.low_selectivity_share:
            flags.append("low_selectivity")
    if signal.kind in ("bank", "insurer"):
        flags.append(f"likely_{signal.kind}")
    elif signal.kind == "other_financial":
        flags.append(f"likely_other_financial:{signal.subkind or 'other'}")
    return flags
```

- [ ] **Step 5: Implement `stage.py`**

```python
"""The locate stage end to end: read pages, locate, time it, write ``locate.json``."""

from __future__ import annotations

import os
import time
from pathlib import Path

from fra_ingest.config import IngestConfig
from fra_ingest.locate import locate
from fra_ingest.ocr import OcrEngine
from fra_ingest.pages import read_pages, sha256_file
from fra_ingest.results import LocateResult


def locate_pdf(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    use_cache: bool = True,
) -> LocateResult:
    sha256 = sha256_file(pdf_path)
    out_dir = config.artifact_root / sha256

    started = time.perf_counter()
    pages = read_pages(pdf_path, config, ocr, cache_dir=out_dir if use_cache else None)
    read_seconds = time.perf_counter() - started
    ocr_seconds = sum(page.ocr_seconds for page in pages)

    result = locate(
        pages,
        config,
        sha256=sha256,
        filename=pdf_path.name,
        timings={"read": read_seconds, "ocr": ocr_seconds},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = out_dir / "locate.json.tmp"
    temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, out_dir / "locate.json")
    return result
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `uv run pytest packages/ingest/tests/test_locate.py -q`
Expected: 6 passed. `test_a_simple_filing` depends on the weights: if it fails, print
`result.pages[2]` and check the scores before touching the test.

- [ ] **Step 7: Check and commit**

Run: `make lint typecheck test`

```bash
git add packages/ingest
git commit -m "Assemble the locate result with its flags and write locate.json"
```

---

### Task 8: `fra-ingest locate` command

**Files:**
- Create: `packages/ingest/src/fra_ingest/cli.py`, `packages/ingest/tests/test_cli.py`

**Interfaces:**
- Consumes: `load_config`, `default_engine`, `locate_pdf`, `IngestError`.
- Produces: `cli.main(argv: list[str] | None = None) -> int`. Options: `--json`, `--no-ocr`,
  `--no-cache`, `--config PATH`, `--artifacts DIR`. Exit 0 on success, 2 on `IngestError`.

- [ ] **Step 1: Write the failing tests**

`packages/ingest/tests/test_cli.py`:

```python
"""fra-ingest locate."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from fra_ingest.cli import main
from fra_ingest.results import LocateResult


@pytest.mark.golden
def test_locate_writes_a_valid_result(
    golden: Callable[[str], Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["locate", str(golden("juhayna-2025-en-standalone.pdf")), "--no-ocr",
                 "--artifacts", str(tmp_path), "--json"])
    assert code == 0
    result = LocateResult.model_validate(json.loads(capsys.readouterr().out))
    assert "image_pages_not_read:3" in result.flags
    assert (tmp_path / result.document.sha256 / "locate.json").exists()


def test_an_unreadable_file_exits_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    assert main(["locate", str(path), "--no-ocr", "--artifacts", str(tmp_path)]) == 2
    assert "unreadable_pdf" in capsys.readouterr().err
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.cli'`.

- [ ] **Step 3: Implement `cli.py`**

```python
"""Command line for the ingest stages.

    fra-ingest locate <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fra_ingest.config import load_config
from fra_ingest.errors import IngestError
from fra_ingest.ocr import default_engine
from fra_ingest.results import LocateResult
from fra_ingest.stage import locate_pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fra-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("locate", help="find the statement pages of a PDF")
    run.add_argument("pdf", type=Path)
    run.add_argument("--json", action="store_true", help="print the full result as JSON")
    run.add_argument("--no-ocr", action="store_true", help="leave image pages unread")
    run.add_argument("--no-cache", action="store_true", help="ignore the page text cache")
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--artifacts", type=Path, default=None, help="artifact root override")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.artifacts is not None:
        config = config.model_copy(update={"artifact_root": args.artifacts})
    engine = None if args.no_ocr else default_engine()

    try:
        result = locate_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
    except IngestError as exc:
        print(f"{args.pdf}: {exc.reason} {exc.detail}".rstrip(), file=sys.stderr)
        return 2

    print(result.model_dump_json(indent=2) if args.json else _summary(result))
    return 0


def _summary(result: LocateResult) -> str:
    document = result.document
    scanned = len(document.scanned_pages)
    lines = [
        f"{document.filename}  {document.page_count} pages ({scanned} image)  "
        f"language {document.language}",
        f"industry  {result.industry.kind}"
        + (f"/{result.industry.subkind}" if result.industry.subkind else "")
        + f"  score {result.industry.score:.1f}",
    ]
    for r in result.ranges:
        lines.append(f"  {r.type.value:22} pp. {r.first_page}-{r.last_page}  "
                     f"score {r.score:.1f}  rank {r.rank}")
    spans = ", ".join(f"{first}-{last}" for first, last in result.convert_ranges) or "none"
    lines.append(f"convert  {spans}  ({result.candidate_share:.0%} of pages)")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    lines.append("time  " + "  ".join(f"{k} {v:.1f}s" for k, v in result.timings.items()))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests, then try it by hand**

Run: `uv run pytest packages/ingest/tests/test_cli.py -q`
Expected: 2 passed.

Run: `uv run fra-ingest locate eval/golden/documents/almarai-2025-en-annualreport.pdf`
Expected: balance around pp. 156-158 and income around p. 159 among the ranges. Record what
it prints; tuning happens in Task 12, not here.

- [ ] **Step 5: Check and commit**

Run: `make lint typecheck test`

```bash
git add packages/ingest
git commit -m "Add the fra-ingest locate command"
```

---

### Task 9: Sector labels on the corpus negative controls

**Files:**
- Modify: `scripts/corpus.py`, `eval/corpus/candidates.yaml`, `tests/scripts/test_corpus.py`,
  `eval/corpus/README.md`

**Interfaces:**
- Produces: `corpus.SECTORS = ("bank", "insurer", "other_financial")`,
  `corpus.SUBSECTORS = ("investment_holding", "brokerage", "exchange_operator", "consumer_finance", "asset_manager", "other")`;
  `check` reports a missing or wrong `sector`/`subsector` as an error.

- [ ] **Step 1: Write the failing tests**

Append to `tests/scripts/test_corpus.py`:

```python
def control(doc_id: str, **fields: str) -> dict[str, Any]:
    return {**doc(doc_id, f"Issuer {doc_id}", "train"), "role": "negative_control", **fields}


def test_negative_controls_need_a_sector() -> None:
    report = check([control("a")], GOLDEN)
    assert report.errors == ["a: negative_control needs a sector (bank, insurer, other_financial)"]


def test_other_financial_needs_a_subsector() -> None:
    report = check([control("a", sector="other_financial")], GOLDEN)
    assert report.errors == [
        "a: other_financial needs a subsector (investment_holding, brokerage, "
        "exchange_operator, consumer_finance, asset_manager, other)"
    ]


def test_banks_take_no_subsector() -> None:
    report = check([control("a", sector="bank", subsector="brokerage")], GOLDEN)
    assert report.errors == ["a: subsector is only for other_financial"]


def test_corporates_take_no_sector() -> None:
    report = check([{**doc("a", "Savola Group", "train"), "sector": "bank"}], GOLDEN)
    assert report.errors == ["a: sector is only for negative_control"]


def test_labelled_controls_pass() -> None:
    report = check(
        [control("a", sector="bank"), control("b", sector="other_financial", subsector="brokerage")],
        GOLDEN,
    )
    assert report.errors == []
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/scripts/test_corpus.py -q`
Expected: the five new tests FAIL (no sector errors reported).

- [ ] **Step 3: Validate sectors in `scripts/corpus.py`**

Next to `ROLES`:

```python
SECTORS = ("bank", "insurer", "other_financial")
SUBSECTORS = (
    "investment_holding",
    "brokerage",
    "exchange_operator",
    "consumer_finance",
    "asset_manager",
    "other",
)
```

Inside the `for doc in documents:` loop of `check`, after the role check:

```python
        report.errors.extend(_sector_errors(doc_id, role, doc.get("sector"), doc.get("subsector")))
```

And the helper, below `check`:

```python
def _sector_errors(doc_id: str, role: Any, sector: Any, subsector: Any) -> list[str]:
    """Negative controls say what kind of financial company they are, for the industry eval."""
    if role != "negative_control":
        return [f"{doc_id}: sector is only for negative_control"] if sector else []
    if sector not in SECTORS:
        return [f"{doc_id}: negative_control needs a sector ({', '.join(SECTORS)})"]
    if sector == "other_financial" and subsector not in SUBSECTORS:
        return [f"{doc_id}: other_financial needs a subsector ({', '.join(SUBSECTORS)})"]
    if sector != "other_financial" and subsector:
        return [f"{doc_id}: subsector is only for other_financial"]
    return []
```

- [ ] **Step 4: Label the 24 negative controls**

Run this once from the worktree root; it rewrites only the `role: negative_control` entries:

```python
import re
from pathlib import Path

LABELS = {
    "alrajhi-2025-en": "bank",
    "credit-agricole-egypt-ar": "bank",
    "abu-dhabi-commercial-bank-2024-ar": "bank",
    "arab-banking-corporation-2025-ar": "bank",
    "saudi-national-bank-2024-en": "bank",
    "saudi-national-bank-2024-en-interim": "bank",
    "tawuniya-2025-en": "insurer",
    "malath-cooperative-insurance-2025-ar": "insurer",
    "orient-takaful-2025-ar": "insurer",
    "middle-east-financial-investment-2017-en": "other_financial/brokerage",
    "united-financial-services-2021-ar": "other_financial/consumer_finance",
    "b-investments-holding-2020-en-interim": "other_financial/investment_holding",
    "b-investments-holding-2023-en-interim": "other_financial/investment_holding",
    "coast-investment-and-development-2025-en-interim": "other_financial/investment_holding",
    "pioneers-holding-2017-ar-interim": "other_financial/investment_holding",
    "pioneers-holding-2018-ar": "other_financial/investment_holding",
    "contact-financial-holding-2022-ar": "other_financial/consumer_finance",
    "contact-financial-holding-2023-en-interim": "other_financial/consumer_finance",
    "contact-financial-holding-2024-ar-interim": "other_financial/consumer_finance",
    "musharaka-capital-2019-ar": "other_financial/asset_manager",
    "dubai-financial-market-2021-ar": "other_financial/exchange_operator",
    "dubai-financial-market-2021-ar-interim": "other_financial/exchange_operator",
    "saudi-tadawul-group-2025-en-interim": "other_financial/exchange_operator",
    "saudi-tadawul-group-2025-en-interim-795f": "other_financial/exchange_operator",
}
path = Path("eval/corpus/candidates.yaml")
text = path.read_text(encoding="utf-8")
for doc_id, label in LABELS.items():
    sector, _, subsector = label.partition("/")
    extra = f"sector: {sector}" + (f", subsector: {subsector}" if subsector else "")
    pattern = re.compile(rf"(\{{id: {re.escape(doc_id)},.*?role: negative_control)")
    text, count = pattern.subn(rf"\1, {extra}", text)
    assert count == 1, doc_id
path.write_text(text, encoding="utf-8")
```

Contact Financial Holding and United Financial Services (revenue is income from Islamic
financing contracts) are `consumer_finance`; Musharaka Capital is `asset_manager` (owner's
decision, 2026-09-27). `other` stays available for companies that fit none of them.

Add one line to the `role:` comment at the top of `candidates.yaml`:

```yaml
# sector:    bank | insurer | other_financial   (negative controls only)
# subsector: investment_holding | brokerage | exchange_operator | consumer_finance |
#            asset_manager | other   (other_financial only)
```

In `eval/corpus/README.md`, rule 4, add: "Each carries `sector`, and other financial companies
a `subsector`, so the industry signal can be scored (`docs/blueprint/09-ingest-locate.md`)."

- [ ] **Step 5: Run the tests and the corpus check**

Run: `uv run pytest tests/scripts/test_corpus.py -q && make corpus-check`
Expected: tests pass; `corpus-check` reports no errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/corpus.py eval/corpus tests/scripts/test_corpus.py
git commit -m "Label bank, insurer and other financial controls by sector"
```

---

### Task 10: Blind labelling tool for the scanned golden documents

**Files:**
- Create: `scripts/label_statement_pages.py`, `tests/scripts/test_label_statement_pages.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `load_config`, `VisionOcr`, `read_pages`, `sha256_file`, `load_title_book`,
  `score_page`.
- Produces: `parse_ranges(text: str) -> list[tuple[int, int]]`,
  `manifest_block(labels: dict[str, list[tuple[int, int]]]) -> list[str]`,
  `apply_to_manifest(text: str, doc_id: str, labels) -> str`,
  `disagreements(marked: set[int], suggested: set[int]) -> tuple[list[int], list[int]]`;
  subcommands `serve`, `reconcile`, `apply`. Label files at `var/labels/<doc id>.json` as
  `{"financial_position": [[5, 5]], ...}`.

- [ ] **Step 1: Write the failing tests for the pure parts**

`tests/scripts/test_label_statement_pages.py`:

```python
"""Parsing and writing statement page labels."""

import pytest

from label_statement_pages import apply_to_manifest, disagreements, manifest_block, parse_ranges

MANIFEST = """documents:
  - id: juhayna-2025-ar-standalone
    file: documents/juhayna-2025-ar-standalone.pdf
    statement_pages: null
    traits: [scanned]

  - id: juhayna-2025-ar-consolidated
    statement_pages: null
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [("5", [(5, 5)]), ("5-6", [(5, 6)]), ("5-6, 9", [(5, 6), (9, 9)]), ("", []), (" 7 - 8 ", [(7, 8)])],
)
def test_parse_ranges(text: str, expected: list[tuple[int, int]]) -> None:
    assert parse_ranges(text) == expected


@pytest.mark.parametrize("text", ["6-5", "x", "0"])
def test_bad_ranges_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_ranges(text)


def test_manifest_block_writes_single_and_multiple_ranges() -> None:
    block = manifest_block({"financial_position": [(5, 5)], "profit_or_loss": [(6, 6), (20, 20)]})
    assert block == [
        "    statement_pages:  # labelled blind from the OCR sheet, then reconciled",
        "      financial_position: [5, 5]",
        "      profit_or_loss: [[6, 6], [20, 20]]",
    ]


def test_apply_replaces_only_that_document() -> None:
    updated = apply_to_manifest(MANIFEST, "juhayna-2025-ar-standalone", {"financial_position": [(4, 4)]})
    assert "      financial_position: [4, 4]\n    traits: [scanned]" in updated
    assert updated.count("statement_pages: null") == 1


def test_apply_refuses_a_document_that_is_already_labelled() -> None:
    once = apply_to_manifest(MANIFEST, "juhayna-2025-ar-standalone", {"financial_position": [(4, 4)]})
    with pytest.raises(ValueError, match="already"):
        apply_to_manifest(once, "juhayna-2025-ar-standalone", {"financial_position": [(4, 4)]})


def test_disagreements() -> None:
    assert disagreements({4, 5}, {5, 6}) == ([6], [4])  # (suggested only, marked only)
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/scripts/test_label_statement_pages.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'label_statement_pages'`.

- [ ] **Step 3: Implement the script**

`scripts/label_statement_pages.py`:

```python
"""Label the statement pages of the scanned golden documents, blind first.

    uv run python scripts/label_statement_pages.py serve       # OCR, then a sheet per document
    uv run python scripts/label_statement_pages.py reconcile   # compare labels with the cues
    uv run python scripts/label_statement_pages.py apply       # write the labels into manifest.yaml

The sheet shows every page with no suggestions. Only after the owner saves the labels does
``reconcile`` list where the title and structure cues disagree, so the answer key is not
anchored to the thing it scores (spec 09, R23).
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import yaml

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.locate import load_title_book, score_page
from fra_ingest.ocr import VisionOcr
from fra_ingest.pages import read_pages, sha256_file

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
LABELS_DIR = REPO_ROOT / "var" / "labels"
KEYS = {
    "financial_position": StatementType.BALANCE,
    "profit_or_loss": StatementType.INCOME,
    "comprehensive_income": StatementType.COMPREHENSIVE_INCOME,
    "changes_in_equity": StatementType.EQUITY,
    "cash_flows": StatementType.CASH_FLOW,
}
THUMB_DPI = 40
_RANGE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$")


def parse_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        match = _RANGE.match(part)
        if not match:
            msg = f"not a page range: {part!r}"
            raise ValueError(msg)
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first:
            msg = f"not a page range: {part!r}"
            raise ValueError(msg)
        ranges.append((first, last))
    return ranges


def manifest_block(labels: dict[str, list[tuple[int, int]]]) -> list[str]:
    lines = ["    statement_pages:  # labelled blind from the OCR sheet, then reconciled"]
    for key in KEYS:
        spans = labels.get(key) or []
        if len(spans) == 1:
            lines.append(f"      {key}: [{spans[0][0]}, {spans[0][1]}]")
        elif spans:
            joined = ", ".join(f"[{a}, {b}]" for a, b in spans)
            lines.append(f"      {key}: [{joined}]")
    return lines


def apply_to_manifest(text: str, doc_id: str, labels: dict[str, list[tuple[int, int]]]) -> str:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"- id: {doc_id}"), None)
    if start is None:
        msg = f"{doc_id} is not in the manifest"
        raise ValueError(msg)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("- id:")),
               len(lines))
    for i in range(start, end):
        if lines[i].strip() == "statement_pages: null":
            return "\n".join(lines[:i] + manifest_block(labels) + lines[i + 1:]) + "\n"
    msg = f"{doc_id} already has statement pages"
    raise ValueError(msg)


def disagreements(marked: set[int], suggested: set[int]) -> tuple[list[int], list[int]]:
    """(pages suggested but not marked, pages marked but not suggested)."""
    return sorted(suggested - marked), sorted(marked - suggested)


def _unlabelled() -> list[dict[str, Any]]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return [doc for doc in manifest["documents"] if doc.get("statement_pages") is None]


def _pdf_path(doc: dict[str, Any]) -> Path:
    return MANIFEST.parent / doc["file"]


def _pages(doc: dict[str, Any]) -> list[Any]:
    config = load_config()
    path = _pdf_path(doc)
    return read_pages(path, config, VisionOcr(), cache_dir=config.artifact_root / sha256_file(path))


def _thumbnails(path: Path) -> list[str]:
    pdf = pdfium.PdfDocument(path)
    try:
        encoded = []
        for index in range(len(pdf)):
            image = pdf[index].render(scale=THUMB_DPI / 72).to_pil().convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=70)
            encoded.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
        return encoded
    finally:
        pdf.close()


def _sheet(doc: dict[str, Any], thumbs: list[str]) -> str:
    saved = LABELS_DIR / f"{doc['id']}.json"
    current = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    inputs = "".join(
        f'<label>{key}<input name="{key}" placeholder="e.g. 5 or 5-6" value="'
        + html.escape(", ".join(f"{a}-{b}" if a != b else str(a) for a, b in current.get(key, [])))
        + '"></label>'
        for key in KEYS
    )
    tiles = "".join(
        f'<figure><a href="/page/{doc["id"]}/{n}" target="_blank">'
        f'<img src="data:image/jpeg;base64,{data}"></a><figcaption>{n}</figcaption></figure>'
        for n, data in enumerate(thumbs, start=1)
    )
    return f"""<!doctype html><meta charset="utf-8"><title>{doc['id']}</title>
<style>
body{{font:14px system-ui;margin:0}} form{{position:sticky;top:0;background:#fff;padding:12px;
border-bottom:1px solid #ccc;display:flex;gap:12px;flex-wrap:wrap;align-items:end}}
label{{display:flex;flex-direction:column;font-size:12px}} input{{width:120px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;padding:12px}}
figure{{margin:0;text-align:center}} img{{width:100%;border:1px solid #ddd}}
</style>
<form id="f"><strong>{doc['id']}</strong>{inputs}<button>Save</button><span id="s"></span></form>
<main>{tiles}</main>
<script>
document.getElementById('f').onsubmit = async (e) => {{
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  const r = await fetch(location.pathname, {{method: 'POST', body: JSON.stringify(body)}});
  document.getElementById('s').textContent = r.ok ? 'saved' : await r.text();
}};
</script>"""


def cmd_serve(args: argparse.Namespace) -> int:
    docs = {doc["id"]: doc for doc in _unlabelled()}
    sheets: dict[str, str] = {}
    for doc_id, doc in docs.items():
        print(f"reading {doc_id} ...", flush=True)
        _pages(doc)  # fills the page cache the locator will reuse
        sheets[doc_id] = _sheet(doc, _thumbnails(_pdf_path(doc)))
    LABELS_DIR.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parts = self.path.strip("/").split("/")
            if parts == [""]:
                links = "".join(
                    f'<li><a href="/doc/{d}">{d}</a>'
                    + (" (saved)" if (LABELS_DIR / f"{d}.json").exists() else "")
                    for d in docs
                )
                self._send(f"<!doctype html><meta charset='utf-8'><ul>{links}</ul>")
            elif len(parts) == 2 and parts[0] == "doc" and parts[1] in sheets:
                self._send(_sheet(docs[parts[1]], _thumbnails(_pdf_path(docs[parts[1]]))))
            elif len(parts) == 3 and parts[0] == "page" and parts[1] in docs:
                pdf = pdfium.PdfDocument(_pdf_path(docs[parts[1]]))
                image = pdf[int(parts[2]) - 1].render(scale=100 / 72).to_pil().convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85)
                pdf.close()
                self._send(buffer.getvalue(), "image/jpeg")
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[1] not in docs:
                self.send_error(404)
                return
            raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            try:
                labels = {key: parse_ranges(raw.get(key, "")) for key in KEYS}
            except ValueError as exc:
                self._send(str(exc), "text/plain", status=400)
                return
            out = LABELS_DIR / f"{parts[1]}.json"
            out.write_text(json.dumps({k: v for k, v in labels.items() if v}), encoding="utf-8")
            self._send("saved", "text/plain")

        def _send(self, body: str | bytes, kind: str = "text/html", status: int = 200) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"labelling sheets at {url} (Ctrl-C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_reconcile(_: argparse.Namespace) -> int:
    book = load_title_book()
    for doc in _unlabelled():
        saved = LABELS_DIR / f"{doc['id']}.json"
        if not saved.exists():
            print(f"{doc['id']}: not labelled yet")
            continue
        labels = json.loads(saved.read_text(encoding="utf-8"))
        scores = [score_page(page, book) for page in _pages(doc)]
        report = [f"# {doc['id']}"]
        for key, statement_type in KEYS.items():
            marked = {p for a, b in labels.get(key, []) for p in range(a, b + 1)}
            suggested = {s.page_no for s in scores if s.is_candidate(statement_type)}
            only_suggested, only_marked = disagreements(marked, suggested)
            if only_suggested or only_marked:
                report.append(f"{key}: suggested, not marked {only_suggested}; "
                              f"marked, not suggested {only_marked}")
        text = "\n".join(report if len(report) > 1 else [*report, "no disagreements"])
        (LABELS_DIR / f"{doc['id']}.reconcile.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
    return 0


def cmd_apply(_: argparse.Namespace) -> int:
    text = MANIFEST.read_text(encoding="utf-8")
    for doc in _unlabelled():
        saved = LABELS_DIR / f"{doc['id']}.json"
        if saved.exists():
            labels = {k: [tuple(span) for span in v]
                      for k, v in json.loads(saved.read_text(encoding="utf-8")).items()}
            text = apply_to_manifest(text, doc["id"], labels)
            print(f"applied {doc['id']}")
    MANIFEST.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(run=cmd_serve)
    commands.add_parser("reconcile").set_defaults(run=cmd_reconcile)
    commands.add_parser("apply").set_defaults(run=cmd_apply)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

The GET handler for `/doc/<id>` re-renders thumbnails so a reload shows saved values; if that
is slow, cache `_thumbnails` per document in a dict inside `cmd_serve`.

- [ ] **Step 4: Add Make targets**

In `Makefile`, add `label-pages eval-locate` to `.PHONY` and:

```make
label-pages: ## Blind labelling sheets for the scanned golden documents (Mac, Vision OCR)
	$(UV) run python scripts/label_statement_pages.py serve

eval-locate: ## Score the locator (TARGET=golden, or TARGET=train / dev; model_test needs CHECKPOINT=1)
	$(UV) run python eval/harness/locate.py $(or $(TARGET),golden) $(if $(CHECKPOINT),--checkpoint)
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `uv run pytest tests/scripts/test_label_statement_pages.py -q`
Expected: 12 passed.

- [ ] **Step 6: Check and commit**

Run: `make lint test`

```bash
git add scripts/label_statement_pages.py tests/scripts/test_label_statement_pages.py Makefile
git commit -m "Add a blind labelling sheet for the scanned golden documents"
```

---

### Task 11: Locator eval harness

**Files:**
- Create: `eval/harness/__init__.py`, `eval/harness/locate.py`,
  `tests/eval/test_locate_harness.py`

**Interfaces:**
- Consumes: `locate_pdf`, `load_config`, `default_engine`, `LocateResult`, `StatementRange`.
- Produces: `MANIFEST_KEYS`, `labelled_ranges(value: Any) -> list[tuple[int, int]]`,
  `found_pages(result: LocateResult, statement_type, pad: int) -> set[int]`,
  `recall(labelled: set[int], found: set[int]) -> float`,
  `check_target(target: str, checkpoint: bool) -> None` (raises `SystemExit(2)`),
  `truth_label(entry: dict) -> str`, `verdict_label(result: LocateResult) -> str`,
  `main(argv) -> int`. Writes `var/eval/locate-<target>.json`.

- [ ] **Step 1: Write the failing tests**

`tests/eval/test_locate_harness.py`:

```python
"""Scoring rules of the locator eval."""

import pytest

from fra_core.schemas import Document, StatementType
from fra_ingest.results import IndustrySignal, LocateResult, StatementRange
from harness.locate import (
    check_target,
    found_pages,
    labelled_ranges,
    recall,
    truth_label,
    verdict_label,
)

B = StatementType.BALANCE


def result(*ranges: StatementRange, kind: str = "corporate", subkind: str | None = None) -> LocateResult:
    return LocateResult(
        version="1",
        document=Document(sha256="0" * 64, filename="x.pdf", page_count=20),
        pages=[],
        ranges=list(ranges),
        convert_ranges=[],
        industry=IndustrySignal(kind=kind, subkind=subkind),
    )


def test_labelled_ranges_accept_one_or_several() -> None:
    assert labelled_ranges([5, 6]) == [(5, 6)]
    assert labelled_ranges([[5, 6], [9, 9]]) == [(5, 6), (9, 9)]
    assert labelled_ranges(None) == []


def test_a_labelled_page_is_found_inside_a_padded_range() -> None:
    located = result(StatementRange(type=B, first_page=6, last_page=6, score=8, rank=1))
    assert found_pages(located, B, pad=1) == {5, 6, 7}
    assert recall({5, 6}, found_pages(located, B, pad=1)) == 1.0
    assert recall({5, 9}, found_pages(located, B, pad=1)) == 0.5


def test_blind_is_refused() -> None:
    with pytest.raises(SystemExit) as caught:
        check_target("blind", checkpoint=False)
    assert caught.value.code == 2


def test_model_test_needs_a_checkpoint() -> None:
    with pytest.raises(SystemExit):
        check_target("model_test", checkpoint=False)
    check_target("model_test", checkpoint=True)
    check_target("train", checkpoint=False)
    check_target("golden", checkpoint=False)


def test_industry_labels_line_up() -> None:
    assert truth_label({"role": "corporate"}) == "corporate"
    assert truth_label({"role": "negative_control", "sector": "bank"}) == "bank"
    assert truth_label({"role": "negative_control", "sector": "other_financial",
                        "subsector": "brokerage"}) == "other_financial/brokerage"
    assert verdict_label(result(kind="other_financial", subkind="brokerage")) == "other_financial/brokerage"
    assert verdict_label(result(kind="insurer")) == "insurer"
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/eval/test_locate_harness.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness'`.

- [ ] **Step 3: Implement the harness**

Create an empty `eval/harness/__init__.py`, then `eval/harness/locate.py`:

```python
"""Score the statement page locator (spec 09, Scoring).

    uv run python eval/harness/locate.py golden
    uv run python eval/harness/locate.py train
    uv run python eval/harness/locate.py model_test --checkpoint

Golden: recall per type against labelled pages, candidate share and time. Corpus pools: whether
each corporate document has a balance and an income range, candidate share, and industry
verdicts against the sector labels. The blind pool is refused.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from fra_core.schemas import StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.ocr import default_engine
from fra_ingest.results import LocateResult
from fra_ingest.stage import locate_pdf

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
CANDIDATES = REPO_ROOT / "eval" / "corpus" / "candidates.yaml"
CORPUS = REPO_ROOT / "var" / "corpus"
OUT = REPO_ROOT / "var" / "eval"
MANIFEST_KEYS = {
    "financial_position": StatementType.BALANCE,
    "profit_or_loss": StatementType.INCOME,
    "comprehensive_income": StatementType.COMPREHENSIVE_INCOME,
    "changes_in_equity": StatementType.EQUITY,
    "cash_flows": StatementType.CASH_FLOW,
}
POOLS = ("dev", "train", "model_test")


def labelled_ranges(value: Any) -> list[tuple[int, int]]:
    if not value:
        return []
    if isinstance(value[0], list):
        return [(int(a), int(b)) for a, b in value]
    return [(int(value[0]), int(value[1]))]


def found_pages(result: LocateResult, statement_type: StatementType, pad: int) -> set[int]:
    count = result.document.page_count
    return {
        page
        for r in result.ranges
        if r.type is statement_type
        for page in range(r.padded(pad, count)[0], r.padded(pad, count)[1] + 1)
    }


def recall(labelled: set[int], found: set[int]) -> float:
    return len(labelled & found) / len(labelled) if labelled else 1.0


def check_target(target: str, checkpoint: bool) -> None:
    if target == "blind":
        print("the blind pool is never scored during development (R17)", file=sys.stderr)
        raise SystemExit(2)
    if target == "model_test" and not checkpoint:
        print("model_test runs only at checkpoints: pass --checkpoint", file=sys.stderr)
        raise SystemExit(2)
    if target != "golden" and target not in POOLS:
        print(f"unknown target {target!r}", file=sys.stderr)
        raise SystemExit(2)


def truth_label(entry: dict[str, Any]) -> str:
    if entry.get("role") != "negative_control":
        return "corporate"
    if entry.get("sector") == "other_financial":
        return f"other_financial/{entry.get('subsector', 'other')}"
    return str(entry.get("sector"))


def verdict_label(result: LocateResult) -> str:
    kind = result.industry.kind
    return f"{kind}/{result.industry.subkind or 'other'}" if kind == "other_financial" else kind


def run_golden(no_ocr: bool) -> dict[str, Any]:
    config = load_config()
    engine = None if no_ocr else default_engine()
    enabled = set(config.enabled_types)
    rows, shares, misses = [], [], []
    for doc in yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]:
        pages = doc.get("statement_pages")
        if not pages:
            print(f"skip {doc['id']}: not labelled")
            continue
        result = locate_pdf(MANIFEST.parent / doc["file"], config, engine)
        row: dict[str, Any] = {"id": doc["id"], "share": result.candidate_share,
                               "seconds": sum(result.timings.values()), "recall": {}}
        for key, statement_type in MANIFEST_KEYS.items():
            labelled = {p for a, b in labelled_ranges(pages.get(key)) for p in range(a, b + 1)}
            if not labelled:
                continue
            found = found_pages(result, statement_type, config.pad_pages)
            row["recall"][statement_type.value] = recall(labelled, found)
            if statement_type in enabled and labelled - found:
                misses.append(f"{doc['id']} {statement_type.value} pages {sorted(labelled - found)}")
        rows.append(row)
        shares.append(result.candidate_share)
        print(f"{doc['id']:34} share {result.candidate_share:5.1%}  {row['seconds']:6.1f}s  "
              + "  ".join(f"{k} {v:.0%}" for k, v in row["recall"].items()))

    median_share = statistics.median(shares) if shares else 0.0
    print(f"\nmedian candidate share {median_share:.1%} (target 15% or less)")
    print("enabled-type misses: " + ("none" if not misses else "\n  " + "\n  ".join(misses)))
    return {"documents": rows, "median_share": median_share, "misses": misses}


def run_pool(pool: str, no_ocr: bool) -> dict[str, Any]:
    config = load_config()
    engine = None if no_ocr else default_engine()
    entries = yaml.safe_load(CANDIDATES.read_text(encoding="utf-8"))["documents"]
    rows, confusion, uncovered = [], Counter[tuple[str, str]](), []
    for entry in (e for e in entries if e.get("pool") == pool):
        path = CORPUS / pool / f"{entry['id']}.pdf"
        if not path.exists():
            continue
        try:
            result = locate_pdf(path, config, engine)
        except IngestError as exc:
            rows.append({"id": entry["id"], "error": exc.reason})
            continue
        types = {r.type for r in result.ranges}
        covered = {StatementType.BALANCE, StatementType.INCOME} <= types
        truth, verdict = truth_label(entry), verdict_label(result)
        confusion[(truth, verdict)] += 1
        if truth == "corporate" and not covered:
            uncovered.append(entry["id"])
        rows.append({"id": entry["id"], "covered": covered, "share": result.candidate_share,
                     "truth": truth, "verdict": verdict, "flags": result.flags})

    corporates = [r for r in rows if r.get("truth") == "corporate"]
    coverage = sum(r["covered"] for r in corporates) / len(corporates) if corporates else 0.0
    print(f"{pool}: {len(rows)} documents, corporate coverage {coverage:.1%} (target 95%)")
    print("not covered: " + (", ".join(uncovered) or "none"))
    print("industry (truth -> verdict):")
    for (truth, verdict), n in sorted(confusion.items()):
        print(f"  {truth:34} -> {verdict:34} {n}")
    return {"documents": rows, "coverage": coverage, "uncovered": uncovered,
            "confusion": [[t, v, n] for (t, v), n in sorted(confusion.items())]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="golden, dev, train or model_test")
    parser.add_argument("--checkpoint", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args(argv)
    check_target(args.target, args.checkpoint)

    report = run_golden(args.no_ocr) if args.target == "golden" else run_pool(args.target, args.no_ocr)
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"locate-{args.target}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`Counter[tuple[str, str]]()` needs Python 3.9+; fine on 3.12.

- [ ] **Step 4: Run the tests to see them pass**

Run: `uv run pytest tests/eval/test_locate_harness.py -q`
Expected: 5 passed.

- [ ] **Step 5: Check and commit**

Run: `make lint test`

```bash
git add eval/harness tests/eval
git commit -m "Add the locator eval over the golden set and corpus pools"
```

---

### Task 12: Label, measure, tune, record

This task is measurement with the owner, not new code. Weight and phrase changes go through
the tests: any tuning that breaks a trap test is wrong, not the test.

**Files:**
- Modify: `eval/golden/manifest.yaml` (labels), `packages/ingest/src/fra_ingest/locate.py`
  (weights, only if measured), `packages/ingest/src/fra_ingest/data/*.yaml`,
  `docs/blueprint/09-ingest-locate.md` (Results section), `eval/golden/README.md`
  (correct the OCR timing and finding 4)

- [ ] **Step 1: Owner labels the 9 scanned documents blind**

Run: `make label-pages`
The first run OCRs about 500 pages (roughly 2 to 3 minutes plus model load), then opens the
sheets. The owner fills each document's ranges and saves. Nothing is suggested on the sheet.

- [ ] **Step 2: Reconcile, then apply**

Run: `uv run python scripts/label_statement_pages.py reconcile`
The owner settles each disagreement (re-saving in the sheet if a label changes), then:

Run: `uv run python scripts/label_statement_pages.py apply && make corpus-check`
Commit the labels on their own:

```bash
git add eval/golden/manifest.yaml
git commit -m "Label statement pages of the scanned golden filings"
```

Keep `var/labels/*.reconcile.txt`: the disagreement lists go into the Results section.

- [ ] **Step 3: Score the golden set**

Run: `make eval-locate`
Targets: recall 100% on enabled types; median candidate share 15% or less; digital annual
report under 10 s; 64-page scan under 30 s with the OCR model loaded.

- [ ] **Step 4: Score `train` and `dev`**

Run: `make eval-locate TARGET=train` and `make eval-locate TARGET=dev`
Targets: corporate coverage 95% or more with every miss explained; all 6 bank and insurer
documents given that verdict; at most 2 corporate documents marked bank or insurer.

- [ ] **Step 5: Tune on failures, one class at a time**

For each miss: open `var/artifacts/<sha256>/locate.json`, read the `pages` entry of the missed
page (title hits, cue types, numeric tokens, structure, negatives), decide the failure class,
and fix it with a new test in `test_score_page.py` or `test_industry.py` first. Prefer phrase
additions in the YAML over weight changes. Rerun Steps 3 and 4 after each fix; scoring reruns
in seconds because page text is cached. Never open or tune against `blind` documents.

- [ ] **Step 6: Record results and correct the golden README**

Add a `## Results` section to `docs/blueprint/09-ingest-locate.md` with the measured table
from Steps 3 and 4 (date, per-target value, pass or fail), the final weights if they changed,
the reconcile disagreement counts, and any target missed with its reason.

In `eval/golden/README.md`: finding 4 says the Almarai Arabic edition is proper Unicode; add
that its text layer stores Arabic in visual order (every word reversed), found 2026-09-27, the
only one of 61 Arabic corpus documents with a text layer to do so. Finding 9: note that the
24.7 s per page was mostly model load and warm accurate mode reads a page in about 0.25 s at
72 dpi. Update `meta.ocr_reference` in `manifest.yaml` to match.

- [ ] **Step 7: Full check and commit**

Run: `make lint typecheck test corpus-check docs-check`

```bash
git add docs/blueprint/09-ingest-locate.md eval/golden packages/ingest
git commit -m "Record locator results on the golden set and training pool"
```

Report gate status to the owner: which Done-when targets pass, which do not, and why.
