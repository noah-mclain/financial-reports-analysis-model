# Ingest Part 2: Convert Implementation Plan

**Goal:** Convert the pages locate chose (`convert_ranges` in `locate.json`) with docling, one child process per document, writing docling JSON per range, page images and a `convert.json` index, with the child's peak memory recorded.

**Architecture:** `fra-ingest convert <pdf>` is the child: it loads or runs locate, plans an OCR mode and one OCR language per range, runs docling per range through a small adapter (`DoclingRunner`), and writes artifacts atomically. `convert_in_child` runs that command in a subprocess and maps how it ended to a `ConvertResult` or an `IngestError`. Only `converter.py` imports docling, lazily, so fast tests never load PyTorch.

**Tech Stack:** Python 3.12, docling 2.126.0 (docling-core 2.96.0, torch 2.14.0), pydantic 2, pypdfium2, Pillow, pytest, uv.

**Spec:** `docs/blueprint/10-ingest-convert.md` (read it first). Background: `docs/blueprint/04-execution-phases.md` 1.2, `docs/blueprint/01-constraints.md` 1.5, `docs/blueprint/09-ingest-locate.md`.

## Global Constraints

- docling pinned exactly: `docling==2.126.0`; the lock must resolve `docling-core 2.96.0` and `torch 2.14.0`. Lock with `MACOSX_DEPLOYMENT_TARGET=15.0`.
- Only `packages/ingest/src/fra_ingest/converter.py` imports `docling` or `docling_core`, and only inside functions.
- Tests are written before the code they cover. `make test` passes before every commit; `make lint` and `make typecheck` pass before the final commit of each task.
- Stage artifacts live under `<artifact_root>/<sha256>/`: `convert.json`, `docling/p<a>-<b>.json`, `pages/<n>.png`. `var/` is gitignored; never commit artifacts or PDFs.
- `CONVERT_VERSION = "1"`. `memory_budget_gb` default 3.0. `images_scale` default 2.0. batch size 2.
- One OCR language per range, never the pair (R21, R27).
- Mac only this week; `ocr_engine = "none"` skips image ranges with `ocr_unavailable:<a>-<b>`.
- Never run anything against the `blind` pool; do not run `model_test` (owner decision).
- Commits: author and committer `noah-mclain <nadam.30032415@gmail.com>`; messages carry no trailers.

## Review Focus

- A crash mid-conversion leaves `docling/` or `pages/` files but no `convert.json`, or a `convert.json` whose files were deleted: the next run must convert again, not trust the leftovers (Task 7, cache tests).
- A re-run with different ranges must not leave the previous run's `docling/p*.json` or PNGs behind to be mistaken for this run's (Task 7, stale-file test).
- PDF paths with spaces and Arabic characters must survive the trip to the child and back, including the error reason parsed from its stderr (Task 9).
- An old-version or corrupt `locate.json` must be recomputed, never trusted or crashed on (Task 8).
- docling returning a page without an image, or fewer pages than the range, must be flagged per page, not silently dropped (Task 7).

---

### Task 1: Pin docling and check its contract

**Files:**
- Modify: `packages/ingest/pyproject.toml` (dependencies)
- Modify: `uv.lock` (regenerated)
- Create: `packages/ingest/tests/test_docling_contract.py`
- Possibly modify: `docs/blueprint/04-execution-phases.md` (1.2), `docs/blueprint/01-constraints.md` (1.5), only if a name differs

**Interfaces:**
- Produces: docling installed at 2.126.0. Confirms the names every later task uses: `PdfPipelineOptions` fields `do_ocr, ocr_options, do_table_structure, table_structure_options, generate_page_images, images_scale, ocr_batch_size, layout_batch_size, table_batch_size, document_timeout, accelerator_options`; `OcrMacOptions(lang, mode)`; `OcrMode.FULL_PAGE`, `OcrMode.PDF_AWARE_LAYOUT_REGIONS`; `TableStructureOptions(mode, do_cell_matching)`; `TableFormerMode.ACCURATE`; `AcceleratorDevice.MPS|CPU|AUTO`; `ConversionStatus.SUCCESS|PARTIAL_SUCCESS|FAILURE`; `DocumentConverter.convert(source, page_range=..., raises_on_error=...)`; `DocumentConverter.initialize_pipeline`; `ImageRefMode.PLACEHOLDER`; `page_range` keeps document page numbers.

- [ ] **Step 1: Write the contract test**

```python
"""The docling names spec 10 relies on, checked against the pinned version (04, 1.2; R25)."""

from __future__ import annotations

import importlib.metadata
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


def test_docling_is_the_pinned_version() -> None:
    assert importlib.metadata.version("docling") == "2.126.0"
    assert importlib.metadata.version("docling-core") == "2.96.0"


def test_pipeline_option_names_exist() -> None:
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMacOptions,
        OcrMode,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ImageRefMode

    fields = set(PdfPipelineOptions.model_fields)
    expected = {
        "do_ocr",
        "ocr_options",
        "do_table_structure",
        "table_structure_options",
        "generate_page_images",
        "images_scale",
        "ocr_batch_size",
        "layout_batch_size",
        "table_batch_size",
        "document_timeout",
        "accelerator_options",
    }
    assert expected <= fields, expected - fields
    assert {"lang", "mode"} <= set(OcrMacOptions.model_fields)
    assert {"mode", "do_cell_matching"} <= set(TableStructureOptions.model_fields)
    assert {"FULL_PAGE", "PDF_AWARE_LAYOUT_REGIONS"} <= {m.name for m in OcrMode}
    assert TableFormerMode.ACCURATE is not None
    assert {"MPS", "CPU", "AUTO"} <= {d.name for d in AcceleratorDevice}
    assert {"SUCCESS", "PARTIAL_SUCCESS", "FAILURE"} <= {s.name for s in ConversionStatus}
    assert ImageRefMode.PLACEHOLDER is not None
    assert "device" in AcceleratorOptions.model_fields
    assert InputFormat.PDF is not None and PdfFormatOption is not None
    assert callable(DocumentConverter.initialize_pipeline)
    parameters = inspect.signature(DocumentConverter.convert).parameters
    assert {"page_range", "raises_on_error"} <= set(parameters)


@pytest.mark.golden
def test_page_range_keeps_the_document_page_numbers(golden: Callable[[str], Path]) -> None:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    result = converter.convert(
        golden("almarai-2025-en-annualreport.pdf"), page_range=(156, 158)
    )
    assert sorted(result.document.pages) == [156, 157, 158]
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest packages/ingest/tests/test_docling_contract.py -v`
Expected: FAIL, `PackageNotFoundError: docling` / `ModuleNotFoundError: No module named 'docling'`.

- [ ] **Step 3: Add the dependency and lock**

In `packages/ingest/pyproject.toml`, add to `dependencies` (keep alphabetical order):

```toml
dependencies = [
  "docling==2.126.0",
  "fra-core",
  "pillow>=11.0",
  "pydantic>=2.11",
  "pypdfium2>=5.13",
  "pyyaml>=6.0",
]
```

Run:

```bash
MACOSX_DEPLOYMENT_TARGET=15.0 uv lock
make setup
uv run python -c "import importlib.metadata as m; print(m.version('docling'), m.version('docling-core'), m.version('torch'))"
```

Expected last line: `2.126.0 2.96.0 2.14.0`. If the resolver picks different docling-core or torch versions, stop and report the resolution to the owner before going on: 01, 1.5 records the set that was verified together.

- [ ] **Step 4: Run the contract test**

Run: `uv run pytest packages/ingest/tests/test_docling_contract.py -v`
Expected: PASS. The last test downloads docling's layout models on first run (about 1 GB) and takes a few minutes the first time.

If a name assertion fails: find the replacement in the installed package (`uv run python -c "import docling.datamodel.pipeline_options as p, inspect; print(inspect.getsource(p.OcrMacOptions))"`), change the test to the real name, and record the change in 04, 1.2 (the code block) and in 01, 1.5. Later tasks must then use the real name; update Task 6's code accordingly before starting it.

If `test_page_range_keeps_the_document_page_numbers` fails because pages come back numbered from 1 (R25 fires): stop and tell the owner. The design then needs an offset mapping in the adapter, which is a spec change.

- [ ] **Step 5: Run the fast suite, lint, typecheck**

Run: `make test && make lint && make typecheck`
Expected: all pass (the contract test is `slow`, so `make test` skips it).

- [ ] **Step 6: Commit**

```bash
git add packages/ingest/pyproject.toml uv.lock packages/ingest/tests/test_docling_contract.py
git commit -m "Pin docling 2.126.0 and check the names the converter relies on"
```

(Add the two blueprint files too if Step 4 changed them.)

---

### Task 2: `[convert]` settings

**Files:**
- Modify: `packages/ingest/src/fra_ingest/config.py`
- Modify: `configs/ingest.toml`
- Test: `packages/ingest/tests/test_config.py`

**Interfaces:**
- Produces on `IngestConfig`: `device: Literal["mps", "cpu", "auto"] = "mps"`, `convert_ocr: Literal["ocrmac", "none"] = "ocrmac"`, `images_scale: float = 2.0`, `batch_size: int = 2`, `do_cell_matching: bool = True`, `document_timeout_s: float = 600.0`, `child_timeout_s: float = 900.0`, `memory_budget_gb: float = 3.0`. TOML keys live in `[convert]`; `ocr_engine` maps to `convert_ocr`, every other key to the field of the same name.

- [ ] **Step 1: Write the failing tests** (append to `test_config.py`; add `from pydantic import ValidationError` to its imports)

```python
def test_convert_defaults() -> None:
    config = IngestConfig()
    assert config.device == "mps"
    assert config.convert_ocr == "ocrmac"
    assert config.images_scale == 2.0
    assert config.batch_size == 2
    assert config.do_cell_matching is True
    assert config.document_timeout_s == 600.0
    assert config.child_timeout_s == 900.0
    assert config.memory_budget_gb == 3.0


def test_convert_settings_load(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            '[convert]\ndevice = "cpu"\nocr_engine = "none"\nimages_scale = 1.5\n'
            "child_timeout_s = 60\n",
        )
    )
    assert config.device == "cpu"
    assert config.convert_ocr == "none"
    assert config.images_scale == 1.5
    assert config.child_timeout_s == 60.0


def test_an_unknown_convert_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"unknown setting convert\.dpi"):
        load_config(write(tmp_path, "[convert]\ndpi = 2\n"))


def test_an_unknown_device_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(write(tmp_path, '[convert]\ndevice = "cuda"\n'))
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_config.py -v`
Expected: FAIL (`AttributeError: 'IngestConfig' object has no attribute 'device'`, `unknown setting convert.device`).

- [ ] **Step 3: Implement**

In `config.py`, add `from typing import Any, Literal` (replace the existing `from typing import Any`), extend `_TOML_FIELDS`:

```python
    ("convert", "device"): "device",
    ("convert", "ocr_engine"): "convert_ocr",
    ("convert", "images_scale"): "images_scale",
    ("convert", "batch_size"): "batch_size",
    ("convert", "do_cell_matching"): "do_cell_matching",
    ("convert", "document_timeout_s"): "document_timeout_s",
    ("convert", "child_timeout_s"): "child_timeout_s",
    ("convert", "memory_budget_gb"): "memory_budget_gb",
```

and add to `IngestConfig`, after `low_selectivity_share`:

```python
    device: Literal["mps", "cpu", "auto"] = "mps"
    convert_ocr: Literal["ocrmac", "none"] = "ocrmac"
    images_scale: float = Field(default=2.0, gt=0.0, le=4.0)
    batch_size: int = Field(default=2, ge=1)
    do_cell_matching: bool = True
    document_timeout_s: float = Field(default=600.0, gt=0.0)
    child_timeout_s: float = Field(default=900.0, gt=0.0)
    memory_budget_gb: float = Field(default=3.0, gt=0.0)
```

In `configs/ingest.toml`, add before `[artifacts]`:

```toml
[convert]
# mps on the Mac; cpu in the Docker profile.
device = "mps"
# ocrmac on the Mac. none until the week 2 bake-off picks a Linux engine: text ranges
# convert, image ranges are skipped and flagged ocr_unavailable.
ocr_engine = "ocrmac"
# Page images at 2x (144 dpi), for the review report and click-to-source.
images_scale = 2.0
# docling's OCR, layout and table batch sizes. 2 on this machine (01, 1.5).
batch_size = 2
# Task 1.9 compares TableFormer with cell matching on and off.
do_cell_matching = true
# docling's own limit per range, and the parent's limit on the whole child.
document_timeout_s = 600
child_timeout_s = 900
# The ingest child's peak (01, 1.3). Above it the result is flagged, not failed.
memory_budget_gb = 3.0
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_config.py -v`
Expected: PASS, including `test_repository_config_matches_the_defaults`.

- [ ] **Step 5: Commit**

```bash
make test
git add packages/ingest/src/fra_ingest/config.py configs/ingest.toml packages/ingest/tests/test_config.py
git commit -m "Add the convert settings: device, OCR engine, page image scale, timeouts, memory budget"
```

---

### Task 3: Result models and error reasons

**Files:**
- Modify: `packages/ingest/src/fra_ingest/results.py`
- Modify: `packages/ingest/src/fra_ingest/errors.py`
- Test: `packages/ingest/tests/test_convert_results.py`

**Interfaces:**
- Produces in `fra_ingest.results`: `RangeOcr = Literal["pdf_aware", "full_page", "skipped"]`, `RangeStatus = Literal["ok", "partial", "failed", "skipped"]`, `RangePlan(first_page, last_page, ocr, ocr_language, image_pages: tuple[int, ...] = ())` with property `label -> "a-b"`, `RangeConversion(first_page, last_page, ocr, ocr_language, docling_path=None, tables=0, seconds=0.0, status, flags=[])`, `ConvertResult(version, sha256, locate_version, docling_version, device, settings_hash, ranges, page_images: dict[int, str] = {}, peak_footprint_gb: float | None = None, flags=[], timings={})` with property `all_failed -> bool`.
- Produces in `fra_ingest.errors`: reasons `"convert_failed"`, `"convert_timeout"`, `"convert_crashed"` in `IngestErrorReason`.

- [ ] **Step 1: Write the failing tests**

```python
"""What the convert stage writes to convert.json (spec 10, Data model)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fra_ingest.results import ConvertResult, RangeConversion, RangePlan, RangeStatus

SHA = "a" * 64


def conversion(status: RangeStatus, first: int = 2, last: int = 3) -> RangeConversion:
    return RangeConversion(
        first_page=first,
        last_page=last,
        ocr="pdf_aware",
        ocr_language="en-US",
        status=status,
    )


def result(*statuses: RangeStatus) -> ConvertResult:
    return ConvertResult(
        version="1",
        sha256=SHA,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[conversion(s, i * 10 + 1, i * 10 + 2) for i, s in enumerate(statuses)],
    )


def test_a_result_survives_a_json_round_trip() -> None:
    original = result("ok", "partial").model_copy(
        update={"page_images": {12: "pages/12.png"}, "peak_footprint_gb": 2.4}
    )
    again = ConvertResult.model_validate_json(original.model_dump_json())
    assert again == original
    assert again.page_images == {12: "pages/12.png"}


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("failed",), True),
        (("failed", "skipped"), True),
        (("failed", "ok"), False),
        (("failed", "partial"), False),
        (("skipped",), False),
        ((), False),
    ],
)
def test_all_failed_counts_only_attempted_ranges(
    statuses: tuple[RangeStatus, ...], expected: bool
) -> None:
    assert result(*statuses).all_failed is expected


def test_a_plan_has_a_label_and_rejects_a_reversed_range() -> None:
    plan = RangePlan(first_page=12, last_page=15, ocr="full_page", ocr_language="ar-SA")
    assert plan.label == "12-15"
    with pytest.raises(ValidationError):
        RangePlan(first_page=5, last_page=4, ocr="pdf_aware", ocr_language="en-US")
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_convert_results.py -v`
Expected: FAIL with `ImportError: cannot import name 'ConvertResult'`.

- [ ] **Step 3: Implement**

In `results.py`, add `model_validator` to the pydantic import, then append:

```python
RangeOcr = Literal["pdf_aware", "full_page", "skipped"]
RangeStatus = Literal["ok", "partial", "failed", "skipped"]


class RangePlan(BaseModel):
    """How docling reads one range of ``convert_ranges``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    ocr: RangeOcr
    ocr_language: str | None = Field(
        description="One language: Vision reads only the first it is given (R21). None when "
        "the range is skipped or no OCR engine is configured"
    )
    image_pages: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> RangePlan:
        if self.last_page < self.first_page:
            msg = f"range {self.first_page}-{self.last_page} ends before it starts"
            raise ValueError(msg)
        return self

    @property
    def label(self) -> str:
        return f"{self.first_page}-{self.last_page}"


class RangeConversion(BaseModel):
    """What happened to one range."""

    model_config = ConfigDict(extra="forbid")

    first_page: int = Field(ge=1)
    last_page: int = Field(ge=1)
    ocr: RangeOcr
    ocr_language: str | None
    docling_path: str | None = Field(
        default=None, description="Relative to the artifact directory; None when nothing was saved"
    )
    tables: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.0, ge=0.0)
    status: RangeStatus
    flags: list[str] = Field(default_factory=list)


class ConvertResult(BaseModel):
    """Output of the convert stage, written to ``<artifact root>/<sha256>/convert.json``."""

    model_config = ConfigDict(extra="forbid")

    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locate_version: str
    docling_version: str
    device: str
    settings_hash: str
    ranges: list[RangeConversion]
    page_images: dict[int, str] = Field(default_factory=dict)
    peak_footprint_gb: float | None = None
    flags: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

    @property
    def all_failed(self) -> bool:
        attempted = [r for r in self.ranges if r.status != "skipped"]
        return bool(attempted) and all(r.status == "failed" for r in attempted)
```

In `errors.py`:

```python
IngestErrorReason = Literal[
    "unreadable_pdf",
    "encrypted_pdf",
    "empty_pdf",
    "convert_failed",
    "convert_timeout",
    "convert_crashed",
]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_convert_results.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
make test
git add packages/ingest/src/fra_ingest/results.py packages/ingest/src/fra_ingest/errors.py packages/ingest/tests/test_convert_results.py
git commit -m "Add the convert result models and the convert failure reasons"
```

---

### Task 4: OCR mode and language per range

**Files:**
- Create: `packages/ingest/src/fra_ingest/ocr_policy.py`
- Modify: `packages/ingest/tests/support.py` (add `located`)
- Test: `packages/ingest/tests/test_ocr_policy.py`

**Interfaces:**
- Consumes: `RangePlan` (Task 3), `IngestConfig.convert_ocr` (Task 2), `LocateResult` (Part 1).
- Produces: `plan_ranges(located: LocateResult, ocr_languages: Mapping[int, str | None], config: IngestConfig) -> list[RangePlan]`, one plan per entry of `located.convert_ranges`, in order. `ocr_languages` maps page number to the OCR language Part 1 kept for that page (`PageText.ocr_language`). Test helper `located(modes, ranges, *, language="en", sha256="0"*64) -> LocateResult` in `support.py`.

- [ ] **Step 1: Add the test helper to `support.py`**

Add imports `from fra_core.schemas import Document, PageMode, PageProfile, TextSource`, `from fra_ingest.locate import LOCATE_VERSION`, `from fra_ingest.results import IndustrySignal, LocateResult, PageText`, then:

```python
def located(
    modes: Sequence[PageMode],
    ranges: Sequence[tuple[int, int]],
    *,
    language: str = "en",
    sha256: str = "0" * 64,
) -> LocateResult:
    """A locate result over pages of the given modes, with the given convert ranges."""
    document = Document(
        sha256=sha256,
        filename="doc.pdf",
        page_count=len(modes),
        pages=[
            PageProfile(
                page_no=i + 1,
                mode=mode,
                char_count=0 if mode is PageMode.IMAGE else 500,
                width_pt=595,
                height_pt=842,
            )
            for i, mode in enumerate(modes)
        ],
        language=language,
    )
    return LocateResult(
        version=LOCATE_VERSION,
        document=document,
        pages=[],
        ranges=[],
        convert_ranges=list(ranges),
        industry=IndustrySignal(kind="corporate"),
    )
```

- [ ] **Step 2: Write the failing tests**

```python
"""OCR mode and language per range (spec 10, Data flow step 3)."""

from __future__ import annotations

from support import located

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.ocr_policy import plan_ranges

T, I = PageMode.TEXT, PageMode.IMAGE
MAC = IngestConfig()
NO_OCR = IngestConfig(convert_ocr="none")


def test_text_ranges_are_pdf_aware_in_the_document_language() -> None:
    plans = plan_ranges(located([T] * 6, [(2, 3), (5, 6)]), {}, MAC)
    assert [(p.first_page, p.last_page, p.ocr, p.ocr_language) for p in plans] == [
        (2, 3, "pdf_aware", "en-US"),
        (5, 6, "pdf_aware", "en-US"),
    ]
    arabic = plan_ranges(located([T] * 3, [(1, 3)], language="ar"), {}, MAC)
    assert arabic[0].ocr_language == "ar-SA"


def test_one_image_page_makes_the_whole_range_full_page() -> None:
    (plan,) = plan_ranges(located([T, T, I, T], [(2, 4)]), {3: "en-US"}, MAC)
    assert plan.ocr == "full_page"
    assert plan.image_pages == (3,)


def test_the_language_is_the_one_most_pages_were_read_in() -> None:
    (plan,) = plan_ranges(
        located([I] * 4, [(1, 4)], language="ar"),
        {1: "ar-SA", 2: "en-US", 3: "en-US", 4: None},
        MAC,
    )
    assert plan.ocr_language == "en-US"


def test_a_tie_goes_to_the_earliest_page() -> None:
    (plan,) = plan_ranges(located([I, I], [(1, 2)]), {1: "ar-SA", 2: "en-US"}, MAC)
    assert plan.ocr_language == "ar-SA"


def test_image_pages_with_no_reads_fall_back_to_the_document_language() -> None:
    (plan,) = plan_ranges(located([I, I], [(1, 2)], language="ar"), {1: None}, MAC)
    assert plan.ocr_language == "ar-SA"


def test_without_an_ocr_engine_image_ranges_are_skipped() -> None:
    plans = plan_ranges(located([T, I, T, T], [(1, 2), (3, 4)]), {2: "en-US"}, NO_OCR)
    assert [(p.ocr, p.ocr_language) for p in plans] == [("skipped", None), ("pdf_aware", None)]


def test_no_convert_ranges_means_no_plans() -> None:
    assert plan_ranges(located([T] * 3, []), {}, MAC) == []
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_ocr_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.ocr_policy'`.

- [ ] **Step 4: Implement `ocr_policy.py`**

```python
"""Which OCR mode and which one language docling uses for each range (spec 10, Data flow).

Our own types, not docling's, so this is tested without loading docling.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.results import LocateResult, RangeOcr, RangePlan

_DOCUMENT_LANGUAGE = {"ar": "ar-SA"}
_DEFAULT_LANGUAGE = "en-US"


def plan_ranges(
    located: LocateResult,
    ocr_languages: Mapping[int, str | None],
    config: IngestConfig,
) -> list[RangePlan]:
    modes = {page.page_no: page.mode for page in located.document.pages}
    fallback = _DOCUMENT_LANGUAGE.get(located.document.language, _DEFAULT_LANGUAGE)
    plans = []
    for first, last in located.convert_ranges:
        image_pages = tuple(n for n in range(first, last + 1) if modes.get(n) is PageMode.IMAGE)
        ocr: RangeOcr
        language: str | None
        if config.convert_ocr == "none":
            ocr = "skipped" if image_pages else "pdf_aware"
            language = None
        else:
            ocr = "full_page" if image_pages else "pdf_aware"
            # Counter keeps first-seen order among equal counts, so a tie goes to the earliest page.
            reads = Counter(lang for n in image_pages if (lang := ocr_languages.get(n)))
            language = reads.most_common(1)[0][0] if reads else fallback
        plans.append(
            RangePlan(
                first_page=first,
                last_page=last,
                ocr=ocr,
                ocr_language=language,
                image_pages=image_pages,
            )
        )
    return plans
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_ocr_policy.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
make test
git add packages/ingest/src/fra_ingest/ocr_policy.py packages/ingest/tests/support.py packages/ingest/tests/test_ocr_policy.py
git commit -m "Plan each convert range: OCR mode from the page modes, one language from the page reads"
```

---

### Task 5: Peak memory footprint

**Files:**
- Create: `packages/ingest/src/fra_ingest/footprint.py`
- Modify: `packages/ingest/tests/support.py` (add `run_python`)
- Test: `packages/ingest/tests/test_footprint.py`

**Interfaces:**
- Produces: `peak_footprint_gb() -> float`, this process's lifetime peak physical footprint in GiB (macOS `proc_pid_rusage`, `ri_lifetime_max_phys_footprint`; elsewhere `ru_maxrss`). Test helper `run_python(code: str) -> subprocess.CompletedProcess[str]` that runs code in a fresh interpreter with this process's `sys.path`.

- [ ] **Step 1: Add `run_python` to `support.py`**

Add `import os`, `import subprocess`, `import sys`, then:

```python
def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter that sees the same packages as the tests. uv writes the
    workspace .pth files hidden on this machine and Python skips hidden .pth files, so the
    path is passed explicitly."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
```

- [ ] **Step 2: Write the failing tests**

```python
"""The child's lifetime peak memory (spec 10, Peak memory; R26)."""

from __future__ import annotations

from support import run_python

from fra_ingest.footprint import peak_footprint_gb

MEASURE = "from fra_ingest.footprint import peak_footprint_gb\n{body}\nprint(peak_footprint_gb())"


def test_the_peak_is_a_plausible_figure() -> None:
    assert 0.005 < peak_footprint_gb() < 64


def test_the_peak_counts_memory_the_process_touched() -> None:
    idle = float(run_python(MEASURE.format(body="")).stdout)
    busy = float(run_python(MEASURE.format(body='block = b"\\x01" * (500 * 1024**2)')).stdout)
    assert busy - idle > 0.4
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_footprint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.footprint'`.

- [ ] **Step 4: Implement `footprint.py`**

```python
"""This process's lifetime peak memory (spec 10, Peak memory).

On Apple silicon, memory PyTorch allocates through MPS is not all counted in RSS, so on macOS
the figure is the lifetime peak physical footprint, the one Activity Monitor shows. The child
reads it for itself just before writing: once it exits the figure is gone (R26).
"""

from __future__ import annotations

import ctypes
import os
import resource
import sys
from typing import Any

_RUSAGE_INFO_V4 = 4
_GIB = 1024**3
_V4_FIELDS = (
    "ri_user_time",
    "ri_system_time",
    "ri_pkg_idle_wkups",
    "ri_interrupt_wkups",
    "ri_pageins",
    "ri_wired_size",
    "ri_resident_size",
    "ri_phys_footprint",
    "ri_proc_start_abstime",
    "ri_proc_exit_abstime",
    "ri_child_user_time",
    "ri_child_system_time",
    "ri_child_pkg_idle_wkups",
    "ri_child_interrupt_wkups",
    "ri_child_pageins",
    "ri_child_elapsed_abstime",
    "ri_diskio_bytesread",
    "ri_diskio_byteswritten",
    "ri_cpu_time_qos_default",
    "ri_cpu_time_qos_maintenance",
    "ri_cpu_time_qos_background",
    "ri_cpu_time_qos_utility",
    "ri_cpu_time_qos_legacy",
    "ri_cpu_time_qos_user_initiated",
    "ri_cpu_time_qos_user_interactive",
    "ri_billed_system_time",
    "ri_serviced_system_time",
    "ri_logical_writes",
    "ri_lifetime_max_phys_footprint",
    "ri_instructions",
    "ri_cycles",
    "ri_billed_energy",
    "ri_serviced_energy",
    "ri_interval_max_phys_footprint",
    "ri_runnable_time",
)


class _RusageInfoV4(ctypes.Structure):
    """``struct rusage_info_v4`` from <sys/resource.h>."""

    _fields_: list[tuple[str, Any]] = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        *((name, ctypes.c_uint64) for name in _V4_FIELDS),
    ]


def peak_footprint_gb() -> float:
    if sys.platform == "darwin":
        info = _RusageInfoV4()
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if libsystem.proc_pid_rusage(os.getpid(), _RUSAGE_INFO_V4, ctypes.byref(info)) == 0:
            return float(info.ri_lifetime_max_phys_footprint) / _GIB
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    return peak / _GIB if sys.platform == "darwin" else peak * 1024 / _GIB
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_footprint.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
make test && make typecheck
git add packages/ingest/src/fra_ingest/footprint.py packages/ingest/tests/support.py packages/ingest/tests/test_footprint.py
git commit -m "Measure the lifetime peak memory footprint, which counts GPU allocations"
```

---

### Task 6: docling adapter

**Files:**
- Create: `packages/ingest/src/fra_ingest/converter.py`
- Test: `packages/ingest/tests/test_converter.py`

**Interfaces:**
- Consumes: `IngestConfig` (Task 2), `RangePlan` (Task 3), docling names confirmed in Task 1.
- Produces in `fra_ingest.converter`:
  - `@dataclass RangeOutput(status: Literal["ok", "partial", "failed"], page_numbers: list[int], page_images: dict[int, Image.Image | None], tables: int, errors: list[str], write_json: Callable[[Path], None])`
  - `class RangeRunner(Protocol)`: attribute `models_seconds: float`; `__call__(self, pdf: Path, plan: RangePlan) -> RangeOutput`
  - `class DoclingRunner(RangeRunner)`: `__init__(self, config: IngestConfig)`; one `DocumentConverter` per `(plan.ocr, plan.ocr_language)`; adds model load time to `models_seconds`.
  - `docling_version() -> str`
  - `pipeline_options(config: IngestConfig, plan: RangePlan) -> Any`

- [ ] **Step 1: Write the failing tests**

```python
"""The docling adapter (spec 10, Components)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from support import run_python

from fra_ingest.config import IngestConfig
from fra_ingest.converter import DoclingRunner, docling_version, pipeline_options
from fra_ingest.results import RangePlan

ALMARAI_EN = "almarai-2025-en-annualreport.pdf"


def test_importing_the_converter_loads_neither_docling_nor_torch() -> None:
    code = (
        "import sys, fra_ingest.converter\n"
        "print('docling' in sys.modules, 'torch' in sys.modules)"
    )
    assert run_python(code).stdout.split() == ["False", "False"]


def test_the_docling_version_is_the_pinned_one() -> None:
    assert docling_version() == "2.126.0"


@pytest.mark.slow
def test_options_follow_the_plan_and_the_settings() -> None:
    config = IngestConfig(device="cpu", images_scale=1.5, batch_size=3)
    scanned = RangePlan(first_page=5, last_page=5, ocr="full_page", ocr_language="ar-SA")
    options = pipeline_options(config, scanned)
    assert options.do_ocr is True
    assert options.ocr_options.lang == ["ar-SA"]
    assert options.ocr_options.mode.name == "FULL_PAGE"
    assert options.generate_page_images is True
    assert options.images_scale == 1.5
    assert (options.ocr_batch_size, options.layout_batch_size, options.table_batch_size) == (3, 3, 3)
    assert options.accelerator_options.device.name == "CPU"
    assert options.table_structure_options.mode.name == "ACCURATE"

    digital = RangePlan(first_page=1, last_page=2, ocr="pdf_aware", ocr_language="en-US")
    assert pipeline_options(config, digital).ocr_options.mode.name == "PDF_AWARE_LAYOUT_REGIONS"

    no_engine = RangePlan(first_page=1, last_page=2, ocr="pdf_aware", ocr_language=None)
    assert pipeline_options(config, no_engine).do_ocr is False


@pytest.mark.slow
@pytest.mark.golden
def test_almarai_balance_sheet_converts_with_its_page_numbers_and_images(
    golden: Callable[[str], Path], tmp_path: Path
) -> None:
    pdf = golden(ALMARAI_EN)
    runner = DoclingRunner(IngestConfig())
    plan = RangePlan(first_page=156, last_page=158, ocr="pdf_aware", ocr_language="en-US")
    output = runner(pdf, plan)

    assert output.status == "ok"
    assert output.page_numbers == [156, 157, 158]
    assert output.tables >= 1
    image = output.page_images[156]
    assert image is not None
    document = pdfium.PdfDocument(pdf)
    width_pt = document[155].get_size()[0]
    document.close()
    assert abs(image.width - 2 * width_pt) <= 2
    output.write_json(tmp_path / "doc.json")
    assert (tmp_path / "doc.json").stat().st_size > 1000
    assert runner.models_seconds > 0


@pytest.mark.slow
@pytest.mark.golden
def test_a_scanned_arabic_statement_converts_with_full_page_ocr(
    golden: Callable[[str], Path],
) -> None:
    runner = DoclingRunner(IngestConfig())
    plan = RangePlan(
        first_page=5, last_page=5, ocr="full_page", ocr_language="ar-SA", image_pages=(5,)
    )
    output = runner(golden("juhayna-2025-ar-consolidated.pdf"), plan)
    assert output.status == "ok"
    assert output.page_numbers == [5]
    assert output.tables >= 1
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_converter.py -v -m "slow or not slow"`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.converter'`.

- [ ] **Step 3: Implement `converter.py`**

```python
"""docling behind one call per range.

The only module that imports docling, and only inside functions, so importing it does not load
PyTorch (spec 10, Components). Everything it returns is in our own types.
"""

from __future__ import annotations

import importlib.metadata
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from PIL import Image

from fra_ingest.config import IngestConfig
from fra_ingest.results import RangePlan

RunStatus = Literal["ok", "partial", "failed"]


@dataclass
class RangeOutput:
    """What one docling run over one range gave."""

    status: RunStatus
    page_numbers: list[int]
    page_images: dict[int, Image.Image | None]
    tables: int
    errors: list[str]
    write_json: Callable[[Path], None]


class RangeRunner(Protocol):
    models_seconds: float

    def __call__(self, pdf: Path, plan: RangePlan) -> RangeOutput: ...


def docling_version() -> str:
    return importlib.metadata.version("docling")


def pipeline_options(config: IngestConfig, plan: RangePlan) -> Any:
    """The options of 04, 1.2, with the OCR mode and one language taken from the plan."""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.pipeline_options import (
        OcrMacOptions,
        OcrMode,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
    )

    if plan.ocr == "skipped":
        msg = f"range {plan.label} is skipped and has no pipeline"
        raise ValueError(msg)
    devices = {
        "mps": AcceleratorDevice.MPS,
        "cpu": AcceleratorDevice.CPU,
        "auto": AcceleratorDevice.AUTO,
    }
    options = PdfPipelineOptions(
        do_ocr=plan.ocr_language is not None,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=config.do_cell_matching,
        ),
        generate_page_images=True,
        images_scale=config.images_scale,
        ocr_batch_size=config.batch_size,
        layout_batch_size=config.batch_size,
        table_batch_size=config.batch_size,
        document_timeout=config.document_timeout_s,
        accelerator_options=AcceleratorOptions(device=devices[config.device]),
    )
    if plan.ocr_language is not None:
        mode = OcrMode.FULL_PAGE if plan.ocr == "full_page" else OcrMode.PDF_AWARE_LAYOUT_REGIONS
        options.ocr_options = OcrMacOptions(lang=[plan.ocr_language], mode=mode)
    return options


class DoclingRunner:
    """Converts ranges, keeping one converter per (OCR mode, language) so a document loads
    docling's models once in the usual case."""

    def __init__(self, config: IngestConfig) -> None:
        self.config = config
        self.models_seconds = 0.0
        self._converters: dict[tuple[str, str | None], Any] = {}

    def _converter(self, plan: RangePlan) -> Any:
        key = (plan.ocr, plan.ocr_language)
        if key not in self._converters:
            started = time.perf_counter()
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter, PdfFormatOption

            options = pipeline_options(self.config, plan)
            converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
            )
            converter.initialize_pipeline(InputFormat.PDF)
            self._converters[key] = converter
            self.models_seconds += time.perf_counter() - started
        return self._converters[key]

    def __call__(self, pdf: Path, plan: RangePlan) -> RangeOutput:
        from docling.datamodel.base_models import ConversionStatus
        from docling_core.types.doc import ImageRefMode

        converter = self._converter(plan)
        result = converter.convert(
            pdf, page_range=(plan.first_page, plan.last_page), raises_on_error=False
        )
        document = result.document
        statuses: dict[Any, RunStatus] = {
            ConversionStatus.SUCCESS: "ok",
            ConversionStatus.PARTIAL_SUCCESS: "partial",
        }
        images: dict[int, Image.Image | None] = {}
        for page_no, page in document.pages.items():
            images[int(page_no)] = page.image.pil_image if page.image is not None else None

        def write_json(path: Path) -> None:
            # Page images are written as PNGs beside it, not embedded.
            document.save_as_json(path, image_mode=ImageRefMode.PLACEHOLDER)

        return RangeOutput(
            status=statuses.get(result.status, "failed"),
            page_numbers=sorted(images),
            page_images=images,
            tables=len(document.tables),
            errors=[str(error.error_message) for error in result.errors],
            write_json=write_json,
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_converter.py -v -m "slow or not slow"`
Expected: PASS. The two golden conversions take tens of seconds (model load dominates).

If `test_a_scanned_arabic_statement_converts_with_full_page_ocr` finds no table, keep the test, mark it `xfail(strict=True, reason=...)` with what docling returned, and report it: it is a Part 3 input problem to record in spec 10's Results, not a reason to change the adapter.

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/converter.py packages/ingest/tests/test_converter.py
git commit -m "Add the docling adapter: options per range plan, page images, JSON without embedded images"
```

---

### Task 7: The convert stage

**Files:**
- Create: `packages/ingest/src/fra_ingest/convert.py`
- Modify: `packages/ingest/tests/support.py` (add `FakeRunner`)
- Test: `packages/ingest/tests/test_convert.py`

**Interfaces:**
- Consumes: `plan_ranges` (Task 4), `peak_footprint_gb` (Task 5), `RangeOutput`, `RangeRunner`, `DoclingRunner`, `docling_version` (Task 6), `ConvertResult`, `RangeConversion`, `RangePlan` (Task 3).
- Produces in `fra_ingest.convert`:
  - `CONVERT_VERSION = "1"`
  - `settings_hash(config: IngestConfig, plans: Sequence[RangePlan], docling: str, locate_version: str) -> str`
  - `convert_pdf(pdf: Path, located: LocateResult, ocr_languages: Mapping[int, str | None], config: IngestConfig, *, runner_factory: Callable[[IngestConfig], RangeRunner] = DoclingRunner, use_cache: bool = True, docling: str | None = None, timings: Mapping[str, float] | None = None) -> ConvertResult`, which writes `<artifact_root>/<sha256>/convert.json` and returns what it wrote.
  - Range flags: `convert_failed:<a-b>`, `convert_partial:<a-b>`, `page_outside_range:<a-b>`, `ocr_unavailable:<a-b>`, `page_image_missing:<n>`, `page_not_converted:<n>`, `error:<text>`. Result flags: `no_statements_found`, `all_ranges_failed`, `memory_over_budget`.

- [ ] **Step 1: Add `FakeRunner` to `support.py`**

Add imports `from typing import Literal` and `from fra_ingest.converter import RangeOutput` and `from fra_ingest.results import RangePlan` (merge with the existing results import), then:

```python
class FakeRunner:
    """Stands in for DoclingRunner. ``behaviour`` maps a range label such as "2-3" to one of
    ok, partial, failed, raise, outside, no_image or short; unlisted ranges are ok."""

    def __init__(self, behaviour: Mapping[str, str] | None = None) -> None:
        self.behaviour = dict(behaviour or {})
        self.calls: list[RangePlan] = []
        self.models_seconds = 0.0

    def __call__(self, pdf: Path, plan: RangePlan) -> RangeOutput:
        self.calls.append(plan)
        kind = self.behaviour.get(plan.label, "ok")
        if kind == "raise":
            msg = "docling stopped"
            raise RuntimeError(msg)
        pages = list(range(plan.first_page, plan.last_page + 1))
        if kind == "outside":
            pages = [n + 100 for n in pages]
        if kind == "short":
            pages = pages[:1]
        images: dict[int, Image.Image | None] = {
            n: None if kind == "no_image" else Image.new("RGB", (20, 30), "white") for n in pages
        }
        statuses: dict[str, Literal["ok", "partial", "failed"]] = {
            "partial": "partial",
            "failed": "failed",
        }
        status = statuses.get(kind, "ok")
        return RangeOutput(
            status=status,
            page_numbers=pages,
            page_images=images,
            tables=1,
            errors=[] if status == "ok" else ["page 3: document timeout exceeded"],
            write_json=lambda path: path.write_text("{}", encoding="utf-8"),
        )
```

- [ ] **Step 2: Write the failing tests**

```python
"""The convert stage inside the child (spec 10, Data flow and Failure handling)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from support import FakeRunner, located

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.convert import CONVERT_VERSION, convert_pdf, settings_hash
from fra_ingest.converter import RangeRunner
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, LocateResult

T, I = PageMode.TEXT, PageMode.IMAGE
SHA = "c" * 64
PDF = Path("doc.pdf")


def config(tmp_path: Path, **changes: object) -> IngestConfig:
    return IngestConfig(artifact_root=tmp_path).model_copy(update=changes)


def factory(runner: FakeRunner) -> Callable[[IngestConfig], RangeRunner]:
    return lambda _config: runner


def never(_config: IngestConfig) -> RangeRunner:
    raise AssertionError("docling must not load")


def doc(ranges: list[tuple[int, int]], modes: list[PageMode] | None = None) -> LocateResult:
    return located(modes or [T] * 6, ranges, sha256=SHA)


def run(
    tmp_path: Path, runner: FakeRunner, located_result: LocateResult, **changes: object
) -> ConvertResult:
    return convert_pdf(
        PDF,
        located_result,
        {},
        config(tmp_path, **changes),
        runner_factory=factory(runner),
        docling="2.126.0",
    )


def test_each_range_is_converted_and_written(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = run(tmp_path, runner, doc([(2, 3), (5, 5)]))
    out = tmp_path / SHA

    assert [p.label for p in runner.calls] == ["2-3", "5-5"]
    assert [r.status for r in result.ranges] == ["ok", "ok"]
    assert [r.docling_path for r in result.ranges] == ["docling/p2-3.json", "docling/p5-5.json"]
    assert result.page_images == {2: "pages/2.png", 3: "pages/3.png", 5: "pages/5.png"}
    assert all((out / path).is_file() for path in result.page_images.values())
    assert (out / "docling" / "p2-3.json").is_file()
    assert ConvertResult.model_validate_json((out / "convert.json").read_text()) == result
    assert result.version == CONVERT_VERSION
    assert result.docling_version == "2.126.0"
    assert result.peak_footprint_gb is not None and result.peak_footprint_gb > 0
    assert {"models", "convert", "write"} <= set(result.timings)


def test_no_ranges_writes_a_result_without_loading_docling(tmp_path: Path) -> None:
    result = convert_pdf(
        PDF, doc([]), {}, config(tmp_path), runner_factory=never, docling="2.126.0"
    )
    assert result.flags == ["no_statements_found"]
    assert result.ranges == []
    assert (tmp_path / SHA / "convert.json").is_file()


def test_a_second_run_with_the_same_settings_is_served_from_convert_json(tmp_path: Path) -> None:
    first = run(tmp_path, FakeRunner(), doc([(2, 3)]))
    again = FakeRunner()
    assert run(tmp_path, again, doc([(2, 3)])) == first
    assert again.calls == []


def test_a_changed_setting_converts_again(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]), images_scale=1.0)
    assert len(again.calls) == 1


def test_use_cache_false_converts_again(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    again = FakeRunner()
    convert_pdf(
        PDF,
        doc([(2, 3)]),
        {},
        config(tmp_path),
        runner_factory=factory(again),
        docling="2.126.0",
        use_cache=False,
    )
    assert len(again.calls) == 1


def test_a_missing_artifact_file_converts_again(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    (tmp_path / SHA / "pages" / "3.png").unlink()
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]))
    assert len(again.calls) == 1


def test_leftovers_without_convert_json_are_not_trusted(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    (tmp_path / SHA / "convert.json").unlink()
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3)]))
    assert len(again.calls) == 1


def test_a_cached_result_with_a_failed_range_is_retried(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner({"2-3": "raise"}), doc([(2, 3), (5, 5)]))
    again = FakeRunner()
    run(tmp_path, again, doc([(2, 3), (5, 5)]))
    assert [p.label for p in again.calls] == ["2-3", "5-5"]


def test_files_from_an_earlier_run_are_removed(tmp_path: Path) -> None:
    run(tmp_path, FakeRunner(), doc([(1, 1)]))
    run(tmp_path, FakeRunner(), doc([(2, 3)]))
    out = tmp_path / SHA
    assert sorted(p.name for p in (out / "docling").iterdir()) == ["p2-3.json"]
    assert sorted(p.name for p in (out / "pages").iterdir()) == ["2.png", "3.png"]


def test_a_range_that_raises_fails_alone(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "raise"}), doc([(2, 3), (5, 5)]))
    failed, ok = result.ranges
    assert failed.status == "failed"
    assert failed.docling_path is None
    assert "convert_failed:2-3" in failed.flags
    assert any(f.startswith("error:RuntimeError") for f in failed.flags)
    assert ok.status == "ok"
    assert not result.all_failed


def test_a_partial_range_keeps_its_output_and_says_why(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "partial"}), doc([(2, 3)]))
    (partial,) = result.ranges
    assert partial.status == "partial"
    assert partial.docling_path == "docling/p2-3.json"
    assert "convert_partial:2-3" in partial.flags
    assert "error:page 3: document timeout exceeded" in partial.flags


def test_a_docling_failure_status_fails_the_range(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "failed"}), doc([(2, 3)]))
    assert result.ranges[0].status == "failed"
    assert "convert_failed:2-3" in result.ranges[0].flags
    assert result.all_failed
    assert "all_ranges_failed" in result.flags


def test_pages_outside_the_range_fail_it(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "outside"}), doc([(2, 3)]))
    (bad,) = result.ranges
    assert bad.status == "failed"
    assert "page_outside_range:2-3" in bad.flags
    assert bad.docling_path is None
    assert result.page_images == {}


def test_a_page_without_an_image_is_flagged(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "no_image"}), doc([(2, 3)]))
    assert {"page_image_missing:2", "page_image_missing:3"} <= set(result.ranges[0].flags)
    assert result.page_images == {}


def test_a_page_docling_did_not_return_is_flagged(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner({"2-3": "short"}), doc([(2, 3)]))
    assert "page_not_converted:3" in result.ranges[0].flags
    assert result.page_images == {2: "pages/2.png"}


def test_without_an_ocr_engine_image_ranges_are_skipped_and_docling_never_loads(
    tmp_path: Path,
) -> None:
    result = convert_pdf(
        PDF,
        doc([(2, 3)], [T, I, T, T]),
        {},
        config(tmp_path, convert_ocr="none"),
        runner_factory=never,
        docling="2.126.0",
    )
    (skipped,) = result.ranges
    assert skipped.status == "skipped"
    assert skipped.flags == ["ocr_unavailable:2-3"]
    assert not result.all_failed


def test_a_peak_above_the_budget_is_flagged(tmp_path: Path) -> None:
    result = run(tmp_path, FakeRunner(), doc([(2, 3)]), memory_budget_gb=0.001)
    assert "memory_over_budget" in result.flags


def test_timings_passed_in_are_kept(tmp_path: Path) -> None:
    result = convert_pdf(
        PDF,
        doc([(2, 3)]),
        {},
        config(tmp_path),
        runner_factory=factory(FakeRunner()),
        docling="2.126.0",
        timings={"locate": 1.5},
    )
    assert result.timings["locate"] == 1.5


@pytest.mark.parametrize("change", [{"device": "cpu"}, {"do_cell_matching": False}])
def test_the_settings_hash_follows_settings_and_plans(
    tmp_path: Path, change: dict[str, object]
) -> None:
    base = config(tmp_path)
    plans = plan_ranges(doc([(2, 3)]), {}, base)
    digest = settings_hash(base, plans, "2.126.0", "2")
    assert settings_hash(base.model_copy(update=change), plans, "2.126.0", "2") != digest
    other = plan_ranges(doc([(2, 4)]), {}, base)
    assert settings_hash(base, other, "2.126.0", "2") != digest
    assert settings_hash(base, plans, "2.127.0", "2") != digest
    assert settings_hash(base.model_copy(update={"child_timeout_s": 5.0}), plans, "2.126.0", "2") == digest
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_convert.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.convert'`.

- [ ] **Step 4: Implement `convert.py`**

```python
"""The convert stage inside the child: plan, run docling per range, write the artifacts
(spec 10, Data flow and Failure handling)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from fra_ingest.config import IngestConfig
from fra_ingest.converter import DoclingRunner, RangeRunner, docling_version
from fra_ingest.footprint import peak_footprint_gb
from fra_ingest.ocr_policy import plan_ranges
from fra_ingest.results import ConvertResult, LocateResult, RangeConversion, RangePlan

CONVERT_VERSION = "1"

# Errors that mean our code is wrong, not that docling could not read the pages. They are
# raised, as in the pages stage, so a broken adapter cannot pass as unreadable ranges.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError)
_ERROR_CHARS = 200


def settings_hash(
    config: IngestConfig, plans: Sequence[RangePlan], docling: str, locate_version: str
) -> str:
    """Everything that changes what convert writes. The timeouts on the child and the memory
    budget change how a run is judged, not its output, so they are left out."""
    payload = {
        "device": config.device,
        "ocr_engine": config.convert_ocr,
        "images_scale": config.images_scale,
        "batch_size": config.batch_size,
        "do_cell_matching": config.do_cell_matching,
        "document_timeout_s": config.document_timeout_s,
        "docling": docling,
        "locate": locate_version,
        "plans": [plan.model_dump(mode="json") for plan in plans],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def convert_pdf(
    pdf: Path,
    located: LocateResult,
    ocr_languages: Mapping[int, str | None],
    config: IngestConfig,
    *,
    runner_factory: Callable[[IngestConfig], RangeRunner] = DoclingRunner,
    use_cache: bool = True,
    docling: str | None = None,
    timings: Mapping[str, float] | None = None,
) -> ConvertResult:
    out_dir = config.artifact_root / located.document.sha256
    plans = plan_ranges(located, ocr_languages, config)
    release = docling or docling_version()
    digest = settings_hash(config, plans, release, located.version)
    if use_cache and (cached := _cached(out_dir, digest)) is not None:
        return cached

    for name in ("docling", "pages"):
        shutil.rmtree(out_dir / name, ignore_errors=True)
        (out_dir / name).mkdir(parents=True)

    runner: RangeRunner | None = None
    ranges: list[RangeConversion] = []
    page_images: dict[int, str] = {}
    run_seconds = 0.0
    for plan in plans:
        if plan.ocr == "skipped":
            ranges.append(_conversion(plan, "skipped", flags=[f"ocr_unavailable:{plan.label}"]))
            continue
        if runner is None:
            runner = runner_factory(config)
        started = time.perf_counter()
        ranges.append(_convert_range(runner, pdf, plan, out_dir, page_images))
        run_seconds += time.perf_counter() - started

    models = runner.models_seconds if runner is not None else 0.0
    flags = [] if plans else ["no_statements_found"]
    result = ConvertResult(
        version=CONVERT_VERSION,
        sha256=located.document.sha256,
        locate_version=located.version,
        docling_version=release,
        device=config.device,
        settings_hash=digest,
        ranges=ranges,
        page_images=page_images,
        flags=flags,
        timings={**(timings or {}), "models": models, "convert": max(run_seconds - models, 0.0)},
    )
    if result.all_failed:
        result.flags.append("all_ranges_failed")
    return _write(out_dir, result, config)


def _convert_range(
    runner: RangeRunner,
    pdf: Path,
    plan: RangePlan,
    out_dir: Path,
    page_images: dict[int, str],
) -> RangeConversion:
    started = time.perf_counter()
    try:
        output = runner(pdf, plan)
    except _PROGRAMMING_ERRORS:
        raise
    except Exception as exc:
        return _conversion(
            plan,
            "failed",
            seconds=time.perf_counter() - started,
            flags=[f"convert_failed:{plan.label}", _error(f"{type(exc).__name__}: {exc}")],
        )

    seconds = time.perf_counter() - started
    errors = [_error(text) for text in output.errors]
    if any(not plan.first_page <= n <= plan.last_page for n in output.page_numbers):
        return _conversion(
            plan, "failed", seconds=seconds, flags=[f"page_outside_range:{plan.label}"]
        )
    if output.status == "failed":
        return _conversion(
            plan, "failed", seconds=seconds, flags=[f"convert_failed:{plan.label}", *errors]
        )

    flags = [f"convert_partial:{plan.label}", *errors] if output.status == "partial" else []
    docling_path = f"docling/p{plan.first_page}-{plan.last_page}.json"
    output.write_json(out_dir / docling_path)
    for page_no in range(plan.first_page, plan.last_page + 1):
        if page_no not in output.page_images:
            flags.append(f"page_not_converted:{page_no}")
            continue
        image = output.page_images[page_no]
        if image is None:
            flags.append(f"page_image_missing:{page_no}")
            continue
        relative = f"pages/{page_no}.png"
        image.save(out_dir / relative)
        page_images[page_no] = relative
    return _conversion(
        plan,
        output.status,
        seconds=time.perf_counter() - started,
        docling_path=docling_path,
        tables=output.tables,
        flags=flags,
    )


def _conversion(
    plan: RangePlan,
    status: str,
    *,
    seconds: float = 0.0,
    docling_path: str | None = None,
    tables: int = 0,
    flags: list[str] | None = None,
) -> RangeConversion:
    return RangeConversion.model_validate(
        {
            "first_page": plan.first_page,
            "last_page": plan.last_page,
            "ocr": plan.ocr,
            "ocr_language": plan.ocr_language,
            "docling_path": docling_path,
            "tables": tables,
            "seconds": seconds,
            "status": status,
            "flags": flags or [],
        }
    )


def _error(text: str) -> str:
    return f"error:{' '.join(text.split())[:_ERROR_CHARS]}"


def _cached(out_dir: Path, digest: str) -> ConvertResult | None:
    """A previous result, when it was made with the same settings, converted every range
    without failure, and every file it names is still there. A failed or partial range is
    retried, as a failed OCR read is in the pages stage."""
    path = out_dir / "convert.json"
    if not path.is_file():
        return None
    try:
        result = ConvertResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None
    if result.version != CONVERT_VERSION or result.settings_hash != digest:
        return None
    if any(r.status in ("failed", "partial") for r in result.ranges):
        return None
    files = [r.docling_path for r in result.ranges if r.docling_path is not None]
    files += list(result.page_images.values())
    if not all((out_dir / name).is_file() for name in files):
        return None
    return result


def _write(out_dir: Path, result: ConvertResult, config: IngestConfig) -> ConvertResult:
    started = time.perf_counter()
    peak = peak_footprint_gb()
    flags = list(result.flags)
    if peak > config.memory_budget_gb:
        flags.append("memory_over_budget")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = result.model_copy(update={"peak_footprint_gb": peak, "flags": flags})
    final.timings["write"] = time.perf_counter() - started
    temporary = out_dir / "convert.json.tmp"
    temporary.write_text(final.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, out_dir / "convert.json")
    return final
```

Note: `_write` records its own time before the final `write_text`, so `write` covers the peak read and flagging, not the last file write; that is small and keeps the written file equal to the returned result.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_convert.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/convert.py packages/ingest/tests/support.py packages/ingest/tests/test_convert.py
git commit -m "Add the convert stage: per-range docling runs, page images, flags, cached by settings"
```

---

### Task 8: Locate reuse and the `convert` command

**Files:**
- Modify: `packages/ingest/src/fra_ingest/stage.py`
- Modify: `packages/ingest/src/fra_ingest/cli.py`
- Test: `packages/ingest/tests/test_stage.py` (create), `packages/ingest/tests/test_cli.py`

**Interfaces:**
- Consumes: `convert_pdf` (Task 7), `locate_pdf`, `read_pages`, `LOCATE_VERSION` (Part 1).
- Produces in `fra_ingest.stage`: `load_or_locate(pdf_path: Path, config: IngestConfig, ocr: OcrEngine | None) -> LocateResult`; `page_ocr_languages(pdf_path: Path, config: IngestConfig, ocr: OcrEngine | None) -> dict[int, str | None]`.
- Produces CLI: `fra-ingest convert <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]`. Exit 0 on a written result (including partial, skipped and no statements), 2 on `IngestError` with `<pdf>: <reason> <detail>` as the last stderr line, 3 when every attempted range failed. `--no-cache` reconverts; `locate.json` and the page cache are still reused.

- [ ] **Step 1: Write the failing stage tests** (`test_stage.py`)

```python
"""Reusing locate.json for the convert stage (spec 10, Data flow step 1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from support import make_blank_pdf

from fra_ingest import stage
from fra_ingest.config import IngestConfig
from fra_ingest.pages import sha256_file
from fra_ingest.results import LocateResult


def config(tmp_path: Path) -> IngestConfig:
    return IngestConfig(artifact_root=tmp_path / "artifacts")


def test_a_current_locate_json_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=2)
    first = stage.load_or_locate(pdf, config(tmp_path), None)

    def refuse(*_args: object, **_kwargs: object) -> LocateResult:
        raise AssertionError("locate ran again")

    monkeypatch.setattr(stage, "locate_pdf", refuse)
    assert stage.load_or_locate(pdf, config(tmp_path), None) == first


@pytest.mark.parametrize("content", ["{not json", '{"version": "0"}'])
def test_a_corrupt_or_old_locate_json_is_recomputed(tmp_path: Path, content: str) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=2)
    out = config(tmp_path).artifact_root / sha256_file(pdf)
    out.mkdir(parents=True)
    (out / "locate.json").write_text(content, encoding="utf-8")
    result = stage.load_or_locate(pdf, config(tmp_path), None)
    assert result.document.page_count == 2
    assert LocateResult.model_validate_json((out / "locate.json").read_text()) == result


def test_page_languages_come_from_the_page_cache(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=2)
    stage.load_or_locate(pdf, config(tmp_path), None)
    assert stage.page_ocr_languages(pdf, config(tmp_path), None) == {1: None, 2: None}
```

- [ ] **Step 2: Write the failing CLI tests** (append to `test_cli.py`; add imports `from support import make_blank_pdf`, `from fra_ingest import cli`, `from fra_ingest.results import ConvertResult, RangeConversion`)

```python
def canned(sha256: str, status: str) -> ConvertResult:
    return ConvertResult.model_validate(
        {
            "version": "1",
            "sha256": sha256,
            "locate_version": "2",
            "docling_version": "2.126.0",
            "device": "mps",
            "settings_hash": "h",
            "ranges": [
                RangeConversion.model_validate(
                    {
                        "first_page": 1,
                        "last_page": 1,
                        "ocr": "full_page",
                        "ocr_language": "en-US",
                        "status": status,
                    }
                )
            ],
            "peak_footprint_gb": 1.2,
        }
    )


@pytest.mark.parametrize(("status", "code"), [("ok", 0), ("partial", 0), ("failed", 3)])
def test_convert_exits_by_how_the_ranges_ended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    code: int,
) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf")
    seen: dict[str, object] = {}

    def fake_convert(pdf_path: Path, located: object, languages: object, config: object, **kwargs: object) -> ConvertResult:
        seen.update(kwargs)
        return canned(located.document.sha256, status)  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "convert_pdf", fake_convert)
    argv = ["convert", str(pdf), "--no-ocr", "--artifacts", str(tmp_path / "a"), "--json"]
    assert main(argv) == code
    assert ConvertResult.model_validate_json(capsys.readouterr().out).ranges[0].status == status
    assert seen["use_cache"] is True
    assert "locate" in seen["timings"]  # type: ignore[operator]


def test_convert_no_cache_reconverts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_blank_pdf(tmp_path / "doc.pdf")
    seen: dict[str, object] = {}

    def fake_convert(pdf_path: Path, located: object, languages: object, config: object, **kwargs: object) -> ConvertResult:
        seen.update(kwargs)
        return canned(located.document.sha256, "ok")  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "convert_pdf", fake_convert)
    main(["convert", str(pdf), "--no-ocr", "--no-cache", "--artifacts", str(tmp_path / "a")])
    assert seen["use_cache"] is False


def test_convert_of_an_unreadable_file_exits_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "تقرير سنوي.pdf"
    path.write_bytes(b"not a pdf")
    assert main(["convert", str(path), "--no-ocr", "--artifacts", str(tmp_path / "a")]) == 2
    assert capsys.readouterr().err.strip().splitlines()[-1].startswith(f"{path}: unreadable_pdf")
```

- [ ] **Step 3: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_stage.py packages/ingest/tests/test_cli.py -v`
Expected: FAIL (`AttributeError: module 'fra_ingest.stage' has no attribute 'load_or_locate'`; `invalid choice: 'convert'`).

- [ ] **Step 4: Implement in `stage.py`**

Add imports `from pydantic import ValidationError` and `from fra_ingest.locate import LOCATE_VERSION, locate`, then:

```python
def load_or_locate(pdf_path: Path, config: IngestConfig, ocr: OcrEngine | None) -> LocateResult:
    """The stored locate result when it is current, else a fresh one. A corrupt or older
    ``locate.json`` is recomputed, never trusted."""
    if not pdf_path.is_file():
        raise IngestError("unreadable_pdf", f"{pdf_path}: not a file")
    stored = config.artifact_root / sha256_file(pdf_path) / "locate.json"
    if stored.is_file():
        try:
            result = LocateResult.model_validate_json(stored.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            result = None
        if result is not None and result.version == LOCATE_VERSION:
            return result
    return locate_pdf(pdf_path, config, ocr)


def page_ocr_languages(
    pdf_path: Path, config: IngestConfig, ocr: OcrEngine | None
) -> dict[int, str | None]:
    """The OCR language Part 1 kept for each page, read back from the page cache locate
    filled, so no OCR runs here when locate ran with the same engine."""
    out_dir = config.artifact_root / sha256_file(pdf_path)
    return {
        page.page_no: page.ocr_language
        for page in read_pages(pdf_path, config, ocr, cache_dir=out_dir)
    }
```

- [ ] **Step 5: Implement the CLI**

Replace `cli.py` with:

```python
"""Command line for the ingest stages.

fra-ingest locate <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]
fra-ingest convert <pdf> [--json] [--no-ocr] [--no-cache] [--config PATH] [--artifacts DIR]

convert exits 0 when it wrote a result, 2 on an ingest error and 3 when every range it
attempted failed. It is the child process of convert_in_child (spec 10).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from fra_ingest.config import IngestConfig, load_config
from fra_ingest.convert import convert_pdf
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngine, default_engine
from fra_ingest.results import ConvertResult, LocateResult
from fra_ingest.stage import load_or_locate, locate_pdf, page_ocr_languages

EXIT_ERROR = 2
EXIT_ALL_FAILED = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fra-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("locate", "find the statement pages of a PDF"),
        ("convert", "convert the located statement pages with docling"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("pdf", type=Path)
        command.add_argument("--json", action="store_true", help="print the full result as JSON")
        command.add_argument("--no-ocr", action="store_true", help="leave image pages unread")
        command.add_argument(
            "--no-cache",
            action="store_true",
            help="locate: ignore the page text cache; convert: convert again",
        )
        command.add_argument("--config", type=Path, default=None)
        command.add_argument("--artifacts", type=Path, default=None, help="artifact root override")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.artifacts is not None:
        config = config.model_copy(update={"artifact_root": args.artifacts})
    engine = None if args.no_ocr else default_engine()

    try:
        if args.command == "convert":
            return _convert(args, config, engine)
        result = locate_pdf(args.pdf, config, engine, use_cache=not args.no_cache)
    except IngestError as exc:
        print(f"{args.pdf}: {exc.reason} {exc.detail}".rstrip(), file=sys.stderr)
        return EXIT_ERROR

    print(result.model_dump_json(indent=2) if args.json else _summary(result))
    return 0


def _convert(args: argparse.Namespace, config: IngestConfig, engine: OcrEngine | None) -> int:
    started = time.perf_counter()
    located = load_or_locate(args.pdf, config, engine)
    languages = page_ocr_languages(args.pdf, config, engine)
    result = convert_pdf(
        args.pdf,
        located,
        languages,
        config,
        use_cache=not args.no_cache,
        timings={"locate": time.perf_counter() - started},
    )
    print(result.model_dump_json(indent=2) if args.json else _convert_summary(result))
    return EXIT_ALL_FAILED if result.all_failed else 0


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
        lines.append(
            f"  {r.type.value:22} pp. {r.first_page}-{r.last_page}  "
            f"score {r.score:.1f}  rank {r.rank}"
        )
    spans = ", ".join(f"{first}-{last}" for first, last in result.convert_ranges) or "none"
    lines.append(f"convert  {spans}  ({result.candidate_share:.0%} of pages)")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    lines.append("time  " + "  ".join(f"{k} {v:.1f}s" for k, v in result.timings.items()))
    return "\n".join(lines)


def _convert_summary(result: ConvertResult) -> str:
    peak = f"{result.peak_footprint_gb:.2f} GB" if result.peak_footprint_gb is not None else "n/a"
    lines = [f"{result.sha256[:12]}  docling {result.docling_version}  device {result.device}"]
    for r in result.ranges:
        line = (
            f"  pp. {r.first_page}-{r.last_page}  {r.ocr:9} {r.ocr_language or '-':6} "
            f"{r.status:8} tables {r.tables}  {r.seconds:.1f}s"
        )
        lines.append(line + (f"  {', '.join(r.flags)}" if r.flags else ""))
    lines.append(f"pages  {len(result.page_images)} images  peak {peak}")
    lines.append(f"flags  {', '.join(result.flags) or 'none'}")
    lines.append("time  " + "  ".join(f"{k} {v:.1f}s" for k, v in result.timings.items()))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_stage.py packages/ingest/tests/test_cli.py -v`
Expected: PASS (the existing locate CLI tests too).

- [ ] **Step 7: Try it by hand on a golden document**

Run: `uv run fra-ingest convert eval/golden/documents/almarai-2025-en-annualreport.pdf`
Expected: a summary with the balance, income and comprehensive income ranges `ok`, tables at least 1 each, page images for each converted page, and a peak footprint printed. Then `ls var/artifacts/<sha>/pages | head` shows PNGs. Run it again: it returns at once from `convert.json`.

- [ ] **Step 8: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/stage.py packages/ingest/src/fra_ingest/cli.py packages/ingest/tests/test_stage.py packages/ingest/tests/test_cli.py
git commit -m "Add fra-ingest convert, reusing a current locate.json and the page cache"
```

---

### Task 9: A child process per document

**Files:**
- Create: `packages/ingest/src/fra_ingest/child.py`
- Test: `packages/ingest/tests/test_child.py`

**Interfaces:**
- Consumes: CLI exit codes and stderr format (Task 8), `ConvertResult` (Task 3), `sha256_file`.
- Produces: `convert_in_child(pdf: Path, config: IngestConfig, *, config_path: Path | None = None, extra_args: Sequence[str] = (), command: Sequence[str] | None = None) -> ConvertResult`. `command` replaces `[sys.executable, "-m", "fra_ingest.cli"]` (tests only). Adds `timings["child_wall"]`. Raises `IngestError` with `convert_timeout`, `convert_crashed`, `convert_failed`, or the child's own reason on exit 2.

- [ ] **Step 1: Write the failing tests**

```python
"""Running convert in a child process per document (ADR 0007, spec 10)."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fra_ingest.child import convert_in_child
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.pages import sha256_file
from fra_ingest.results import ConvertResult, RangeConversion, RangeStatus


def fake(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def setup(tmp_path: Path, name: str = "doc.pdf") -> tuple[Path, IngestConfig]:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.4 stand-in")
    return pdf, IngestConfig(artifact_root=tmp_path / "artifacts", child_timeout_s=20)


def write_result(config: IngestConfig, pdf: Path, status: RangeStatus) -> ConvertResult:
    sha = sha256_file(pdf)
    result = ConvertResult(
        version="1",
        sha256=sha,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=4, last_page=6, ocr="pdf_aware", ocr_language="en-US", status=status
            )
        ],
        timings={"convert": 1.0},
    )
    out = config.artifact_root / sha
    out.mkdir(parents=True)
    (out / "convert.json").write_text(result.model_dump_json(), encoding="utf-8")
    return result


def test_a_child_that_writes_a_result_returns_it_with_its_wall_time(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    written = write_result(config, pdf, "ok")
    result = convert_in_child(pdf, config, command=fake("import sys; sys.exit(0)"))
    assert result.ranges == written.ranges
    assert result.timings["convert"] == 1.0
    assert result.timings["child_wall"] > 0


def test_the_child_gets_the_pdf_artifacts_config_and_extra_args(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    write_result(config, pdf, "ok")
    record = tmp_path / "argv.txt"
    script = f"import sys; open({str(record)!r}, 'w').write('\\n'.join(sys.argv[1:]))"
    convert_in_child(
        pdf,
        config,
        config_path=tmp_path / "ingest.toml",
        extra_args=["--no-cache"],
        command=fake(script),
    )
    assert record.read_text().splitlines() == [
        "convert",
        str(pdf),
        "--artifacts",
        str(config.artifact_root),
        "--config",
        str(tmp_path / "ingest.toml"),
        "--no-cache",
    ]


def test_every_range_failing_raises_convert_failed(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    write_result(config, pdf, "failed")
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake("import sys; sys.exit(3)"))
    assert caught.value.reason == "convert_failed"
    assert "4-6" in caught.value.detail


def test_a_child_past_its_timeout_is_killed(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    config = config.model_copy(update={"child_timeout_s": 0.5})
    started = time.perf_counter()
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake("import time; time.sleep(30)"))
    assert caught.value.reason == "convert_timeout"
    assert time.perf_counter() - started < 10


def test_a_child_killed_by_a_signal_is_a_crash(tmp_path: Path) -> None:
    pdf, config = setup(tmp_path)
    script = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake(script))
    assert caught.value.reason == "convert_crashed"
    assert caught.value.detail == "signal 9"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("import sys; sys.exit(0)", "exit 0"),
        ("raise ValueError('bad table')", "ValueError: bad table"),
    ],
)
def test_a_child_that_leaves_no_result_is_a_crash(
    tmp_path: Path, script: str, expected: str
) -> None:
    pdf, config = setup(tmp_path)
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake(script))
    assert caught.value.reason == "convert_crashed"
    assert expected in caught.value.detail


@pytest.mark.parametrize("name", ["doc.pdf", "تقرير سنوي 2025.pdf"])
def test_the_childs_own_reason_is_kept(tmp_path: Path, name: str) -> None:
    pdf, config = setup(tmp_path, name)
    script = (
        "import sys; print('warming up', file=sys.stderr); "
        "print(f'{sys.argv[2]}: encrypted_pdf needs a password', file=sys.stderr); sys.exit(2)"
    )
    with pytest.raises(IngestError) as caught:
        convert_in_child(pdf, config, command=fake(script))
    assert caught.value.reason == "encrypted_pdf"


def test_a_missing_pdf_is_refused_before_starting_a_child(tmp_path: Path) -> None:
    _, config = setup(tmp_path)
    with pytest.raises(IngestError) as caught:
        convert_in_child(tmp_path / "missing.pdf", config, command=fake("raise SystemExit(9)"))
    assert caught.value.reason == "unreadable_pdf"


@pytest.mark.slow
@pytest.mark.golden
def test_almarai_converts_in_a_real_child(golden: Callable[[str], Path], tmp_path: Path) -> None:
    config = IngestConfig(artifact_root=tmp_path)
    result = convert_in_child(golden("almarai-2025-en-annualreport.pdf"), config)
    assert result.ranges
    assert all(r.status == "ok" for r in result.ranges)
    assert result.peak_footprint_gb is not None and result.peak_footprint_gb > 0.3
    assert all((tmp_path / result.sha256 / p).is_file() for p in result.page_images.values())
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest packages/ingest/tests/test_child.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fra_ingest.child'`.

- [ ] **Step 3: Implement `child.py`**

```python
"""Run the convert stage in a child process per document (ADR 0007, spec 10).

PyTorch on MPS returns its memory reliably only when the process exits, so each document is
converted by ``fra-ingest convert`` in its own process. A crash, an out-of-memory kill or a
hang ends only the child; this module reports how it ended.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast, get_args

from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError, IngestErrorReason
from fra_ingest.pages import sha256_file
from fra_ingest.results import ConvertResult

_REASONS: tuple[str, ...] = get_args(IngestErrorReason)
_EXIT_ERROR = 2
_EXIT_ALL_FAILED = 3
_TAIL_LINES = 5
_TAIL_CHARS = 500


def convert_in_child(
    pdf: Path,
    config: IngestConfig,
    *,
    config_path: Path | None = None,
    extra_args: Sequence[str] = (),
    command: Sequence[str] | None = None,
) -> ConvertResult:
    if not pdf.is_file():
        raise IngestError("unreadable_pdf", f"{pdf}: not a file")
    sha256 = sha256_file(pdf)
    argv = [
        *(command or [sys.executable, "-m", "fra_ingest.cli"]),
        "convert",
        str(pdf),
        "--artifacts",
        str(config.artifact_root),
    ]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    argv += list(extra_args)
    # The child sees the same packages as this process: uv writes the workspace .pth files
    # hidden on macOS and Python skips hidden .pth files.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    started = time.perf_counter()
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=config.child_timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"{pdf.name}: still running after {config.child_timeout_s:.0f} s"
        raise IngestError("convert_timeout", detail) from exc
    wall = time.perf_counter() - started

    if done.returncode < 0:
        raise IngestError("convert_crashed", f"signal {-done.returncode}")
    if done.returncode == _EXIT_ERROR and (reason := _reason(done.stderr, pdf)) is not None:
        raise IngestError(reason, _tail(done.stderr))
    stored = config.artifact_root / sha256 / "convert.json"
    if done.returncode not in (0, _EXIT_ALL_FAILED) or not stored.is_file():
        raise IngestError("convert_crashed", _tail(done.stderr) or f"exit {done.returncode}")

    result = ConvertResult.model_validate_json(stored.read_text(encoding="utf-8"))
    if done.returncode == _EXIT_ALL_FAILED:
        failed = ", ".join(f"{r.first_page}-{r.last_page}" for r in result.ranges)
        raise IngestError("convert_failed", f"every range failed: {failed}")
    return result.model_copy(update={"timings": {**result.timings, "child_wall": wall}})


def _reason(stderr: str, pdf: Path) -> IngestErrorReason | None:
    """The reason the child printed as ``<pdf>: <reason> <detail>`` on its last line."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    word = lines[-1].removeprefix(f"{pdf}: ").split(" ", 1)[0]
    return cast(IngestErrorReason, word) if word in _REASONS else None


def _tail(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return " | ".join(lines[-_TAIL_LINES:])[-_TAIL_CHARS:]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/ingest/tests/test_child.py -v -m "slow or not slow"`
Expected: PASS. The real-child test takes about a minute (process start, model load, three ranges).

- [ ] **Step 5: Commit**

```bash
make test && make lint && make typecheck
git add packages/ingest/src/fra_ingest/child.py packages/ingest/tests/test_child.py
git commit -m "Run convert in a child process per document and report how the child ended"
```

---

### Task 10: Golden conversion harness and results

**Files:**
- Create: `eval/harness/convert.py`
- Create: `tests/eval/test_convert_harness.py`
- Modify: `Makefile` (target `eval-convert`)
- Modify: `docs/blueprint/10-ingest-convert.md` (Results section)

**Interfaces:**
- Consumes: `convert_in_child` (Task 9), `load_config`, `REPO_ROOT`.
- Produces: `summarize(doc_id: str, result: ConvertResult) -> dict[str, Any]`, `failure_row(doc_id: str, error: IngestError) -> dict[str, Any]`, `verdict(rows: Sequence[Mapping[str, Any]], budget_gb: float) -> list[str]` (empty list means pass), `main(argv: list[str] | None = None) -> int`. Writes `var/eval/convert-golden.json`.

- [ ] **Step 1: Write the failing tests** (`tests/eval/test_convert_harness.py`)

```python
"""The convert harness's arithmetic and verdict (spec 10, Scoring)."""

from __future__ import annotations

from harness.convert import failure_row, summarize, verdict

from fra_ingest.errors import IngestError
from fra_ingest.results import ConvertResult, RangeConversion, RangeStatus


def result(statuses: list[RangeStatus], peak: float = 2.0) -> ConvertResult:
    return ConvertResult(
        version="1",
        sha256="d" * 64,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=i * 10 + 1,
                last_page=i * 10 + 2,
                ocr="pdf_aware",
                ocr_language="en-US",
                status=status,
                tables=2,
            )
            for i, status in enumerate(statuses)
        ],
        peak_footprint_gb=peak,
        timings={"locate": 1.0, "models": 10.0, "convert": 8.0, "write": 0.1, "child_wall": 25.0},
    )


def test_a_summary_counts_pages_and_seconds_per_page() -> None:
    row = summarize("almarai-2025-en", result(["ok", "ok"]))
    assert row["pages"] == 4
    assert row["tables"] == 4
    assert row["seconds_per_page"] == 2.0
    assert row["statuses"] == ["ok", "ok"]
    assert row["startup_s"] == 25.0 - (1.0 + 10.0 + 8.0 + 0.1)


def test_all_ok_within_budget_passes() -> None:
    assert verdict([summarize("a", result(["ok"])), summarize("b", result(["ok", "ok"]))], 3.0) == []


def test_each_kind_of_miss_is_named() -> None:
    rows = [
        summarize("partial", result(["ok", "partial"])),
        summarize("heavy", result(["ok"], peak=3.4)),
        failure_row("crashed", IngestError("convert_crashed", "signal 9")),
    ]
    reasons = verdict(rows, 3.0)
    assert any(r.startswith("partial:") and "partial" in r for r in reasons)
    assert any(r.startswith("heavy:") and "3.40 GB" in r for r in reasons)
    assert any(r.startswith("crashed:") and "convert_crashed" in r for r in reasons)
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/eval/test_convert_harness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'harness.convert'`.

- [ ] **Step 3: Implement `eval/harness/convert.py`**

```python
"""Measure the convert stage over the golden set (spec 10, Scoring).

    uv run python eval/harness/convert.py [--only ID ...] [--no-cache]

Each document converts in its own child process. The report goes to stdout and to
var/eval/convert-golden.json. Exit 0 when every range of every document is ok and every
child stayed within the memory budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from fra_ingest.child import convert_in_child
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.errors import IngestError
from fra_ingest.results import ConvertResult

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
OUT = REPO_ROOT / "var" / "eval"
_CHILD_STAGES = ("locate", "models", "convert", "write")


def summarize(doc_id: str, result: ConvertResult) -> dict[str, Any]:
    pages = sum(r.last_page - r.first_page + 1 for r in result.ranges if r.status != "skipped")
    timings = result.timings
    wall = timings.get("child_wall", 0.0)
    return {
        "id": doc_id,
        "pages": pages,
        "tables": sum(r.tables for r in result.ranges),
        "statuses": [r.status for r in result.ranges],
        "ranges": [f"{r.first_page}-{r.last_page}:{r.ocr}:{r.ocr_language}" for r in result.ranges],
        "seconds_per_page": round(timings.get("convert", 0.0) / pages, 3) if pages else None,
        "models_s": round(timings.get("models", 0.0), 2),
        "startup_s": wall - sum(timings.get(k, 0.0) for k in _CHILD_STAGES) if wall else None,
        "wall_s": round(wall, 2),
        "peak_gb": result.peak_footprint_gb,
        "flags": result.flags + [f for r in result.ranges for f in r.flags],
    }


def failure_row(doc_id: str, error: IngestError) -> dict[str, Any]:
    return {"id": doc_id, "error": f"{error.reason} {error.detail}".strip()}


def verdict(rows: Sequence[Mapping[str, Any]], budget_gb: float) -> list[str]:
    reasons = []
    for row in rows:
        if "error" in row:
            reasons.append(f"{row['id']}: {row['error']}")
            continue
        not_ok = [s for s in row["statuses"] if s != "ok"]
        if not_ok or not row["statuses"]:
            reasons.append(f"{row['id']}: ranges {', '.join(not_ok) or 'none'}")
        peak = row["peak_gb"]
        if peak is None or peak > budget_gb:
            shown = "unknown" if peak is None else f"{peak:.2f} GB"
            reasons.append(f"{row['id']}: peak {shown} over {budget_gb:.1f} GB")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval/harness/convert.py")
    parser.add_argument("--only", action="append", default=[], help="document id; repeatable")
    parser.add_argument("--no-cache", action="store_true", help="convert again")
    args = parser.parse_args(argv)

    config = load_config()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    rows: list[dict[str, Any]] = []
    for entry in documents:
        if args.only and entry["id"] not in args.only:
            continue
        pdf = MANIFEST.parent / entry["file"]
        print(f"{entry['id']} ...", file=sys.stderr, flush=True)
        try:
            result = convert_in_child(
                pdf, config, extra_args=["--no-cache"] if args.no_cache else ()
            )
        except IngestError as exc:
            rows.append(failure_row(entry["id"], exc))
            continue
        rows.append(summarize(entry["id"], result))

    for row in rows:
        if "error" in row:
            print(f"{row['id']:34} ERROR {row['error']}")
            continue
        peak = f"{row['peak_gb']:.2f}" if row["peak_gb"] is not None else "-"
        per_page = row["seconds_per_page"] if row["seconds_per_page"] is not None else "-"
        print(
            f"{row['id']:34} pages {row['pages']:3}  tables {row['tables']:3}  "
            f"{'/'.join(row['statuses']):20} s/page {per_page}  models {row['models_s']}s  "
            f"wall {row['wall_s']}s  peak {peak} GB"
        )
    reasons = verdict(rows, config.memory_budget_gb)
    print("PASS" if not reasons else "FAIL\n  " + "\n  ".join(reasons))

    OUT.mkdir(parents=True, exist_ok=True)
    report = {"rows": rows, "reasons": reasons, "budget_gb": config.memory_budget_gb}
    (OUT / "convert-golden.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/eval/test_convert_harness.py -v`
Expected: PASS.

- [ ] **Step 5: Add the Makefile target**

Add `eval-convert` to the `.PHONY` line, and after `eval-locate`:

```makefile
eval-convert: ## Convert the golden set, a child process per document; records time and peak memory
	@$(MAKE) --no-print-directory unhide-pth
	$(UV) run python eval/harness/convert.py $(if $(ONLY),--only $(ONLY)) $(if $(FRESH),--no-cache)
```

- [ ] **Step 6: Commit the harness**

```bash
make test && make lint && make typecheck
git add eval/harness/convert.py tests/eval/test_convert_harness.py Makefile
git commit -m "Add the golden conversion harness: time per page, peak memory, range statuses"
```

- [ ] **Step 7: Run the whole golden set**

Close other heavy apps first so the memory figures are clean. Run in the background (12 documents, a few minutes each at most):

```bash
make eval-convert FRESH=1 2>&1 | tee var/eval/convert-golden.log
```

Expected: 12 rows, `PASS`. Any `FAIL` line names the document and the reason. For each one, open `var/artifacts/<sha>/convert.json` and the page PNGs, find the cause, and report it to the owner before changing anything: a fix is its own change, agreed first. Never run this against `blind` or `model_test`.

- [ ] **Step 8: Record the results in spec 10**

Append to `docs/blueprint/10-ingest-convert.md`:

```markdown
## Results

Measured <date> on the <branch> branch, `make eval-convert FRESH=1`, other apps closed.

| Document | Pages | Ranges (mode, language) | Tables | s/page | Models | Wall | Peak |
|----------|-------|-------------------------|--------|--------|--------|------|------|
| <one row per golden document, copied from var/eval/convert-golden.json> |

| Measure | Target | Result |
|---------|--------|--------|
| docling contract smoke test | passes against 2.126.0 | <pass or what changed> |
| Golden documents with every range `ok` | 12 of 12 | <n of 12, and each miss with its cause> |
| Peak footprint of the child | 3.0 GB or less | <max, and which document> |
| Seconds per candidate page | recorded | <median digital, median scanned> |
```

Fill every `<...>` with the measured value; `make docs-check` must pass and no placeholder may remain. Then:

```bash
make docs-check
git add docs/blueprint/10-ingest-convert.md
git commit -m "Record the golden conversion results for ingest part 2"
```

- [ ] **Step 9: Final checks before handing back**

```bash
make test && make lint && make typecheck
uv run pytest -m slow packages/ingest/tests/test_docling_contract.py packages/ingest/tests/test_converter.py packages/ingest/tests/test_child.py
git log origin/main..HEAD --format='%an <%ae> | %cn <%ce>' | sort -u
```

Expected: all pass, and a single identity line, `noah-mclain <nadam.30032415@gmail.com> | noah-mclain <nadam.30032415@gmail.com>`.
