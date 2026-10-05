# Continuous integration and container delivery

The `CI` workflow runs for pull requests targeting `main`, pushes to `main`, and manual
dispatches. It uses GitHub hosted `ubuntu-24.04` runners with read-only repository access. The
quality job checks the lock, installs every locked workspace package, and runs the Makefile
test, lint, typecheck, docs, corpus and workflow checks. The container smoke job runs only after
quality passes. It validates both Compose configurations without starting the optional `llm`
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
deployment. CI does not download model weights or corpora, train, score, or call inference APIs.

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
