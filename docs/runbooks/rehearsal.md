# End-to-end rehearsal

Two end-to-end runs on documents the code has never been run on, one per upload path
([08-revised-plan.md](../blueprint/08-revised-plan.md), 25 to 27 October). This runbook fixes the
procedure and the record so the runs are comparable. It does not choose the documents: that is
the open question after the plan's gates, and it is the owner's decision.

## Before any run

1. **Document source decided.** Option 1 reserves unrun `blind` issuers; option 2 uses documents
   from issuers outside the corpus. Under option 1, nobody opens, reads or tunes against a
   `blind` document before the checkpoint (corpus rule 3); whether a rehearsal run is entered in
   `eval/corpus/scoring_log.tsv` follows the owner's decision on that option. Under option 2 there is no expected result, so the run checks
   internal consistency only, not accuracy.
2. **Machine idle.** The pipeline runs docling and, for scans, OCR. Do not run it beside a
   training fit or any other heavy job: check `vm_stat` and `sysctl vm.swapusage`, and stop if
   free memory is low or swap is in use.
3. **Checks green.** `make test lint typecheck docs-check` pass on the commit being rehearsed.
   Record `git rev-parse HEAD`.
4. **Documents chosen before running.** Write down, before the first run, for each document:
   issuer, period, language, digital or scanned, source URL and `shasum -a 256`. A document
   chosen after seeing its output is not a fresh document.

## Native run

```sh
make smoke PDF=/path/to/report.pdf
```

`scripts/smoke_job.sh` starts an isolated API and worker, uploads the PDF, streams stage events
and fetches the analysis. Exit code 0 means `completed`, 2 means `needs_review` (the analysis is
still saved), 1 means failed or a setup error. Output lands in `var/smoke/<timestamp>-<id>/`:
`analysis.json`, `job.json`, `events.sse`, `api.log`, `worker.log`. `TIMEOUT` (seconds, default
900) bounds the wait.

To look at the results page, run the same document through `make dev` and `make worker` in two
terminals and open `/analyses/<job_id>/view` on `FRA_API_PORT`.

## Docker run

```sh
make docker-up        # needs Docker running
make docker-health
```

Upload the same PDF through the page on the Docker profile's API port and keep the job id. Docker
uses Tesseract for OCR, so a scanned document can differ from the native run; compare the two
and record the difference rather than reconciling it.

## What to record per run

Record in one file outside Git, per document and profile:

- Commit, profile, document identity (the fields from "Before any run"), wall-clock time and
  the final status (`completed`, `needs_review` or failed, with the failing stage).
- From `analysis.json`: `page_count`, `language`, the number of statements, how many are
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
