#!/usr/bin/env bash
set -euo pipefail

api_port="${FRA_API_PORT:-}"
llm_port="${FRA_LLM_PORT:-}"
if [[ ! "$api_port" =~ ^[0-9]+$ || ! "$llm_port" =~ ^[0-9]+$ ]]; then
  echo "FRA_API_PORT and FRA_LLM_PORT must come from the validated .env file" >&2
  exit 2
fi

# Reserve a random name locally, then refuse any matching daemon resources before owning it.
# --project-name overrides both compose.yaml's name and inherited COMPOSE_PROJECT_NAME.
temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/fra-smoke.XXXXXXXXXX")"
project="$(basename "$temp_dir" | tr '[:upper:].' '[:lower:]-')"
compose=(docker compose --project-name "$project")
owned=false

bounded_cleanup() {
  python3 - "$@" <<'PY'
import subprocess
import sys

try:
    result = subprocess.run(sys.argv[1:], timeout=30, check=False)
except subprocess.TimeoutExpired:
    print(f"smoke cleanup timed out: {' '.join(sys.argv[1:])}", file=sys.stderr)
    sys.exit(124)
sys.exit(result.returncode)
PY
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  trap - EXIT
  if [[ "$owned" == true ]]; then
    bounded_cleanup "${compose[@]}" logs --no-color --tail 200 || cleanup_failed=1
    bounded_cleanup "${compose[@]}" stop --timeout 10 api worker || cleanup_failed=1
    bounded_cleanup "${compose[@]}" down --timeout 10 --remove-orphans || cleanup_failed=1
  fi
  rm -rf "$temp_dir" || cleanup_failed=1
  if (( cleanup_failed != 0 )); then
    echo "smoke cleanup failed for project $project" >&2
    if (( status == 0 )); then status=1; fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for resource in containers networks volumes; do
  case "$resource" in
    containers) existing="$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project")" ;;
    networks) existing="$(docker network ls --quiet --filter "label=com.docker.compose.project=$project")" ;;
    volumes) existing="$(docker volume ls --quiet --filter "label=com.docker.compose.project=$project")" ;;
  esac
  if [[ -n "$existing" ]]; then
    echo "smoke project $project already exists ($resource); refusing ownership" >&2
    exit 1
  fi
done

"${compose[@]}" config --quiet
"${compose[@]}" --profile llm config --quiet
"${compose[@]}" build api
# up may partially create resources before reporting a port conflict or startup failure.
owned=true
"${compose[@]}" up --detach --wait --wait-timeout 90 api worker

"${compose[@]}" exec --no-TTY api python -c '
import importlib.metadata as metadata
import os
import torch

assert os.geteuid() != 0, "API container must run as a non-root user"
import fra_core, fra_ingest, fra_analytics, fra_api
import cv2
import docling.document_converter
assert torch.version.cuda is None and not torch.cuda.is_available(), "expected CPU-only torch"
for distribution in metadata.distributions():
    name = (distribution.metadata.get("Name") or "").lower().replace("_", "-")
    assert not name.startswith(("nvidia-", "cuda-", "triton", "mlx", "ocrmac", "pyobjc")), name
print("container imports, non-root identity and CPU-only dependencies passed")
'
"${compose[@]}" exec --no-TTY worker python -c 'import fra_ingest; import fra_core.profile'

# Capture each service identity and prove it is running before requesting SIGTERM. A code
# 143 from a startup crash is not an acceptable shutdown result.
container_ids=()
for service in api worker; do
  container_id="$("${compose[@]}" ps --all --quiet "$service")"
  if [[ -z "$container_id" || "$container_id" == *$'\n'* ]]; then
    echo "expected one $service container after smoke startup" >&2
    exit 1
  fi
  container_ids+=("$container_id")
  state="$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$container_id")"
  if [[ "$state" != "running 0 false" ]]; then
    echo "$service was not running before requested stop: $state" >&2
    exit 1
  fi
  if [[ "$service" == api ]]; then
    health="$(docker inspect --format '{{.State.Health.Status}}' "$container_id")"
    if [[ "$health" != healthy ]]; then
      echo "API was not healthy before requested stop: $health" >&2
      exit 1
    fi
  fi
done
"${compose[@]}" stop --timeout 10 api worker

# Both uvicorn and the init-forwarded placeholder sleep may return 128 + SIGTERM (143)
# after a requested stop. SIGKILL (137), OOM and other exits fail for either service.
for container_id in "${container_ids[@]}"; do
  state="$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$container_id")"
  if [[ "$state" != "exited 0 false" && "$state" != "exited 143 false" ]]; then
    echo "container did not stop gracefully: $container_id: $state" >&2
    exit 1
  fi
done
echo "container start, health, imports and bounded graceful stop passed"
