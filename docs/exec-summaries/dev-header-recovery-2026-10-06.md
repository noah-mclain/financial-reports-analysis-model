# DEV header recovery, 6 October 2026

## Change and measured result

Header recovery addresses scanned tables whose printed period labels are missing from the
converted grid. It crops only the missing header region, bounded by table and nearby caption
geometry, then calls the configured OCR engine at the higher-resolution
`header_ocr_scale=6`. It accepts only
unambiguous observed date-label boxes aligned to amount columns and checked against date
context. Recovered headers and context are persisted in `TableDecision`; source amount
cells are unchanged. Unsafe or ambiguous geometry and OCR/recovery failures are flagged
for review. The `STRUCTURE_VERSION=18` settings hash includes the recovery scale and OCR
engine/language configuration; `CONVERT_VERSION=2` and `PAGES_STAGE_VERSION=5` are unchanged.

The golden DEV gate, structure, and mapping commands loaded the configured ingest settings but
passed `None` as the OCR engine. They now construct the engine once from that configuration and
reuse it for each document. OCR-disabled configurations still pass `None`; engine construction
errors are not caught or replaced with a fallback. Mapping's fit path was already wired this way.

On `juhayna-2024-ar-consolidated` (source PDF SHA256
`2b9e3bc80243dd8cbc76d37271deda4e38acf23a65b993884df884dd4eb1987d`), the native target run
completed in 1.369 seconds at 0.157 GiB peak with four selective OCR calls. Production recovery
bound 2023 and 2024 on page 6 and retained 20 income lines. Page 5 remains unresolved with
`header_recovery_period_uncertain`. The recorded OCR observations can bind both pages in the
source-preservation regression, but the native production run recovered only page 6.
Comprehensive income was already present and was not header-recovered; no period inheritance is
claimed. Source financial cells remained unchanged in the recorded regression. This is a partial
repair, not a claim that both production pages are fixed.

The initial 12-document annual DEV diagnostic used the existing gate and structure functions
with a configured Vision engine explicitly injected by a temporary coordinator adapter. The
normal commands have now reproduced that output: `make eval-structure`, `make eval-gates`, and
`make eval-mapping` each exited 0, and the resulting `structure-golden.json` and `gates-dev.json`
are byte-for-byte equal to the earlier diagnostic JSON files. The measured output compared with
the preserved baseline is:

| Measure | Baseline | Candidate (normal commands) |
|---|---:|---:|
| Checked digital figures | 138/138 | 138/138 |
| Checked scanned figures | 218/254 | 218/254 |
| Scale/currency statements correct or flagged | 30/36 | 31/36 |
| Critical mapped slots | 53/144 | 56/144 |
| Silent absent critical slots | 54 | 55 |
| Critical statements not found | 28 | 24 |
| Mapped slots with a verdict | 20/53 | 20/56 |

There were no interim or unknown-period documents. Identity counts were unchanged: 4 okay, 1
accepted by rule, 4 skipped, and 3 without a balance sheet. No disagreement was found among the
20 mapped slots with a verdict; the other 36 mapped slots had no verdict. Gates A and B still
fail. These provisional DEV results do not establish mapping accuracy or fix the remaining
scanned-cell, identity, unit, missing-statement, classification, or mapping gaps.

## Configuration and evidence

The measured native config used `ocrmac`/Vision, `header_ocr_scale=6`, `STRUCTURE_VERSION=18`,
`CONVERT_VERSION=2`, and `PAGES_STAGE_VERSION=5`. The earlier diagnostic gate and structure runs
took 0.444 and 11.684 seconds respectively, with a 0.188 GiB peak. These remain diagnostic
timings, not normal-command cold timings or an OCR-engine comparison. The earlier raw host
experiment's 0.938-second timing predates recovery and is not a recovery timing.

Target artifacts: `/private/tmp/dev-header-recovery-coordinator/target-validation/summary.json`
and `result.json`. Full DEV diagnostic: `/private/tmp/dev-header-recovery-coordinator/dev-measurement/`
(`execution.json`, `gates-dev.json`, `structure-golden.json`, and baseline JSON files) and
`/private/tmp/dev-header-recovery-coordinator/dev-measurement.log`. The unchanged historical
report is `dev-period-header-repair-2026-10-06.md`; its baseline remains the comparison source.
Earlier implementation checks and source-cell preservation evidence are under
`/private/tmp/dev-header-recovery-evidence/phase2/`.

The normal-command reproduction is recorded in
`/private/tmp/dev-header-recovery-coordinator/normal-dev/`; its JSON outputs match the earlier
diagnostic outputs exactly. Gates A and B still fail. No held-out examples, `model_test`, or
`blind` data were read or scored.

## Checks and status

| Check | Result |
|---|---|
| Focused new wiring regressions before implementation | Failed at missing engine wiring (expected) |
| `UV_OFFLINE=1 .venv/bin/python -m pytest tests/eval/test_gates.py tests/eval/test_structure_harness.py tests/eval/test_mapping_harness.py -q` | Exit 0; 62 passed |
| `UV_OFFLINE=1 UV_CACHE_DIR=/private/tmp/dev-header-recovery-evidence/uv-cache make lint` | Exit 0 |
| `UV_OFFLINE=1 UV_CACHE_DIR=/private/tmp/dev-header-recovery-evidence/uv-cache make typecheck` | Exit 0; all configured mypy targets passed |
| `make docs-check` | Exit 0 |
| `make corpus-check` from prior recovery work | Exit 0; see phase-two evidence |
| Earlier `make test` attempt in the sandbox | Exit 2; 1,802 passed, 11 localhost socket permission failures, 17 deselected (historical; superseded by the successful full-host run below) |
| `make test` pre-safety-revision full-host run | Exit 0; 1,818 passed, 17 deselected, one existing Starlette/httpx deprecation warning; 95.04 seconds |
| `make lint` full-host run | Exit 0 |
| `make typecheck` full-host run | Exit 0 |
| `make docs-check` full-host run | Exit 0 |
| `make corpus-check` full-host run | Exit 0 |
| `make corpus-split` full-host run | Exit 0 |
| `make ci-workflows-check` full-host run | Exit 0 |

For recovered headers, the period-safety revision rejects invalid caption dates for every
comparative year and retains
supported caption durations even without a caption date. Complete printed column dates remain
unchanged; unsupported or unspecified lengths stay unbound. Synthetic regressions cover English
and Arabic captions, leap dates, full-date/year-only conflicts, and split date/duration lines.
This is a semantic safety fix, not a new data-accuracy measurement. Evidence is under
`/private/tmp/dev-header-recovery-evidence/phase4/`: `fail-before.log` records the expected
pre-fix failures, and `focused.log` records the revised recovery, header, cache, stage and
harness checks. The focused suite passed 212 tests; lint, typecheck and docs-check exited 0.
The earlier host checks and native DEV measurements above predate this safety revision.
Full golden validation subsequently caught five regressions because the strict checks had also
been applied to ordinary headers. The original ordinary-header/date-hint parsing was restored;
the stricter validation applies only to recovered headers.

### Final source validation

After that isolation, the normal native `make eval-structure`, `make eval-gates` and
`make eval-mapping` commands all exited 0. Both DEV measurement JSON reports match the
pre-safety-revision reports exactly as parsed JSON; the partial improvements and remaining
Gate A/B failures reported above are unchanged. The final host `make test` exited 0 with
**1,858 passed, 17 deselected and one existing warning** in 96.12 seconds. `make lint`,
`make typecheck`, `make docs-check`, `make corpus-check`, `make corpus-split` and
`make ci-workflows-check` each exited 0. Command logs, exit records and measurement snapshots
are under `/private/tmp/dev-header-recovery-coordinator/final-isolated/`.

A subsequent focused safety review caught a terminal-punctuation bypass in yearless caption
dates. The date-span matcher now excludes terminal nonword punctuation while retaining the
printed day and month for calendar validation. English and Arabic negative regressions cover
invalid days, unknown months, leap-year comparisons, colons, ellipses and repeated punctuation;
positive regressions preserve valid comparative dates and original financial cells. Focused
checks passed 179 tests, including all 13 structure goldens. After this microfix, all three normal
native DEV commands and all seven host checks again exited 0. The full suite passed
**1,914 tests, with 17 deselected and one existing warning**, in 99.22 seconds. Both DEV JSON
reports remain equal to the previous measurement snapshots as parsed JSON. Final microfix
check evidence is under `/private/tmp/dev-header-recovery-coordinator/final-punctuation/`;
fail-before/pass-after regression evidence is under
`/private/tmp/dev-header-punctuation-evidence/`.

This report records an implemented, partial repair. Independent review remains a finalization
gate before any commit; approval is not claimed here. Publication is not authorized by this
report. Measurements were collected on `dev-gates-repair`, from the candidate based on
`cbffce8c87e4497209bbb22a40d9fe3f3963e05e`.
