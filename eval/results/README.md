# Committed evaluation results

Dated copies of the evaluation reports that `docs/accuracy-report.md` cites. The harness writes
the live reports to `var/`, which is not committed and which a later run overwrites; these
copies are the immutable record of the runs that the report describes. Never edit one. A new run
gets a new dated file, and the report then cites that file.

| File | Produced by | Scope | SHA-256 |
| --- | --- | --- | --- |
| `gates-dev-2026-10-24.json` | `make eval-gates` (24 October 2026) | Gate A and Gate B over the 12 `dev` golden documents, per period kind; English is the gated language (D21), Arabic is reported separately. Marked "dev only; not yet measured on model_test" | `dc9439c207b29128d1b417bd31e4ee0364384cd91a539b0b2996077bbc3dcbc7` |
| `locate-golden-2026-10-07.json` | `make eval-locate TARGET=golden` (7 October 2026) | Locator recall over the golden set, which is not held out | `a3115309c2630be9d204fb6e16de4ac9f5220552834da03ad251b857884fa742` |
| `locate-train-2026-10-07.json` | `make eval-locate TARGET=train` (7 October 2026) | Locator over the fit and validation parts of `train`; never the holdout | `b60e3177f631b7a1d35777a740be2d313c5b8d9dcea60d62d830b4c7bdd54cc5` |

Check a copy with `shasum -a 256 eval/results/<file>`.

## What was checked before committing

Each file was searched for absolute local paths, email addresses, credentials and the names of
`model_test` and `blind` documents; none was found. They contain document identifiers, counts,
per-page hashes, outcomes, and row labels read from public, issuer-published reports of the
`dev` golden documents (whose PDFs are already committed under `eval/golden/documents/`) or of
`train` documents. No `model_test` or `blind` result and no holdout document is included.
