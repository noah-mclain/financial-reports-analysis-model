# 05. Verification and Test Work Packages

Two independent tracks check the build tracks.

- **Verification** owns layout fidelity: whether what we extracted is what the page says,
  in the right place, period and scale.
- **Test** owns calculation accuracy and software correctness: whether numbers computed
  from correct inputs are right, and whether the system behaves as specified.

Neither track changes production code. Each files findings against the owning task.

## 5.1 Handoff protocol

- **Inputs per run:** git sha, golden set version (`eval/golden/manifest.yaml` hash), config
  hashes, model and adapter ids.
- **Output:** `eval/reports/<wp-id>/<date>-<sha>.md` plus `.json`, with results against
  `eval/thresholds.toml` and a findings list.
- **Finding format:** severity (blocking, major, minor); location (document id, page and bbox,
  or `file:line`); expected vs actual; a one-line reproduction command.
- **Sign-off:** every gate (G0 to G4) needs a Verification report with no open blocking findings.

## 5.2 Cadence

| When | Work packages |
|------|---------------|
| Every commit (fast subset) | T1, T2 (100 examples), T5, T6, T7, T8, T9, T10 |
| Nightly | T3, T4 (5,000 examples), V1 to V8 on the golden set, T11 |
| Each phase gate | Every V package relevant to the phase, T12 from Phase 3 |
| Before each tagged release | V11 |

---

## 5.3 Verification track (layout and fidelity)

**V1. Provenance re-read** (Phase 1, nightly)

- Method: for every extracted numeric cell, independently re-read the source region.
  - Digital pages: convert the bbox back to PDF bottom-left coordinates and read
    `PdfTextPage.get_text_bounded(left, bottom, right, top)` from pypdfium2.
  - Scanned pages: crop the page image and OCR the crop with ocrmac.
  - Parse the result with `parse_number` and compare it to `Cell.reported`.
- Pass: agreement of 99.9% or higher on digital pages and 99.0% or higher on scanned pages.
- Output: every disagreement shown as a crop in an HTML report.
- Also owns sign-off of the hand-authored `expected/*.json` files: two readings per file,
  with disagreements resolved against the page.

**V2. Column and period alignment** (Phase 1)

- Method:
  - The x-center of each value cell must fall inside its header column's range, taken from
    `TableData.get_column_bounding_boxes()`.
  - Note-reference columns must never map to a period.
  - Fault injection: shift one column of an expected file by one position; the check must
    report `column_misaligned`.
- Pass: 100% on the golden set, and the injected fault is caught.

**V3. Hierarchy and subtotals** (Phase 1)

- Method: every `is_subtotal` row must equal the sum of its children within D6 tolerance for
  each period. Review depth assignment on review reports for 5 documents.
- Pass: every subtotal either balances or carries a flag with the correct reason code.

**V4. Scale and currency** (Phase 1)

- Method: compare detected scale and currency to expected for each document.
- Magnitude cross-check: `total_assets` must stay within a 10x band across periods and between
  primary statements.
- Conflicting signals must block.
- Include the "in millions, except per share data" case: EPS must not be scaled.
- Pass: 100%.

**V5. Cross-statement ties** (Phase 1)

- Method:
  - The first line of an indirect cash flow statement ties to income-statement
    `net_income` or `profit_before_tax`, whichever the statement starts from.
  - Closing cash in the cash flow statement ties to balance-sheet `cash_and_equivalents`
    (bank overdrafts get a reconciliation flag).
  - Closing total equity in the equity statement ties to balance-sheet `total_equity`.
- Pass: every tie passes, or is flagged with a specific reason code.

**V6. Page continuation** (Phase 1)

- Method: on documents whose statements span pages, compare row counts with expected, check
  that no header row appears as a line item, and check that no row is lost at the page boundary.
- Pass: exact row match.

**V7. Classification and scope** (Phase 1)

- Method: build a confusion matrix across statement types, with notes tables as the negative
  class (a trade receivables note must not become a balance sheet). The bank document must
  route to `needs_review` with `unsupported_industry_template`.
- Pass: G1 classification threshold and correct routing.

**V8. OCR policy** (Phase 1)

- Method: pages with a text layer never get full-page OCR, scanned pages always do, and mixed
  documents are handled per range. Record seconds per page by mode.
- Pass: 100% correct mode selection.

**V9. Narrative grounding audit** (Phase 2)

- Method:
  - Re-check grounding with a script that shares no code with `fra_model.grounding`: extract
    every digit sequence from narrative text and match it to `metrics.json`.
  - Manually read 20 narratives for grounded but misleading statements: wrong period
    attribution, a change described against the wrong base.
- Pass: 0 ungrounded mentions and 0 period misattributions.

**V10. Runtime budgets** (Phases 0 to 3)

- Method:
  - Measure peak memory for the ingest process, the analysis worker and the training smoke
    run, and for a full session in each profile (development and demo).
  - Demo rehearsal: with all other apps closed, run `make demo` and upload 3 golden documents
    back to back through the UI. Record peak memory, swap used before and after
    (`sysctl vm.swapusage`), and wall time per document.
  - Fault injection: enqueue an ingest job while `narrate` runs; the two heavy stages must
    never compute at the same time (lease log timestamps).
  - Run `make disk-report` against section 01, 1.4.
- Pass: all budgets in section 01 met, and no swap growth during the demo rehearsal.

**V11. Repository hygiene audit** (every release)

- Method:
  - Confirm `.gitignore` lists project paths only.
  - Confirm `git ls-files` contains no editor or cache directories, model weights or training data.
- Pass: zero findings.

**V12. UI provenance click-through** (Phase 3)

- Method: for 50 randomly chosen displayed values, click through to `SourceViewer` and compare
  the highlighted rectangle with the pypdfium2 text box of the printed number.
- Pass: intersection over union of 0.5 or higher for every sample.

---

## 5.4 Test track (calculation accuracy and correctness)

**T1. Parsing tables** (`packages/core/tests`)

| Input | Expected |
|-------|----------|
| `1,234` | 1234 |
| `(1,234)` | -1234 |
| `1,234-` | -1234 |
| `−1,234` (U+2212) | -1234 |
| `–`, `—`, `-` alone | 0, flag `dash_as_zero` |
| empty | None |
| `nil` | 0 |
| `n/a` | None |
| `1 234` (U+202F thin space) | 1234 |
| `12.5%` | 0.125, unit ratio |
| `(12.5)%` | -0.125 |
| `1,234¹`, `1,234 (a)` | 1234, flag `footnote_marker` |
| `1.234,56` with locale `en` | None, flag `ambiguous_separator` |
| `١٬٢٣٤` | 1234 (xfail until Phase 4) |
| `(١٢٫٥)` | -12.5 (xfail until Phase 4) |

| Period text | Expected |
|-------------|----------|
| `2024` | FY2024, duration, 12 months |
| `31 December 2024` | instant, 2024-12-31 |
| `Year ended 31 Dec 2024` | duration, 12 months, end 2024-12-31 |
| `2023 (Restated)` | FY2023, `restated=True` |
| `Six months ended 30 June 2025` | duration, 6 months |

| Scale text | Expected |
|------------|----------|
| `SAR '000` | 1,000, SAR |
| `(in thousands of U.S. dollars)` | 1,000, USD |
| `USD millions` | 1,000,000, USD |
| `€m` | 1,000,000, EUR |
| `in millions, except per share data` | 1,000,000, per-share exempt |

**T2. Metric invariants (property-based)** (`packages/analytics/tests`)

```python
from hypothesis import given, strategies as st

from fra_analytics.metrics.registry import compute
from tests.strategies import consistent_statements  # builds frames where A = L + E holds


@given(frame=consistent_statements(), k=st.sampled_from([1_000, 1_000_000]))
def test_ratios_are_scale_invariant(frame, k, policy):
    base = {(m.metric_id, m.period_key): m for m in compute(frame, policy)}
    scaled = {
        (m.metric_id, m.period_key): m for m in compute(frame.assign(value=frame.value * k), policy)
    }
    for key, metric in base.items():
        if metric.unit in ("ratio", "times", "days") and metric.value is not None:
            assert abs(scaled[key].value - metric.value) <= 1e-9 * max(1.0, abs(metric.value))
```

Further properties:

- Currency metrics scale linearly with k.
- Growth between identical values is 0.
- Forward and backward growth satisfy `(1 + g1)(1 + g2) = 1`.
- `first x (1 + cagr)^years = last`, within 1e-9.
- The average of equal balances equals the balance.
- Row order in the frame does not change results.
- A missing input yields `None` with flag `missing_input:<id>`, never 0.
- Zero or negative denominators yield the D1 behaviour, never `inf` or `NaN`.

**T3. Golden metric fixtures** (nightly)

- 5 companies x 3 years; expected metrics computed independently in a spreadsheet with
  visible formulas, exported to `eval/golden/expected/<doc>.metrics.csv`.
- Inputs come from the expected statements JSON, so extraction errors cannot mask
  calculation errors.
- Pass: relative difference 1e-9 or less, and identical flags.

**T4. Differential testing** (nightly)

- `reference/naive.py` vs the registry on 5,000 generated statement sets, reporting the
  minimal failing example.
- Pass: relative difference 1e-9 or less on every metric.

**T5. Identity checks and fault injection**

- D6 edges: seven rounded addends off by 3 units pass; off by 4 units fail (tolerance 3.5).
- Injected faults must each fail with the named reason code:
  - flip one sign: `identity_sign`
  - drop one row: `identity_missing_component`
  - swap two period columns: `identity_period_mismatch`

**T6. Charts**

- Assert plotted data, not pixels: `Line2D.get_ydata()` and bar heights equal frame values.
- Axis labels contain currency and scale.
- The chart JSON equals the plotted arrays.
- `pytest-mpl` baselines for 6 charts with tolerance.
- A worker import check fails if the backend is not Agg.

**T7. API and contract**

- OpenAPI snapshot diff; `tsc --noEmit` on the generated client.
- schemathesis over every route (no 5xx responses).
- Upload rules: non-PDF, over 50 MB, over 400 pages, duplicate sha256 returning the existing document.
- `import-linter` boundaries from section 03, 3.2.

**T8. Model wrapper and grounding** (stub runtime, no MLX)

| Case | Expected |
|------|----------|
| Malformed JSON, repair succeeds | Parsed `Narrative` |
| Malformed twice | `grounding="fallback_template"` |
| "Net margin rose to 12.5%", metric 0.1249, growth positive | Pass |
| Same sentence, metric 0.1261 | Fail (renders as 12.6%) |
| "Revenue fell 3%", growth +0.03 | Fail (direction) |
| "DSO improved", DSO increased | Fail (polarity lower) |
| "In 2024 ...", period key FY2024 present | Year ignored |
| Number with no claim | Fail |
| Adapter swap: normalize, then narrate, then normalize | Each output matches loading that adapter fresh, and the stub records exactly one base load |

**T9. Workers and jobs**

- Stage state machine transitions.
- A re-run skips existing artifacts; a stage version bump invalidates only downstream stages.
- Two workers contending: only one heavy holder.
- Stale heartbeat takeover after 60 s (fake clock).
- `memory_guard` requeues the job.
- An ingest process crash marks the job `failed` with the stage error and releases the lease.
- Profile behaviour: in `dev` the model unloads after 120 s idle (fake clock) and as soon as an
  ingest job waits; in `demo` it stays loaded and only compute is serialized.
- `fra-worker preflight` exits non-zero when a profile threshold fails, and names the check.

**T10. Training data pipeline**

- Same seed gives the same split hashes.
- Zero entity overlap across train, valid and test.
- Every JSONL line validates as mlx-lm chat format.
- `length_audit` fails on a 2,049-token example using the target tokenizer and chat template.
- Crosswalk coverage and label distribution reports are produced.

**T11. Regression gates**

- `make eval` compares against `eval/thresholds.toml` and the last promoted results.
- Adapter promotion is blocked on any critical-item regression.
- An adapter whose manifest names a different base revision or `num_layers` than the resident
  base is rejected: every task adapter must swap onto one loaded base (ADR 0009).
- Dependency upgrades run the full eval before merge.

**T12. Frontend** (Phase 3)

- Vitest for `format.ts`: parenthesized negatives setting, scale suffix, API-supplied
  precision; an `ar-SA` snapshot as xfail until Phase 4.
- Upload state machine; `useJobEvents` reconnect with backoff.
- `SourceViewer` scaling at arbitrary rendered widths.
- Playwright flows from G3, plus axe checks.
