# Financial reports analysis

This local application accepts English annual or interim financial-report PDFs, locates and
extracts statement pages, computes deterministic metrics, and presents each figure with its
source page. It is a development candidate, not a production-ready or generally accurate
financial analysis service. Many inputs are deliberately held for review; a displayed value
is not globally verified merely because a metric or summary is present.

## Quick Start

Requirements and exact setup, environment, native, Docker, upload and result behavior are in
the [submission runbook](docs/runbooks/submission.md). In brief, use macOS on Apple Silicon
with Python 3.12 and `uv` for the native profile, or Docker Desktop with Compose for
`make docker-up`. The tracked `.env` already provides example ports; change them only if
unavailable. Process environment values override `.env` in both Make and Compose. Run `make
setup` for the native profile or before the optional host-side `make docker-health` command.
For native use, start `make dev` and `make worker` in separate terminals; for Docker use
`make docker-up`, then optionally run `make docker-health` after host setup. Both serve the
upload page at
`http://127.0.0.1:<FRA_API_PORT>/`. The same API port is used by either profile, so stop one
profile before starting the other.

Use a permitted `eval/golden/documents/` PDF or an already authorized rehearsal input. Never
use `model_test`, `blind`, or other held-out examples for a demo. A successful demonstration
can end in `needs_review`: review reasons and source links should be visible, and a partial
summary may contain only individually eligible metrics. See the runbook for expected states
and the exact limits of the current Task 3 evidence.

## Behavior and evidence

The pipeline locates statement pages, converts them with provenance, structures periods,
currency, scale and labels, then computes metrics with deterministic `Decimal` rules. The
default summary uses a checked fixed template, with no language-model narration. Optional
normalization/serving is disabled by default and is not required for the basic workflow.

The result page separates held statements from passed statements, explains review reasons in
plain language, retains raw reason codes under Technical details, labels formulas for readers,
and links source figures to highlighted page regions. A partial summary is limited to eligible
facts and states exclusions; it does not certify the remaining figures. Some reason labels are
English even on Arabic result pages. Arabic is retained in code and data, but the current scored
scope is English; mainly Arabic uploads are declined.

The exact candidate at `65a7e5bcfdb2197411d55071dbad990c6316a08c` has final Task 3 deployment,
browser and presentation verification: fresh native and Docker jobs on an already authorized
English digital rehearsal input and one Docker English-scan job all ended `needs_review` with no
job error. Source links, images, mobile layout and the digital profile comparison were checked.
This did not repeat Task 2's independent source arithmetic, and it does not establish broad
accuracy, a clean unseen-document rehearsal, Arabic or decline behavior, fresh native scan
support, optional normalization, or full Gate D. See the [accuracy report](docs/accuracy-report.md)
for measured gate status and the [submission runbook](docs/runbooks/submission.md) for evidence
scope. Do not interpret a successful upload or a partial summary as production readiness.

## Project map

| Path | Contents |
| --- | --- |
| `packages/core` | Shared schemas, parsers, taxonomy and pool split rules |
| `packages/ingest` | Page location, conversion and statement structuring |
| `packages/analytics` | Identity checks and deterministic metrics |
| `packages/modeling` | Fixed summary templates and grounding checks |
| `apps/api`, `apps/worker` | Upload page, job API and background processing |
| `eval`, `training` | Corpus, evaluation and model-development tools |

## Checks

```sh
make test
make lint
make typecheck
make docs-check
```

Evaluation gates and pool rules are defined in [the revised plan](docs/blueprint/08-revised-plan.md)
and [corpus rules](eval/corpus/README.md). No development work uses the `blind` pool.
