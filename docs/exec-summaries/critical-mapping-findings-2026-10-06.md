# Critical mapping findings, 2026-10-06

Branch: `critical-mapping-findings`, stacked on `dev-gates-repair` (base
`bd7dc10c81a23f576a23683c87400d9867397c2a`).

Statements now carry immutable, named `critical_item_unmapped` findings covering only their
observed periods. Mapping regenerates findings after duplicate settlement, and review artifacts
hold the statement with each missing item's name. A duplicate statement keeps its single
`duplicate_statement` reason; its findings stay on the statement. The harness counts validated
findings as `explicitly_flagged`, after mapped and named ambiguous slots. Missing statements,
disagreements, and unreadable documents remain blocking; findings add neither rows nor accuracy
verdicts.

The structure cache version is now 19; conversion and page versions remain 2 and 5.

## Measured on `dev`

The native `eval-structure`, `eval-gates` and `eval-mapping` runs over the 12 golden documents
gave 144 critical slots: 56 mapped, 9 ambiguous, 55 explicitly flagged, 0 silently unmapped and
24 in statements not found. Before this change the same 55 slots were silently unmapped. Mapping
verdicts are unchanged: 1 verified, 19 lexicon-consistent and 36 without a verdict, with no
disagreement among the 20 tested. Gate A figures are unchanged (138/138 digital, 218/254
scanned, identity 4 ok, 1 accepted by rule, 4 skipped, 3 without a balance sheet, units 31/36).
Gates A and B still fail. This is visibility of missing items, not an accuracy gain.

## What to expect in later runs

Every balance sheet or income statement that lacks one of its critical items is now held as
`needs_review` with the item named, including statements where the item is legitimately absent
(for example `net_income_attributable_parent` when there is no minority interest). The count of
statements held for review in the next dry run will therefore rise; that rise comes from this
change and is not a regression in extraction.

## Checks

`make test` (fast suite), `make lint`, `make typecheck`, `make docs-check` and `make corpus-check`
pass on Linux. The golden tests that need the local conversion caches under `var/artifacts`
were skipped there and still need a run on the Mac. No OCR, corpus scoring, held-out access,
downloads, training, label changes, or gate threshold changes were made.
