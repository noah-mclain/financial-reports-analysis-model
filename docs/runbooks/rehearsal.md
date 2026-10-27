# End-to-end rehearsal protocol and record

This protocol describes a bounded unseen-document rehearsal, not the ordinary application
requirements. Task 3's 27 October verification was a separate fresh deployment and UI check on
already authorized inputs, not a clean unseen-document rehearsal. Its three jobs all ended
`needs_review`; no clean unseen-document result has been established. See the
[submission runbook](submission.md) for the assessor workflow and
[accuracy report](../accuracy-report.md) for dated results.

For a future rehearsal, select two different eligible documents before seeing any output, one
for native and one for Docker, and record their issuer, period, language, scan type, source URL
and hash. The first two runs must remain the selected runs; a replacement chosen after an
outcome is seen must be reported as a replacement, not as a pre-planned run. This protocol does
not authorize opening or scoring protected pools.

## Before any run

1. **Checkpoint taken.** Run these only after the owner-run checkpoint (D13, D17). Until then,
   documents are exercised only as train-holdout dry runs through `make dry-run`, which reports
   no figures; a full-pipeline run on any document that could join `model_test` or `blind` would
   spend a look outside the permitted pool (corpus rules 3, 5 and 6).
2. **Document source decided.** Option 1 reserves unrun `blind` issuers; option 2 uses documents
   from issuers outside the corpus. Under option 1, nobody opens, reads or tunes against a
   `blind` document before the checkpoint (corpus rule 3); whether a rehearsal run is entered in
   `eval/corpus/scoring_log.tsv` follows the owner's decision on that option. Under option 2 there
   is no expected result, so the run checks internal consistency only, not accuracy.
3. **Rehearsal resource check.** This is a precaution for a heavyweight rehearsal, especially
   scan conversion; it is not a general app startup requirement. Do not run the rehearsal beside
   a training fit or another heavy job. Check `vm_stat` and `sysctl vm.swapusage`, and stop if
   free memory is low or swap is in use.
4. **Configuration present.** `.env` exists in the checkout with `FRA_API_PORT` and
   `FRA_LLM_PORT` set (the Makefile refuses to run without them): `grep -E '^FRA_(API|LLM)_PORT=.+' .env`
   prints both lines.
5. **Checks green.** `make test lint typecheck docs-check` pass on the commit being rehearsed.
   Record `git rev-parse HEAD`.
6. **Documents chosen before running.** Choose two different eligible documents, A (native) and B (Docker). Before the first run, write down
   for each document: issuer, period, language, digital or scanned, source URL and `shasum -a 256`. A document
   chosen after seeing its output is not a fresh document.

## Native run (document A)

The recorded native run is the one you inspect on the results page, so the spot check examines
the analysis you record. Start the API and worker in two terminals, upload document A through the
page, and note the job id:

```sh
make dev       # terminal 1: API and upload page on FRA_API_PORT
make worker    # terminal 2: queued jobs
```

Open `/analyses/<job_id>/view` for the results and record that job id and the analysis from
`GET /analyses/<job_id>`. `var/jobs` keeps the job, upload and analysis.

For timing and stage events from a clean isolated state, run the same document through the smoke
script. Call the script directly, with the path in a quoted variable. `make smoke PDF=...` passes the
path to the script as an environment variable, so quotes, backticks and `;` in a file name are
not interpreted, but make reads `$` in the value as its own syntax, so a path containing `$` must
go to the script directly.

From the repository root:

```sh
PDF=/path/to/report.pdf
bash scripts/smoke_job.sh "$PDF"
```

Its exit code is 0 for `completed`, 2 for `needs_review` (the analysis is still saved) and 1 for a
failed job or a setup error; those codes hold only when the script is called directly, because
`make` reports any nonzero recipe exit the same way. Output lands in `var/smoke/<timestamp>-<id>/`:
`analysis.json`, `job.json`, `events.sse`, `api.log`, `worker.log`. `TIMEOUT` (seconds, default 900)
bounds the wait. The smoke API and worker stop when it ends, so its analysis cannot be opened on
the results page; record whether its final status matches the page run's, and use the page run
for the spot check.

## Docker run (document B)

```sh
make docker-up        # needs Docker running and the .env from the preconditions
make docker-health
```

Upload document B (not A) through the page on the Docker profile's API port and keep the job id.
Docker uses Tesseract for OCR where the native profile uses Vision, so a scanned document's
result is not comparable across profiles; do not run A on Docker to compare.

## What to record per run

Record in one file outside Git, per document and profile:

- Commit, profile, document identity (the fields from "Before any run"), wall-clock time and
  the final status (`completed`, `needs_review` or failed, with the failing stage).
- From the analysis (`GET /analyses/<job_id>`, or `analysis.json` for a smoke run): `page_count`, `language`, the number of statements, how many are
  `passed` and how many held for review, and the number of metrics.
- The reasons shown for each held statement and each caveat, copied as displayed.
- A spot check by the owner: at least one figure per statement followed from the results page
  to its page region and compared with the PDF, and one metric's formula checked by hand.
- Anything the page showed as confident that the PDF contradicts. This is the number that
  matters most, because the gates do not measure it for scanned documents
  ([accuracy report](../accuracy-report.md)).

A run is clean when it completes without `needs_review` and without any contradiction found in
the spot check. This is the reading used here; the plan says "clean" without defining it, so the
owner confirms or changes it before the first run. A `needs_review` outcome on a document the
tool should hold, or a decline of a bank, insurer or Arabic-majority document, is correct
behavior and is recorded as such, not counted as clean or as a failure.

## After the runs

Add the outcomes to the "Not measured" list in [accuracy-report.md](../accuracy-report.md): move
each run that happened into a dated section with its counts and limits, and keep the line for
any run that did not. Do not generalize from two documents.
