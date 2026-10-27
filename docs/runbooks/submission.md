# Assessor submission runbook

This guide describes the local development candidate at Task 4 base commit
`65a7e5bcfdb2197411d55071dbad990c6316a08c`. It is a reproducible way to submit a permitted
PDF, inspect the resulting analysis and show the application's uncertainty controls. It is not
a production deployment recipe or a general accuracy claim.

## Requirements and setup

- Native profile: macOS on Apple Silicon, Python `>=3.12,<3.13`, and `uv`. The workspace pins
  application and ingestion dependencies in `uv.lock`; key runtime pins include Docling
  `2.126.0`, Docling Core `2.96.0`, and FastAPI `0.141.1`. Run `make setup` to sync all
  workspace packages. Native scans use Apple Vision through `ocrmac`.
- Docker profile: Docker Desktop with Docker Compose is sufficient for `make docker-up`. It
  builds the image and its locked Python environment from the checked-in lockfile (the builder
  uses uv `0.8.3`). The image installs Tesseract and its English and Arabic language data; it
  uses CPU conversion and Tesseract OCR. The documented `make docker-health` check runs Python
  on the host, so that check additionally requires Python 3.12, `uv`, and the host workspace
  dependencies installed by `make setup`. The Compose health check runs inside the container
  and does not require host Python.
- The first dependency or image acquisition can need network access. Task 3 used preexisting
  Docling assets with offline flags; it did not validate acquiring fresh assets offline. A new
  environment without cached assets may need network access on first use. No paid external
  model provider is required for ordinary ingestion or summaries.

The repository already contains a tracked `.env` with example port defaults. Review those
values and change them only if either port is unavailable. Make and Compose let values in the
process environment override the `.env` values. If you edit `.env`, keep both required ports
available and distinct:

```dotenv
FRA_API_PORT=18730
FRA_LLM_PORT=18731
```

The checked-in values are examples, not required port assignments. `FRA_API_PORT` is the host
API port for both profiles. `FRA_LLM_PORT` is required by Make and Compose configuration even
when the optional inference service is disabled. `FRA_NORMALIZATION_ENABLED` defaults to `0`;
leave it unset for the standard submission flow. Do not put credentials in `.env`.

## Native profile

From the repository root, install dependencies once:

```sh
make setup
```

Start the API and worker in separate terminals, both from this checkout:

```sh
make dev
```

```sh
make worker
```

Open `http://127.0.0.1:<FRA_API_PORT>/`, select a permitted PDF and submit it. The job page
shows progress; open its results when processing ends. The API binds to loopback. Stop both
processes with Ctrl+C. Jobs and uploaded files are stored under `var/` and remain available
after stopping the native processes.

## Docker Desktop profile

Stop native API/worker processes first: Compose publishes the same `FRA_API_PORT` on loopback,
so both profiles cannot bind that port at once. From the repository root with Docker Desktop
and Compose running, start the profile:

```sh
make docker-up
```

To run the optional host-side health command, first install the host dependencies with
`make setup` (Python 3.12 and `uv` are required), then run `make docker-health`:

```sh
make setup
make docker-health
```

Open `http://127.0.0.1:<FRA_API_PORT>/` and upload the permitted PDF. Docker uses Tesseract
where native uses Vision, so scanned-document output is not directly comparable across profiles.
Stop the profile with:

```sh
docker compose down
```

This stops and removes the project containers and network while preserving the named `fra-var`
volume that stores jobs, uploads and artifacts. Do not add `-v` if that data should remain.

## Inputs and honest expected behavior

Use a PDF under `eval/golden/documents/` or an already authorized rehearsal input. Do not use
`model_test`, `blind`, training holdout material, or any other protected example for a demo.
The golden set is permitted development material, not a representative sample of unseen reports.

`completed` means processing finished, not that every extracted figure is correct. `needs_review`
is an expected successful outcome when identity checks, statement totals, row alignment, critical
line mapping, separators, or other review checks cannot confirm the statement. The analysis is
saved and the job's error remains empty; held statements and their reasons stay visible. A
partial summary can state only its eligible metrics, lists omitted metrics and the reasons for
omission, and does not make unconfirmed statement figures globally verified. A fully suppressed
summary is also a valid outcome. A decline is a separate routing result for supported cases
such as mainly Arabic input or sufficiently certain unsupported industry classification.

For a successful honest held/partial demonstration, upload the permitted English scanned
Juhayna golden PDF `eval/golden/documents/juhayna-2024-en-consolidated.pdf` on Docker. Task 3
freshly processed this 57-page scan to `needs_review`, with no job error and a partial summary
containing net margin only. This is an example of the visible hold path, not an independent
accuracy check. Task 3 also freshly submitted an already authorized English digital rehearsal
input to both profiles; both jobs were `needs_review` with no job error, and their digital
analysis content matched apart from job ID. Do not use these three runs to claim unseen-input
accuracy.

On results pages, inspect statement badges and grouped plain-language review reasons. The exact
producer codes remain in the collapsed Technical details disclosures. Formula labels use
reader-facing names, and clickable source references open the source page with a highlighted
region and a return link. The UI's reason labels are currently English even when result content
is Arabic. Arabic output is retained, but Arabic has no current gate score and mainly Arabic
uploads are declined.

## Current evidence and limits

Task 3 was a fresh deployment, browser and presentation check on exact candidate commit
`65a7e5bcfdb2197411d55071dbad990c6316a08c`. All three submitted jobs were `needs_review` with
`error=null`; source links/images, mobile layout and a digital native/Docker comparison were
inspected. The scan was exercised on Docker only. No new source arithmetic was performed in
Task 3: Task 2 independently checked representative source arithmetic, including the 2.1% net
margin, in separate evidence. Task 3 did not repeat that work. Task 2's source-verification
record is kept privately at
`/Users/nadamohamed/Developer/GitHub/financial-reports-analysis-model/.pi/task-evidence/source-verification/verification.md`.

The checks and this UI verification do not establish general numerical accuracy, production
readiness, or a clean unseen-document rehearsal. The Task 3 scan coverage is Docker-only; no
fresh native scan, Arabic semantic behavior or decline path was exercised. Normalization and
optional model serving were disabled and not exercised. Full Gate D is not established by Task
3 alone; the scanned portion remains unmet under the documented D24 exception. Gates A/B on
`model_test` and `blind`, Gate C on `model_test`, and Gate E correctness remain unjudged as
specified in the [accuracy report](../accuracy-report.md). Historical measured sections there
remain the source for their original dates and denominators.

Private evidence is kept outside Git in the local Task 3 and Task 2 evidence directories; it is
not required to run the application. Task 3 coordinator summary:
`/Users/nadamohamed/.herdr/worktrees/financial-reports-analysis-model/task3-final-e2e/.pi/task-evidence/task3-resumed-final/coordinator-report.md`.

## Optional local normalization (not required)

The standard pipeline uses deterministic mapping and a fixed direct-template summary. Trained
normalization is an opt-in local service controlled by `FRA_NORMALIZATION_ENABLED=1`; it needs
the configured owner-provided GGUF asset at `var/models/qwen2.5-7b-normalization-q4_k_m.gguf`
(4,683,073,408 bytes and matching the SHA-256 in `configs/normalization.toml`) plus llama.cpp
`llama-server`. Native `make llm` serves on loopback; Docker uses the optional Compose `llm`
profile and its pinned CPU image. If enabled assets or server requirements are missing or fail
verification, normalization errors rather than silently falling back. The optional asset is not
downloaded by this runbook, and optional serving was not part of Task 3 verification. Do not
enable it to complete the ordinary upload demonstration.

## Developer verification commands

The Task 3 check evidence records successful `make test` (5,868 passed, 28 skipped, 17
deselected), `make lint`, `make typecheck`, and `make docs-check` on the candidate. These checks
are engineering evidence, not application requirements. Internal worker escalation, host
runtime, corpus evaluation and training commands are developer tooling and are not part of this
assessor workflow.
