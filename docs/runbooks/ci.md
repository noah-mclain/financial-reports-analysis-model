# Continuous integration and container delivery

The `CI` workflow runs for pull requests targeting `main`, pushes to `main`, manual dispatches,
and every Monday at 04:23 UTC. The Monday run exists to catch a change in the runner image or in
apt that no commit caused. It uses GitHub hosted `ubuntu-24.04` runners with read-only repository
access.

The `quality` and `tests-docker-profile` jobs set up the workspace through one composite action,
`.github/actions/workspace`. It reads the uv version from `docker/Dockerfile` and the Python
version from `.python-version`, installs uv with the SHA-pinned `setup-uv`, checks the lock and
runs `uv sync --locked --all-packages`. With `tesseract: 'true'` it also installs the apt
packages named on the Dockerfile's `apt-get install` line, read by
`scripts/ci/tesseract-packages.sh`. The package list therefore lives only in the Dockerfile, and
the script fails if it cannot find the line. actionlint does not lint an `action.yml` on its
own, but `make ci-workflows-check` checks every use of the action against its declared inputs.

The `quality` job records the reviewed source SHA, sets up the workspace and runs the Makefile
test, lint, typecheck, docs, corpus and workflow checks. The `tests-docker-profile` job runs in
parallel with it: the same workspace plus Tesseract, then
`FRA_PROFILE=docker FRA_OCR_ENGINE=tesseract make test`. The fast tests that need Tesseract skip
when it is missing, so `quality` alone never runs them. The container smoke job runs only after
`quality` passes. It validates both Compose configurations without starting the optional `llm`
service, builds the Docker image, and checks its runtime imports, non-root identity, CPU-only
PyTorch, API health and placeholder worker startup and shutdown.

Each smoke invocation uses a unique Compose project and checks for existing containers,
networks and volumes with that project's label before claiming ownership. It ignores an
inherited `COMPOSE_PROJECT_NAME`. Ports still come from the validated `.env`; an occupied API
port fails startup and removes only the smoke project's partial resources. Existing services
are never stopped to free a port. After proving both services are running and the API is
healthy, the helper requests a bounded SIGTERM stop. Both services may exit with code 0 or 143
(SIGTERM); an OOM, SIGKILL (137), unexpected exit code or non-exited state fails the smoke.

The smoke is a build and runtime contract for the current API skeleton. It does not establish
OCR accuracy, worker functionality, extraction quality, the blueprint's Gate D, or application
deployment. The `CI` workflow does not download model weights or corpora, train, score, or call
inference APIs; only the `Slow tests` workflow downloads models.

## Slow tests

The `Slow tests` workflow runs the tests marked `slow` that do not need a Mac, as
`pytest -m "slow and not mac"` with `FRA_PROFILE=docker` and `FRA_OCR_ENGINE=tesseract`. It runs
every Monday at 04:41 UTC, on manual dispatch, and on a pull request that edits the workflow or
`scripts/ci/docling_models.py`. It is not a pull request gate: the tests load docling's layout and
table models and convert real pages. Measured on a 4-core box with the models already cached, the
12 tests take about 1.5 minutes; the job has a 30-minute timeout.

The `mac` marker, declared in `pyproject.toml`, is for a test that needs Apple Vision (`ocrmac`) or
the Metal device. Such a test fails or skips on Linux, so it carries the marker and the Linux job
deselects it. Nothing else is marked: a slow test that fails on Linux for another reason is a bug
to fix, not a test to mark. No check enforces the marker beyond the job itself, because a test
that needs Vision fails there with `OcrMac is only supported on Mac`, which is the signal to mark it.

The job restores `~/.cache/huggingface/hub` under a key built from the installed `docling` and
`docling-ibm-models` versions and the runner OS (`scripts/ci/docling_models.py key`), so a lock
change that moves either version builds a new cache. Only on a miss, one step sets
`HF_HUB_OFFLINE=0` and runs `scripts/ci/docling_models.py warm`, which downloads the layout
detector and the table structure model, the two repositories the conversion pipeline reads, through
docling's own download helpers (about 506 MB). Every other step, including the tests, runs with
`HF_HUB_OFFLINE=1`, so a model missing from the cache is an error. To change what is downloaded,
edit that script; the key follows the lock without any edit.

## Commit and branch hygiene

The `Hygiene` workflow runs `scripts/ci/hygiene.py` on pull requests to `main` and on pushes to
`main`. It enforces the attribution rules in `CLAUDE.md`, and it needs full history, so it checks
out with `fetch-depth: 0`. A shallow history, a range base that is all zeros, or a base that is
not an ancestor of the head (a force push) is an error, never a pass.

- **Identity.** The owner's email is the author of the oldest root commit reachable from the
  range base, so a pull request cannot bring in its own root and become the owner. An author
  must be that email or `<digits>+<login>@users.noreply.github.com`. A committer may also be
  `noreply@github.com`, which is how GitHub records a merge made in the web interface.
- **Messages.** No `Co-Authored-By` trailer, no tool footer line (a line that starts with the
  phrase and carries a link; the phrase inside a sentence is fine), no session link, and no
  assistant name outside a longer run of lowercase letters, so `_` and CamelCase joins are
  caught. A name followed by `.md` or `-compatible` (the project file, an "OpenAI-compatible"
  endpoint) is a reference, not a match.
- **Branch.** For a pull request, the head branch must not start with a forbidden prefix or hold
  a session id. Pushes to `main` have no branch to check.
- **Added lines.** Only lines added in the range are scanned, for assistant names, tool footer
  lines and session links. `CLAUDE.md`, the checker and its test are excluded because they have
  to spell the rules out. A file already in the repository that names an assistant is flagged
  only when someone edits it.

The word lists and the excluded paths are constants at the top of `scripts/ci/hygiene.py`; the
tests build their bad input from those constants. Pull request values (`head_ref`, the SHAs) reach
the shell only as environment variables.

`make hygiene-check` runs the same check on `origin/main..HEAD` and the current branch name, which
is `CLAUDE.md` rule 6 as a command. It needs a full clone. The owner login comes from
the owner segment of the `origin` remote URL, the same value the workflow takes from
`github.repository_owner`. The target fails if the URL has none; `OWNER_LOGIN=<login>` overrides
it. The `Hygiene` job needs only the standard library, so it runs plain `python3` and does not
use the workspace action.

The check does not look at branches that already exist on the remote.

## Manual GHCR delivery

An owner may dispatch `CI` from `main` with `publish` enabled and `version` set to an existing
canonical `vMAJOR.MINOR.PATCH` tag. The tag must resolve to the exact `main` commit being
dispatched. The publish job also checks that the passing quality and container jobs used that
same SHA, rebuilds and smoke-tests the image in the publishing job, then pushes version and
source-SHA tags to `ghcr.io/<lowercase-owner>/<lowercase-repository>`. It records the registry
digest in the job log. GHCR's default visibility for a newly created package is private; this
workflow does not change it.

Delivery is manually gated and creates no tags, releases or deployments. Pull request images
are never published. The publish job alone receives `packages: write`; it uses the workflow's
`GITHUB_TOKEN` only after release validation. No additional credential, cloud target or
deployment environment is configured.

Registry login uses a temporary `DOCKER_CONFIG` after validation and the image build. An exit
trap attempts logout and removes that directory on success or failure, preserving a failed
publication's exit status.

Pull requests may supersede earlier runs for that PR. Main pushes share their own concurrency
group; each manual dispatch has a separate group identified by its run ID, so a new push or
dispatch cannot replace a pending delivery. Manual dispatches can run concurrently. Operators
must avoid overlapping publications of the same version or source SHA; the workflow provides
no serial release queue or registry tag lock.

## Operator checks

Before dispatching a publication, create and review the canonical version tag on the exact
`main` commit, then select the `main` branch in the Actions dispatch form. Leave `publish`
disabled for a verification run. To inspect a failed smoke, review its Compose logs and the
failed assertion in the job output. Once startup is attempted, the smoke always attempts logs,
service stop and Compose cleanup, each bounded to 30 seconds with a 10-second container stop
grace period. Cleanup failure fails an otherwise successful smoke and preserves any earlier
failure code. Configuration or build failure before startup does not tear down daemon
resources. A container build or import pass is evidence only for the tested runner architecture.
