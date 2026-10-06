# Week 2 candidate reconciliation — 6 October 2026

## Disposition

Accept the cache repair and the measured reconciliation as engineering work. This is not
acceptance of Gates A or B: both remain **NOT MET**. The final numerical checkpoint and any
publication remain deferred. This note records current fresh golden/dev measurements beside
the previously reported historical counts; it does not rewrite historical results.

## Current measurements and historical comparison

The fresh golden set covers 12 annual documents, 0 interim documents and 0 with unknown period
(12 total). Interim and unknown are empty strata, so no claim is made for them.

| Measure | Historical report | Fresh result, 6 October |
|---|---:|---:|
| Digital numeric cells | 138/138 | 138/138 |
| Scanned numeric cells | 218/254 (85.83%) | 218/254 (85.83%) |
| Gated bilingual pairs identical | 2/2 | 2/2 |
| Identity status | No comparable historical count retained in the available aggregate | 5/12 hold or are explained; 4 skipped; 3 have no balance sheet |
| Scale and currency | 29/36 | 30/36 |
| Critical slots mapped | 52/144 | 53/144 |
| Critical slots flagged ambiguous | 6/144 | 9/144 |
| Critical slots silently absent | 50/144 | 54/144 |
| Critical slots with statement missing | 36/144 | 28/144 |

The fresh structure output includes a balance sheet not present in the historical cached result.
The identity count therefore is reported from the fresh aggregate only; it is not described as
an unchanged or directly comparable count. Digital and scanned cell shares and bilingual pair
counts are extraction evidence, not full gate passes.

Gate A is **NOT MET**: scanned cell accuracy is below the 98% threshold; identity results
include skipped checks and documents without a balance sheet; scale/currency is correct for only
30 of 36 expected statements. Gate B is **NOT MET**: 54 critical slots are silently absent, in
addition to slots whose statements were not found. A successful report command does not change
these dispositions.

The 4 October first train-holdout dry run covered 5 annual and 30 interim documents (35 total).
Its aggregate artifact is retained as the first-look record. A first-versus-second-run
comparison belongs in the closing note, after the one authorized later clean committed run; no
second run is recorded here. `model_test` has not been measured (D13), and `blind` has not been
run (D17).

## Scope and remaining evidence

Accepted here: the independently reviewed cache repair and reconciliation of fresh golden/dev
aggregates. Deferred: the `config.py` profile-parser duplication/default Minor. Preserve current
CLI and service behavior; address the shared contract later using permitted synthetic/dev
inputs, never held-out examples.

No accuracy gate, final numerical checkpoint or publication is accepted. The continuation did
not run real Tesseract or Docker conversion. Golden conversion regeneration verified outputs,
but resumed rows retain cached conversion timings, so those timings are not a fresh end-to-end
runtime comparison. The golden set contains annual documents only and does not establish
interim, unknown-period, model-test or blind performance.

## Evidence identity

Fresh aggregate artifacts and SHA-256:

- `var/eval/gates-dev.json` — `2c6279e62bd3ec06931696158853f0bf44c8b6d2db5ec5e3c7b464baaf73c9aa`
- `var/eval/structure-golden.json` — `f956c65c491e6048cb80e8418cb23ae1a22399063e094ea3a4336739982bec17`
- `var/eval/convert-golden.json` — `201ea6627a1429f6f0be9e1b5c4b45025cd0c9807d2b6a97623642f9fc63b7d4`
- First dry run, `2026-10-04-3abdc22.json` — `7dfafa03c494ad5df0d727359c9d0cc0148d4ff7bc5deccefcb884806d9e17e5`

Recorded successful commands: `PYTHONPATH=eval uv run python -m harness.gates` (exit 0),
`uv run python eval/harness/structure.py` (exit 0), and `uv run python eval/harness/convert.py`
(exit 0). Their captured outputs are in `/private/tmp/week2-cache-verification/eval-gates.log`,
`eval-structure.log`, and `eval-convert-resumed.log`, respectively. These establish report
production and golden regeneration, not gate acceptance or fresh end-to-end timing.
