# 04. Execution Phases

Every task names its interfaces so it can be implemented and reviewed on its own. Gate values
live in `eval/thresholds.toml`; the numbers below are the initial values. Work packages
prefixed V and T are defined in [05-verification-and-test.md](05-verification-and-test.md).

```text
0.1 -> 0.2 -> 0.3 -> 0.4 -> 0.5 -> G0
G0 -> 1.1 -> 1.2 -> 1.3 -> {1.4, 1.5, 1.6} -> 1.7 -> 1.8 -> 1.9 -> G1
G0 -> 1B.1 -> 1B.2 -> {1B.3, 1B.4} ------------------------------^
G1 -> 2.1 -> 2.2 -> {2.3, 2.4} -> 2.5 -> 2.6 -> 2.7 -> 2.8 -> G2
G2 -> 3.1 -> 3.2 -> 3.3 -> 3.4 -> {3.5, 3.6} -> 3.7 -> 3.8 -> G3
G3 -> Phase 4
```

---

## Phase 0: Foundations

**0.1 Repository bootstrap.**

- `git init` in this directory, and add the directory to the parent repository's `.git/info/exclude`.
- Set up local-only exclusions (section 06, 6.3).
- uv workspace (section 03, 3.3), and pnpm through `corepack enable pnpm`.
- Makefile, pre-commit, empty packages with one passing test each.

Done when: `make test` passes and `git status` shows only project files.

**0.2 Contract v0.**

- `fra_core.schemas` (section 02, 2.4) and `taxonomy/canonical_items.yaml` (2.5).
- `numbers.py`, `periods.py` and `units.py`, with T1 cases.
- ADRs 0001 to 0009.

Interfaces produced:

- `parse_number(text: str, locale: str = "en") -> ParsedNumber(value: Decimal | None, unit: Literal["number", "ratio"], flags: list[str])`
- `parse_period(text: str) -> Period | None`
- `detect_scale(text: str) -> ScaleSignal(scale: int, is_per_share_exempt: bool, evidence: str) | None`
- `load_taxonomy() -> Taxonomy`

**0.3 Inference and ingest smoke (lean session).**

- `scripts/bench_memory.py` loads `mlx-community/Qwen2.5-7B-Instruct-4bit`, runs an
  8,192-token prompt and 256 generated tokens, and records `mx.get_peak_memory()`, process RSS,
  system free memory, prompt tokens/s and generation tokens/s.
- It also converts three sample PDFs (digital, scanned, mixed) with the converter from 1.2
  and records the same measurements.
- Results go to `docs/runbooks/memory-and-disk.md` and replace the estimates in section 01, 1.3.

**0.4 Training smoke.** `make train-smoke` runs 20 iterations on 50 synthetic chat examples
of 2,048 tokens: batch 1, gradient checkpointing, 16 layers. Record peak memory and tokens/s,
and derive the wall-clock estimate for Phase 2 runs.

**0.5 Golden set v0.** Choose 12 public filings covering this trait matrix:

- digital IFRS annual report
- digital US 10-K
- scanned statements
- a "Notes" column
- a restated comparative column
- a statement spanning two pages
- parenthesized negatives
- thousands vs millions
- three period columns
- a landscape page
- an interim report
- a bank (negative control for D7)

Hand-author `expected/*.json` for 5 of them, following `docs/runbooks/golden-set-annotation.md`.

**Gate G0**

- Memory and disk tables measured.
- Contract tagged `contract-v0`.
- 5 expected files signed off (V1 procedure).
- `MACOSX_DEPLOYMENT_TARGET`, exact pins and model revisions recorded.

---

## Phase 1: Extraction, with the analytics engine in parallel

### 1.1 Locate

`locate(pdf: Path) -> LocateResult(pages: list[PageScore], ranges: list[tuple[int, int]])`.

- pypdfium2 text per page, scored on statement titles ("statement of financial position",
  "balance sheet", "profit or loss", "income", "cash flows", "changes in equity") and on
  numeric token density.
- Pages with fewer than 50 text characters are marked `no_text_layer` and scored with a
  low-resolution ocrmac pass.
- Ranges are padded by one page on each side.

Target: statement page recall 100%, with candidate pages at most 15% of document pages (median).

### 1.2 Converter and OCR policy

`ocr_policy(pages: list[PageScore]) -> OcrMode` returns `PDF_AWARE_LAYOUT_REGIONS` when all
candidate pages have a text layer and `FULL_PAGE` otherwise. Mixed documents are converted
range by range.

```python
# fra_ingest/converter.py
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    OcrMacOptions,
    OcrMode,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

from fra_ingest.config import IngestConfig


def build_converter(cfg: IngestConfig, ocr_mode: OcrMode) -> DocumentConverter:
    options = PdfPipelineOptions(
        do_ocr=True,
        ocr_options=OcrMacOptions(lang=cfg.ocr_languages, mode=ocr_mode),
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=cfg.do_cell_matching,
        ),
        generate_page_images=True,
        images_scale=2.0,
        ocr_batch_size=2,
        layout_batch_size=2,
        table_batch_size=2,
        document_timeout=cfg.document_timeout_s,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.MPS),
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
```

`convert(pdf: Path, ranges: list[tuple[int, int]]) -> ConvertResult(doc: DoclingDocument, page_images: dict[int, Path])`
calls `converter.convert(pdf, page_range=r)` per range. It runs only inside `ingest_process.py`.
The first implementation step asserts these imports and field names against the pinned
version with a smoke test.

### 1.3 Table grid

`build_grid(table: TableItem, doc: DoclingDocument) -> Grid`

- Iterates `table.data.table_cells`.
- Each `GridCell` holds `text, row, col, row_span, col_span, bbox, is_column_header, is_row_header, is_row_section, page_no`.
- Every bbox is normalized with `to_top_left_origin(doc.pages[page_no].size.height)` before storage.

### 1.4 Classify

`classify(grid: Grid, context: str) -> Classification(type: StatementType | None, confidence: float, industry_flags: list[str])`.

- Nearby headings plus row-label evidence.
- Notes tables that reuse statement labels are the main negative class.
- Financial-institution markers set `industry_flags` (D7).

### 1.5 Header and periods

`parse_header(grid: Grid) -> HeaderLayout(label_col: int, note_col: int | None, value_cols: dict[int, Period], evidence: list[str])`.

- Handles multi-row headers, "Restated" and "Unaudited" markers, and note-reference columns
  (small integers or a "Note" header).
- EPS rows are exempt from the statement scale.

### 1.6 Hierarchy

`infer_hierarchy(rows: list[GridRow]) -> list[RowNode(depth: int, parent_row: int | None, is_subtotal: bool)]`.

- Label bbox left edges are clustered into depth levels.
- Subtotal cues: "total" and "net" keywords, blank value rows acting as section headers, and
  sum agreement with preceding rows (D6 tolerance).

### 1.7 Continuation and metadata

- `merge_continuations(parts: list[PartialStatement]) -> list[PartialStatement]` matches
  consecutive-page parts by statement type, period header signature and column x-geometry,
  and drops repeated header rows.
- `detect_metadata(page_text: str, caption: str | None, header: HeaderLayout) -> Metadata(scale, currency, entity_name, consolidated, signals, conflict: bool)`.

### 1.8 Structure, checks, CLI, review report

- `structure(doc: DoclingDocument, located: LocateResult) -> StructureResult(statements: list[Statement], checks: list[CheckResult])`.
- `fra-ingest review-report <sha256>` writes an HTML page that draws every extracted cell on
  its page image, colored by parse flags. This is the primary tool for V1 to V6.

### 1.9 Extraction eval and table-model comparison

- `eval/harness/extraction.py` scores against `expected/`.
- Compare TableFormer V1 with cell matching on, V1 with matching off, and `TableStructureV2Options`.
- Run the `GRANITEDOCLING_MLX` VLM pipeline on the 5 hardest pages.
- Record cell accuracy, seconds per page and peak memory. The decision becomes an ADR.
- If one extractor systematically misses a class of layout that another handles, keep both
  rather than picking a winner: the standard pipeline runs first, and the 258M-parameter VLM
  (about 0.5 GB, so it fits beside the other runtimes) re-reads only the statements that fail
  an identity check or a table check. A cell where the two disagree raises a review flag.
  Decide this from the measurements, not in advance.

### 1B.1 Frame, policy, identities (parallel track, needs only G0)

- `to_frame(statements: Sequence[Statement]) -> Frame`, a tuple of typed rows with the columns
  `statement, canonical_id, period, reported, scale, value, provenance` (D19).
- `load_policy(path: Path) -> Policy`.
- `check_identities(frame: Frame) -> list[CheckResult]`; the frame carries the statements, and
  the identity tolerance is D6, so no policy is passed.

### 1B.2 Metric registry

`compute(frame: Frame, policy: Policy) -> list[MetricValue]` covers the metrics of section 02,
2.6 that the converted statements support, with the flags defined by D1 to D5; the others are
listed there.

### 1B.3 Reference implementation

`fra_analytics/reference/naive.py` implements the same formulas in plain Python with `Decimal`
and no pandas. It is used only by T3 and T4.

### 1B.4 Charts

`render(chart_id: str, frame: pd.DataFrame, metrics: list[MetricValue], out_dir: Path) -> ChartArtifact(svg: Path, data: Path)`.

- Agg backend; fixed style module.
- Axis labels always state currency and scale.
- The JSON series is written from the same arrays that were plotted.

### Gate G1

| Measure | Threshold |
|---------|-----------|
| Statement page recall (locate) | 100% |
| Statement classification accuracy | 98% or higher |
| Numeric cell accuracy, digital pages | 99.5% or higher |
| Numeric cell accuracy, scanned pages | 98.0% or higher |
| Period mapping accuracy | 100% |
| Scale and currency accuracy | 100% |
| Sign accuracy | 99.9% or higher |
| Balance sheet identity passes or is flagged with the correct reason | 100% of golden documents |
| Registry vs reference implementation | No relative difference above 1e-9 |
| Ingest process peak memory | 3.5 GB or less |
| Seconds per candidate page | No more than 1.2x the Phase 0 baseline |
| Owner decisions D1 to D7 | Signed |

---

## Phase 2: MLX Pipeline

### 2.1 Runtime

```python
# fra_model/runtime.py (interface)
class ModelRuntime:
    def load(self, model_id: str, adapter: str | None) -> None: ...
    def set_adapter(
        self, adapter: str | None
    ) -> None: ...  # swap task adapter, base stays resident
    def generate_json(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        max_tokens: int,
        temperature: float,
    ) -> BaseModel: ...
    def unload(self) -> None: ...  # del model, mx.clear_cache()
    def peak_memory_gb(self) -> float: ...  # mx.get_peak_memory()
```

`load` applies `mx.set_wired_limit` and `mx.set_cache_limit` from `runtime.toml`. A stub
runtime with the same interface backs all non-MLX tests.

`set_adapter` is how one resident base serves several tasks (ADR 0009): it calls
`mlx_lm.tuner.utils.remove_lora_layers(model)` then `load_adapters(model, path)` for the
adapter named in the registry. That costs tens of MB and no base reload. `normalize` and
`narrate` each name their adapter in `runtime.toml`; a task with no trained adapter passes
`None` and runs on the base model.

### 2.2 Baseline and bake-off

- Zero-shot and few-shot evaluation of each candidate from section 01, 1.6: normalization on
  the validation part of the split (2.4), narration on 30 metric payloads from golden documents.
- A 20-iteration smoke train per candidate.
- Apply the decision rule and record D9.
- Delete the losing candidate's weights.

### 2.3 Normalization dataset

- `sec_fsds.py` streams quarterly Financial Statement Data Sets zips.
- From `pre` it reads `plabel`, `tag` and `stmt`, joined to `sub` for entity and fiscal year,
  and writes parquet.
- `crosswalk_us_gaap.yaml` maps tags to canonical ids. Tags outside the crosswalk become
  `null` targets, which teach abstention.

Example (chat format, `--mask-prompt`):

```json
{"messages": [
  {"role": "system", "content": "Map the line item to a canonical id from the list, or null."},
  {"role": "user", "content": "{\"statement\":\"income\",\"label\":\"Revenue from contracts with customers\",\"parent\":null,\"siblings\":[\"Cost of sales\",\"Gross profit\"],\"sign\":\"+\"}"},
  {"role": "assistant", "content": "{\"canonical_id\":\"revenue\"}"}
]}
```

Deduplicate on `(stmt, normalized label, parent)`, cap examples per entity, and keep the long tail.

### 2.4 Narrative dataset, split and length audit

- Payloads come from Phase 1 outputs on real filings (the golden set is excluded) and from
  `num` rows streamed for selected filings only.
- Targets are 300 analyst-written narratives following the guide in `training/README.md`,
  in the `Narrative` JSON shape with claims.
- The split (`fra_core.split` in `packages/core`) is by issuer and never by document, row or
  fiscal year, as set out under "The split" below.
- `length_audit.py` tokenizes with the target model's tokenizer and chat template, and fails
  on any example above 2,048 tokens.
- Fine-tune narration only if the baseline misses its G2 rows.

**The split (D16 in [06-decisions-and-risks.md](06-decisions-and-risks.md), decided
2026-10-02).** Gate C claims that normalization works on companies the model has not seen. A
company's labels and layout repeat across its years and its language editions
([corpus rule 1](../../eval/corpus/README.md), risk R7), so a score on any issuer present in
training is inflated and cannot support that claim. A valid measurement has to hold out whole
issuers. That is the only valid kind; it does not have to be the `model_test` pool, because the
`model_test` pool is the checkpoint (D13, D17), which comes last, and a holdout (issuers kept
out of training and tuning, scored only to judge a candidate) taken from inside `train` measures
the same property earlier. Until the checkpoint, Gate C is measured on that holdout and marked
provisional. The split is `fra_core.split` in `packages/core`, its override record is
`eval/corpus/holdout_moves.yaml` and the scoring log is `eval/corpus/scoring_log.tsv`;
`make corpus-split` prints the counts below. The dataset builder is week 3 work. Everything below
that says what the builder does is what it will do.

- **Three parts of the `train` pool, by issuer.** *Fit*: what the adapter trains on.
  *Validation*: early stopping, hyperparameters, prompt and template choices, the bake-off of
  2.2, and the quantization level ([14-run-profiles.md](14-run-profiles.md), "Decision for
  week 3"). *Holdout*: never trained on, never tuned on, and scored only to judge a candidate.
  Every scoring is entered in one log: date, what ran, what was looked at. A dry run (below) is
  a scoring and is entered like a model scoring, because each look spends some of the holdout's
  independence. The same parts apply to narration payloads.
- **How an issuer is placed.** A stable hash, in the manner of `pool_for` in
  `training/sources/sec_fsds.py` and `assign_pool` in `scripts/corpus.py`: SHA-256 of
  `holdout:` plus the issuer's key, modulo 100. The key is `cik:<number>` for an issuer that has
  a CIK and `corpus.issuer_key` of its name otherwise, so the SEC data and the PDF corpus agree
  on one issuer only if its corpus documents carry the `cik` field; a US issuer added to `train`
  needs it. Buckets 0 to 14 are the holdout, 15 to 29 the validation part, the rest fit. The
  salt differs from the pools' hash, so the parts are not correlated with the pool buckets. Only
  issuers already in `train` are placed. The shares and the salt are fixed as of this document.
- **Overrides.** An override moves an issuer into fit. It is allowed only before the first dry
  run or scoring, it is recorded with its reason and date beside the split, and it only ever
  moves an issuer out of the holdout. After the first look, an issuer that has to leave the
  holdout (because someone debugged one of its documents) is recorded as a spent look and moved
  to fit, and every earlier holdout score is recomputed without it and shown both ways. Four
  overrides are made now, before any look. Al Dawaa Medical Services lands in the holdout by the
  hash, but its 2023 and 2025 pages drove a locator fix
  ([09-ingest-locate.md](09-ingest-locate.md), "What tuning and review changed"), so it goes to
  fit; the locator was also scored on all the corporate `train` documents, and the table names
  nine `train` issuers whose pages drove a fix (Al Kathiri, Naba Al Saha, Al Dawaa, Herfy, Jazan,
  Orient Takaful, Saudi Energy, United Electronics, Egypt Kuwait), of which the hash puts only Al
  Dawaa in the holdout. Alramz Real Estate lands in the holdout too, and on 2 October, while the
  corpus was extended, its statement pages were rendered and read by eye, to check that an Arabic
  filing with an auditor's letter as an image still had its primary statements as text (the clean
  rule of [eval/corpus/README.md](../../eval/corpus/README.md)); that was not tuning, but the rule
  above is strict about looks, so it goes to fit as well, dated 2 October. Seven other `train`
  issuers had pages rendered the same way for the same check (Anaam International Holding, Batic
  Investment and Logistics, Dar Albalad for Business Solutions, Etihad Etisalat (Mobily),
  L'azurde, Miahona, Nofoth Food Products) and the hash puts none of them in the holdout. Two more
  issuers that the hash puts in the holdout had their statement pages rendered and read the same
  day for the same check: ADES Holding (its Arabic first-quarter file, the located balance sheet
  and profit or loss pages, found to be real text) and Saudi Industrial Investment Group (its
  Arabic first-quarter file, page 4, the balance sheet, found to be an image, and the file was
  then set aside; the issuer stays in `train` with older documents). By the same rule both go to
  fit, dated 2 October. The four overrides are Al Dawaa Medical Services, Alramz Real Estate, ADES
  Holding and Saudi Industrial Investment Group. Four more issuers were opened earlier (Nahdi
  Medical, Aldrees, Catrion Catering, Development Works Food); they are set aside in
  `eval/corpus/deferred.yaml`, in no pool, so the split does not place them, and one that joins
  `train` later falls under this rule.
- **Files.** Fit is `train.jsonl` and validation is `valid.jsonl`, the layout of
  [01-constraints.md](01-constraints.md) 1.5. The holdout is not written as `test.jsonl`, or
  anywhere in the training data directory: whether `mlx_lm lora` reads `test.jsonl` on its own was
  not checked, and a file outside the directory cannot be scored by accident or by the training
  command. The dataset builder will take only rows whose pool is `train` and will fail if a
  `model_test` row reaches its output, so no `model_test` row is written there before the
  checkpoint (`sec_fsds.py` already drops `blind` filers, but it writes `model_test` rows, tagged
  by pool, into the same `labels-<quarter>.jsonl.gz` file as the `train` rows, so the builder's
  filter on pool is the only barrier). Gate C on `model_test` at the checkpoint is scored by the
  eval harness, not from the training directory.
- **Counts, read from `eval/corpus/candidates.yaml` and `eval/corpus/fetched.yaml` on 3 October,
  after the corpus was extended on 2 and 3 October** (the extension is described in the README's
  "Current size"). The `train` pool has 118 issuers and 208 documents (106 corporate issuers, 87
  with an Arabic document, 46 that publish both editions, 14 Egyptian). By the hash the issuers
  fall as 77 fit, 15 validation and 26 holdout (133, 29 and 46 documents). After the four
  overrides above, 81 fit (144 documents), 15 validation (29) and 22 holdout (35 documents, 955
  pages); with only the Al Dawaa override the holdout would have 25 issuers and 42 documents. The
  extension of 3 October put four new issuers in the holdout, all Saudi parallel-market (Nomu)
  companies with half-year 2026 statements (Basma Adeem Medical, National Environmental Recycling,
  Natural Gas Distribution, Professional Medical Expertise; 6 documents), and none of the issuers
  added that day was opened by eye, so no new override is applied; the rule above requires nothing
  else. The holdout that will be used has 21 corporate issuers and 1 negative control (Pioneers
  Holding, a financial company that must be declined), 16 issuers with Arabic documents (18
  documents) and 15 with English ones (17), 9 with both editions, and 3 Egyptian. No holdout
  document is a fully scanned file: 13 have a mixed text layer (some pages have text and some do
  not) and 22 a digital one, and 1 of them has a text layer that is unreadable
  (`text_layer_undecodable`). 30 of the 35 are interim statements and 5 annual. In all, 58 of the
  holdout's 955 pages have no text layer, so a dry run exercises OCR on those pages and on no
  whole scanned filing, and the holdout is light on OCR. By period kind (annual / interim, D18):
  fit 46 / 98 documents, validation 9 / 20, holdout 5 / 30 and `model_test` 26 / 39. Validation
  has 15 issuers, of which 5 are negative controls, so 10 corporate issuers (20 documents of 29);
  9 of the 15 have Arabic documents (8 of the 10 corporate) and 6 both editions; that is enough
  for early stopping and the 50-label quantization check once the SEC rows are in, and not enough
  to choose Arabic prompts or templates. SEC filers have not been counted: no SEC output is on
  disk. `pool_for` already sends 15% of filers to `model_test`, so a 15% holdout of the rest is
  about 13% of all filers (0.85 x 0.15), and the filer count is not known. Fit keeps 31 of the 46
  issuers that publish both editions (the holdout 9, validation 6), so the aligned Arabic and
  English training pairs are still thinner than the Arabic and English counts suggest; a larger
  holdout would thin them further. These counts were derived from the two files by the hash above;
  `make corpus-split` prints them, and a test holds them to this paragraph.
- **Period kind is a stratum, not a pool (D18, decided 2026-10-03, in
  [06-decisions-and-risks.md](06-decisions-and-risks.md)).** Interim and annual statements of one
  company carry the same line labels, so putting them in different pools, or splitting one
  issuer's documents by period, would leak labels between training and scoring (the reason for
  corpus rule 1, risk R7). Period kind is recorded and reported instead, at four points. (1)
  *Data.* Every document records `period: annual | interim` in `candidates.yaml`, and the dataset
  builder (week 3, not built yet) will give every training example built from it that value and
  the length in months of the column it was read from (`Period.months` in
  `fra_core.schemas.statement`; `Period.kind` there is duration or instant, not annual or
  interim). (2) *Weight.* The builder will keep identical (issuer, printed label, canonical item)
  rows once per issuer, so an issuer with several quarterly filings does not count several times
  for the same labels. Interim-only headers (for example "for the three months ended") are period
  headers for the parsers, not line items for the model. (3) *Scoring.* Every extraction and
  mapping score (Gates A, B and E, and every dry run) is reported per period kind beside the
  total, so simpler quarterly filings cannot flatter an annual score or hide an annual failure.
  Gate C is reported per period kind as description only, not as an extra judged stratum: the
  mapping task does not change with the period and the strata are already small. (4) *Analytics
  and narration.* Numbers come only from the analytics engine; interim periods are handled by D4
  and D5. The narration rule is in 2.7 below.
- **Dry runs and the holdout.** The full pipeline is rehearsed on the documents of the corpus
  holdout issuers (the pool rules are in [eval/corpus/README.md](../../eval/corpus/README.md),
  rule 6). A dry run is a look at those issuers: it reports Gate A and Gate B (label mapping),
  and the fixes it prompts are tuning. So a dry-run failure is recorded as a failure class and
  fixed on fit, validation or `dev` documents only; nobody opens a holdout document to debug
  it. The SEC part of the holdout is not touched by dry runs at all: SEC filers have no PDFs in
  the corpus (no `train` document in `candidates.yaml` carries a `cik`) and a dry run is a PDF
  pipeline, so the SEC English stratum keeps its independence for Gate C. The residue: for the
  corpus (regional) issuers, the Gate B and Gate C figures measured before the checkpoint are
  not a first look after the first dry run; `model_test` at the checkpoint is. Splitting the 22
  corpus issuers into a dry-run half and a scoring half was considered and rejected: it would
  leave about 11 issuers on each side.
- **Gate C is never pooled across sources.** Pooled, the interval would be driven by US
  English labels and the gate could pass with the regional issuers contributing almost
  nothing. It is reported, each with its own interval, for three strata: SEC English labels,
  corpus English labels and corpus Arabic labels. The gate's thresholds are judged on a stratum
  only if it is large enough: about 300 critical-item examples for the 99% (below), and enough
  issuers for a bootstrap interval to mean something, which this document takes to be about 30.
  That figure is a judgement, not a measurement. A stratum that is too small is reported as
  description with its counts, marked "not shown", and waits for `model_test`. The corpus
  holdout has 22 issuers in all (15 with English documents, 16 with Arabic ones), and `model_test`
  has 36 (25 with English documents, 25 with Arabic ones, 14 with both editions). Both holdout
  strata are under the judgement of about 30, so the expected outcome (not a certainty, since the
  SEC rows are not counted yet) is still that before the checkpoint Gate C is judged on the SEC
  English stratum only and the two corpus strata are "not shown". What changed is the
  description: 16 Arabic issuers, 9 of them also in English, give a per-issuer picture of Arabic
  that 12 did not, and the paired Arabic and English results of the same issuers can be read side
  by side. It still supports no Arabic claim. At the checkpoint, `model_test` has 36 issuers in
  all, over the judgement, but its language strata (25 and 25) are still under it, so the
  limit is not relaxed: reaching about 30 in one stratum would permit judging that stratum's
  thresholds on its own interval, and none does. The holdout is also mostly digital interim
  statements (22 of 35 digital, none fully scanned, 30 interim), so it says little about
  scans.
- **Uncertainty without retraining.** A bootstrap resamples a stratum's issuers with
  replacement, recomputes the metric each time and reads the spread: at least 1,000 resamples,
  the seed recorded. The statistic is the paired per-issuer difference between the adapter and
  the base model, scored on the same resamples. Both levels, stated once here: the 5-point rule
  of Gate C is met only if the lower end of the two-sided 95% interval of that difference is at
  least 5 points; a point estimate above 5 with an interval reaching below it is reported as
  "met on the point estimate, not shown". With under about 30 issuers in a stratum the
  percentile bootstrap tends to give intervals that are too narrow, and on 21 regional corporate
  issuers (15 for Arabic) it shows direction and rough size and cannot reliably support a lower
  bound of exactly 5 points. Per-issuer results are published beside every interval.
- **"Critical items 99% or higher".** The 99% is judged as a one-sided 95% upper bound on the
  true error rate. With no errors seen in n independent examples, that bound is about 3 / n
  (the rule of three; exact: 1 minus 0.05 to the power 1/n). For under 1% that needs n of about
  300 (3 / 300 = 1.0%; exact 0.994%); one error needs about 475 (exact bound 0.995%). So "99% or
  higher" becomes "no errors in about 300 or more examples, or 1 in about 475". The examples of
  one issuer are not independent, so these are lower bounds on n, and the number of issuers is
  reported with n. A stratum with fewer examples gets "not shown": the point estimate and the
  bound are given and no claim of 99% is made. Whether any stratum has 300 critical-item
  examples is not known: the SEC rows are not counted.
- **What the holdout cannot show, and which number is reported.** It tests unseen issuers, but
  from the same sources and the same collection process as the fit part, so it cannot show how the
  model does on a source or market it has not met. After the checkpoint the `model_test` number is
  always the one reported, with the holdout number shown beside it. They disagree if the interval
  of the difference between them (a bootstrap of each over its own issuers, differenced) excludes
  zero; the report then says so, states the direction, and treats the holdout as having differed.
- **Alternatives considered.** Issuer-grouped k-fold cross-validation (rotate k holdouts) uses
  all the data and gives a variance estimate, but costs k QLoRA runs on this one machine
  (training time for the 7B is not measured) and needs nesting for model selection; it suits
  the non-trained baseline (aliases, fuzzy match), which is cheap to re-score, and not the
  adapter. Scoring on `dev` is the set everything was developed against: the golden set is 12
  documents from 3 issuers, and the `dev` pool in `candidates.yaml` holds only its 3 Almarai
  documents (1 issuer). Scoring on `model_test` now is the most direct, but it is the
  checkpoint, and corpus rule 5 freezes both pools at the first such evaluation. A random split
  by row or by document leaks, by rule 1. The standard design here is the three-part split
  above; the one departure is that the holdout is scored rarely and every look is logged.

### 2.5 QLoRA runs (lean session, `caffeinate -i`)

```yaml
# configs/training/normalize.yaml
model: mlx-community/Qwen2.5-7B-Instruct-4bit
train: true
data: training/data/splits/normalize
fine_tune_type: lora
optimizer: adamw
batch_size: 1
grad_accumulation_steps: 8
grad_checkpoint: true
max_seq_length: 2048
num_layers: 16
iters: 3000                 # set from example count and the Phase 0 tokens/s
learning_rate: 1.0e-4
steps_per_report: 20
steps_per_eval: 200
val_batches: 50
save_every: 200
mask_prompt: true
seed: 17
adapter_path: training/adapters/normalize/run-001
lora_parameters:
  rank: 16
  scale: 20.0
  dropout: 0.05
```

- Sweep at most 8 runs per task: rank {8, 16} x `num_layers` {16, 28} x learning rate {5e-5, 1e-4}.
- Each run writes `manifest.json`: base id and revision, data split hashes, config hash,
  peak memory, wall time, eval scores.

### 2.6 Eval and promotion

- `compare_adapters.py` scores the candidate adapter against the promoted one.
- Promotion requires G2 rows to pass with no regression on any critical item.
- Adapters stay unfused at runtime: swaps need no new weights, and a fused 4-bit copy would
  cost another 4.3 GB of disk.

### 2.7 Structured output and grounding

- `structured.py`: parse, validate, one repair, then fallback.
- `grounding.py` implements section 02, 2.7.
- A narrative names the period it describes, and never compares an interim figure with an annual
  one or annualizes it, unless the metric itself is annualized (D18, with D4 and D5 in
  [06-decisions-and-risks.md](06-decisions-and-risks.md)); `grounding.py` enforces it as step 5
  of section 02, 2.7.
- `templates.py` holds the deterministic sentences.

### 2.8 Worker integration

- The `normalize` stage runs the lexicon first, then the model for residual labels, then
  writes to the cache.
- The `narrate` stage runs the model, then grounding, then the fallback if needed.
- The analysis worker follows the lease and unload rules in section 02, 2.2.

### Gate G2

| Measure | Threshold |
|---------|-----------|
| Normalization accuracy on critical items | 99.0% or higher |
| Normalization macro-F1 | 0.95 or higher, and at least 5 points above baseline |
| Abstention precision (predicted null is correct) | 0.95 or higher |
| Narrative schema validity after at most one repair | 100% |
| Ungrounded numeric mentions (G-N1) | 0 |
| Direction and polarity errors | 0 |
| Template fallback rate | 10% or less |
| Training peak memory | 11.5 GB or less |
| Analysis worker peak memory | 6.0 GB or less |
| Narration p95 per document | Set from Phase 0 tokens/s; initial 60 s |

---

## Phase 3: API and UI Integration

### 3.1 Database and upload

- SQLite in WAL mode; tables `documents`, `jobs`, `job_events`, `heavy_lease`, `corrections`.
- Alembic migrations.
- `POST /documents` with the validation rules in section 02, 2.8.

### 3.2 Jobs and workers

- `jobs.py` handles enqueue and transitions.
- `fra-worker --role ingest|analysis` polls every second.
- `GET /jobs/{id}/events` streams from `job_events`.

### 3.3 Read routes and client

- Analyses, pages, charts and export routes.
- The OpenAPI snapshot is committed to `packages/api-client/openapi.json`.
- `schema.d.ts` is built from the snapshot by `openapi-typescript`.

### 3.4 Web shell and upload

- Router, TanStack Query provider.
- `Dropzone` and `useUpload` show progress.
- `StageTimeline` is driven by `useJobEvents`, which reconnects with backoff.

### 3.5 Statements and source viewer

- `StatementTable` shows periods as columns, depth as indentation, and flags as cell markers.
- `SourceViewer` shows the page PNG with the bbox overlay. It scales PDF points by
  `rendered_width / page_width_pt`, which is independent of `images_scale`.

### 3.6 Metrics, narrative, charts

- `MetricGrid` and `MetricDetail`: formula text, inputs, and each input's provenance link.
- `NarrativePanel` with a `ClaimChip` per claim that opens `MetricDetail`.
- `ChartPanel` shows the SVG, with hover values taken from the chart JSON.

### 3.7 Review and correction loop

- `FlagList` and `MappingEditor`; value and mapping corrections go to `POST /analyses/{id}/corrections`.
- The response carries recomputed metrics with no model call.
- `corrections_export.py` turns corrections into labeled examples for the next normalization run.

### 3.8 End-to-end, accessibility, formatting

- Number display comes only from `lib/format.ts`, using precision supplied by the API.
- Parenthesized negatives are a user setting. Scale and currency are always visible.
- Only logical CSS properties are used, and every string goes through i18n keys.

### 3.9 Demo profile

- `configs/profiles/demo.toml` sets the demo values from section 02, 2.2.
- `make demo`:
  1. Runs `fra-worker preflight --profile demo`. It checks free memory
     (`demo_min_free_percent`, initial 50), free disk (15 GB or more) and that no training
     run is active, and prints the largest processes when a check fails.
  2. Builds the web app and serves it from `fra-api` (no Vite dev server).
  3. Starts both workers, loads docling and the model, and warms them with one conversion
     and one narration on a cached golden document.
- `docs/runbooks/demo.md` holds the pre-demo checklist. It also lists which golden documents
  are already processed (their stored results load instantly) and which will be uploaded live.

### Gate G3

| Measure | Threshold |
|---------|-----------|
| Playwright happy path on 5 golden documents: upload, complete, metrics shown, click value, correct region highlighted | Pass |
| Displayed numbers with a provenance or formula link (DOM audit) | 100% |
| Correction round trip to recomputed metrics | Under 1 s |
| axe serious or critical violations | 0 |
| Contract tests (T7) | Pass |
| Demo rehearsal (V10): 3 documents back to back, all other apps closed | Peak 13.3 GB or less, no swap growth |
| Upload to finished narrative, demo profile, 200-page annual report | Recorded; initial target 3 min, reset from Phase 0 measurements |

---

## Phase 4: Arabic (outline, scoped after G3)

**4.1 OCR spike.** Compare ocrmac `ar-SA`, tesseract `ara`, and the docling VLM pipeline on
10 Arabic statement pages. Measure number accuracy, label character accuracy and column order.

**4.2 Numbers.** Handle:

- Arabic-Indic digits U+0660 to U+0669 and Extended Arabic-Indic digits U+06F0 to U+06F9
- decimal separator U+066B, thousands separator U+066C, percent sign U+066A
- stripping of bidi controls U+200E, U+200F, U+061C, U+202A to U+202E and U+2066 to U+2069

The xfail cases in T1 become required.

**4.3 Headers.**

- Right-to-left column order: label column on the right, period columns mirrored.
- Hijri period headers converted to Gregorian end dates.
- Dual-calendar headers.

**4.4 Labels.** Arabic lexicon entries. Issuers that publish English and Arabic statements
with identical figures give aligned pairs: match rows by value vectors, then pair the Arabic
label with the English row's canonical id.

**4.5 Model.** Use the tokens-per-page measurements from 2.2 to set context budgets. Add
Arabic examples to normalization; add narrative language selection.

**4.6 UI.** `dir="rtl"`, an Arabic message catalog, and a digit-style preference
(Arabic-Indic or Latin) through `Intl.NumberFormat`.

**Gate G4.** The G1 to G3 measures on an Arabic golden set.
