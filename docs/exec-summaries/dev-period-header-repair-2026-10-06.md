# Dev period header investigation, 6 October 2026

The bounded header repair is blocked: the current converted grids contain no year
labels to bind. No extraction behavior or cache version changed. This investigation
does not establish a Gate A or Gate B improvement.

Checkout: `dev-gates-repair` at `cbffce8c87e4497209bbb22a40d9fe3f3963e05e`.
The stacked dependency and intended PR base remain `week2-on-main`.

## Observed input and reproduction

Only the development document `juhayna-2024-ar-consolidated` was inspected:
SHA256 `2b9e3bc80243dd8cbc76d37271deda4e38acf23a65b993884df884dd4eb1987d`.
Its cached conversion is successful, with full-page Arabic OCR and tables in
`docling/p4-9.json`. The existing structure result uses version 17 and rejects
both tables below with `no_period_header`.

The reproduction loads that JSON, builds the actual grids, applies the existing
text repairs, obtains the same-page headings, and calls the existing header parser.
It performs no conversion or OCR.

| Page / table | Grid | Observed header rows | Value columns | Date hint |
|---|---|---|---|---|
| 5 / `#/tables/0` | 40 rows, 4 columns | Row 0: empty, empty, `إيضاح رقم`, `حليه مصري` | 0 and 1 both unbound | None |
| 6 / `#/tables/1` | 22 rows, 4 columns | Row 0: `إيضاح رقم` spans all four columns; row 1 has `جنيه مصري` in columns 0 and 1 | 0 and 1 both unbound | None |

Neither table contains a standalone `٢٠٢٣` or `٢٠٢٤` cell anywhere. Each selected
heading ends with `٣١ ديسعبر` and contains no year. The parser identifies the
label column as 3 and the note column as 2 on both pages, then retains
`period_unbound:0` and `period_unbound:1`.

The cached page 5 image visibly prints two year labels over the amount columns;
they were omitted from the converted cells and from the locator's OCR header.
The locator's page 6 OCR header does contain separate `٢٠٢٣` and `٢٠٢٤` lines.
However, the page record stores header/body text without token boxes or column
relationships. Reading their order cannot establish which value column each
belongs to. The docling table's merged note header also provides no missing date
evidence.

## Scope decision

The defect lies upstream of the allowed header binding boundary. Joining existing
header cells cannot recover an omitted year. Document metadata, financial amounts
beginning with year-like digits, OCR line order, and a later page's periods do not
authorize binding these independent statements. The observed grouped amount
`٢٠٧٧ ٦٨٥ ١٨٢` remains a financial value, not a date.

The next bounded task should inspect conversion/layout recovery for these two dev
pages, preserving the printed year tokens and their page boxes as column header
cells. It needs an explicit scope allowing the conversion or OCR/table-grid
boundary. Its regression should demonstrate the missing cells before recovery,
their geometric association afterward, and unchanged amount-cell provenance. If
fresh OCR is needed, that execution must be separately scoped; none ran here.

No regression claiming a header-only fix was added, and `STRUCTURE_VERSION` remains
17. Independent review of any future implementation remains required.

## Evidence and limits

Private reproduction and input hashes:
`/private/tmp/dev-gates-repair-evidence/inspect_headers.py`,
`header-inspection.json`, and `header-inspection.log` in the same directory.
The JSON preserves the actual header cells, boxes, headings, parsed layouts, and
locator page fields. Source artifacts and the historical baseline were not edited.

The previously reproduced baseline remains historical: 12 annual dev documents,
no interim or unknown documents; digital figures 138/138, scanned 218/254;
identities 4 passing, 1 accepted by rule, 4 skipped, 3 missing balance sheets;
scale/currency 30/36; critical slots 53 mapped, 9 ambiguous, 54 silently absent,
28 missing statements, out of 144. These figures come from
`/private/tmp/dev-gates-repair-evidence/baseline-gates.log`, not a new candidate run.
Gates A and B remain unmet. Numeric accuracy, units, mapping, and classification
were not modified or remeasured. No holdout, `model_test`, or `blind` examples were
opened or scored.

## Checks

Commands ran in the assigned checkout with `UV_OFFLINE=1` and
`UV_CACHE_DIR=/private/tmp/dev-gates-repair-uv-cache` for uv/make verification.

| Command | Exit / result | Private evidence file |
|---|---|---|
| `.venv/bin/python /private/tmp/dev-gates-repair-evidence/inspect_headers.py` | 0; reproduces both unbound layouts | `header-inspection.log` |
| `uv run pytest packages/ingest/tests/test_header.py packages/ingest/tests/test_structure_convert.py` | 0; 34 passed | `focused-tests.log` |
| `make test` | 2; pytest 1: 1,753 passed, 11 failed, 17 deselected | `make-test.log` |
| `make lint`, `make typecheck`, `make docs-check`, `make corpus-check`, `make corpus-split` | Each completed successfully before the final workflow target in the combined make invocation | `required-checks.log` |
| `make ci-workflows-check` | Combined make exit 2; target failed with curl exit 6 | `required-checks.log` |
| `make docs-check` after writing this report | 0 | `final-docs-check.log` |
| `git diff --check` and inspected input hash comparison | 0; all five inspected artifacts unchanged | Verification command output |

All 11 test failures are in `apps/api/tests/test_healthcheck.py`: localhost socket
binding raises `PermissionError: [Errno 1] Operation not permitted`, before the
healthcheck assertion runs. The workflow check lacks installed actionlint and its
download fails with `Could not resolve host: github.com`. Neither check was retried
with exclusions or relaxed permissions. The evidence files above live under
`/private/tmp/dev-gates-repair-evidence/`.
