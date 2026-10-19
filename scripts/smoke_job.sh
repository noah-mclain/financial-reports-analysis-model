#!/usr/bin/env bash
# Run one PDF through an isolated native API and worker. Requires curl, jq and a checkout venv.
# Exit: 0 completed, 2 needs_review, 1 failed or setup error.
set -euo pipefail

PDF="${1:?usage: $0 report.pdf}"
[[ -f "$PDF" ]] || { echo "no such file: $PDF" >&2; exit 1; }
for tool in curl jq; do command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }; done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8765}"
TIMEOUT="${TIMEOUT:-900}"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT > 0 && PORT < 65536 )) || { echo "PORT must be 1..65535" >&2; exit 1; }
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] && (( TIMEOUT > 0 )) || { echo "TIMEOUT must be a positive integer" >&2; exit 1; }
VENV="${VENV:-$ROOT/.venv}"
[[ -x "$VENV/bin/python" ]] || { echo "no executable $VENV/bin/python; set VENV explicitly" >&2; exit 1; }
if [[ -n "${LOG_DIR:-}" ]]; then
  mkdir -p "$LOG_DIR"
else
  mkdir -p "$ROOT/var/smoke"
  LOG_DIR="$(mktemp -d "$ROOT/var/smoke/$(date +%Y%m%d-%H%M%S)-XXXXXX")"
fi
LOG_DIR="$(cd "$LOG_DIR" && pwd)"
STATE="$LOG_DIR/state"
if ! mkdir "$STATE"; then
  echo "ERROR: state already exists in $LOG_DIR; choose a fresh LOG_DIR" >&2
  exit 1
fi
mkdir "$STATE/configs" "$STATE/var"
cp "$ROOT/configs/ingest.toml" "$ROOT/configs/analytics.toml" "$STATE/configs/"
cp "$ROOT/configs/jobs.toml" "$STATE/configs/jobs.toml"

BASE="http://127.0.0.1:$PORT"
API_PID=""; WORKER_PID=""; STREAM_PID=""
cleanup() {
  local p attempt
  for p in "$WORKER_PID" "$API_PID"; do
    [[ -n "$p" ]] || continue
    kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
  done
  for p in "$WORKER_PID" "$API_PID"; do
    [[ -n "$p" ]] || continue
    for attempt in {1..20}; do
      kill -0 -- "-$p" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL -- "-$p" 2>/dev/null || true
  done
  for p in "$WORKER_PID" "$API_PID"; do
    [[ -n "$p" ]] && wait "$p" 2>/dev/null || true
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
die() { echo "ERROR: $* (logs: $LOG_DIR)" >&2; exit 1; }
alive() {
  # A jobs pipeline runs in a subshell and can retain stale parent job state.
  kill -0 "$1" 2>/dev/null
}

export FRA_ROOT="$STATE" FRA_PROFILE=native PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export PYTHONPATH="$ROOT/packages/core/src:$ROOT/packages/ingest/src:$ROOT/packages/analytics/src:$ROOT/packages/modeling/src:$ROOT/apps/api/src:$ROOT/apps/worker/src"
# Monitor mode gives each service a process group. Cleanup can therefore stop an owned
# service and its descendants without matching unrelated processes by name.
set -m
if curl --connect-timeout 1 --max-time 2 -fsS "$BASE/health" >/dev/null 2>&1; then
  die "port $PORT already serves an API; choose an unused PORT to keep this run isolated"
fi

"$VENV/bin/python" -m uvicorn fra_api.main:app --host 127.0.0.1 --port "$PORT" >"$LOG_DIR/api.log" 2>&1 & API_PID=$!
"$VENV/bin/python" -m fra_worker.main >"$LOG_DIR/worker.log" 2>&1 & WORKER_PID=$!
ready=0
for _ in {1..60}; do
  alive "$API_PID" || { tail -20 "$LOG_DIR/api.log" >&2; die "API exited at start"; }
  alive "$WORKER_PID" || { tail -20 "$LOG_DIR/worker.log" >&2; die "worker exited at start"; }
  if curl --connect-timeout 1 --max-time 2 -fsS "$BASE/health" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
(( ready )) || die "API not healthy after 30 seconds"
alive "$API_PID" || { tail -20 "$LOG_DIR/api.log" >&2; die "API exited during startup"; }
alive "$WORKER_PID" || { tail -20 "$LOG_DIR/worker.log" >&2; die "worker exited during startup"; }
echo "services ready on isolated state in $STATE"

NAME="$(basename "$PDF")"
UPLOAD="$(curl --connect-timeout 5 --max-time 30 -fsS -X POST "$BASE/documents" -H 'Content-Type: application/pdf' -H "X-Filename: $NAME" --data-binary @"$PDF")" || die "upload failed"
SHA="$(jq -er '.sha256 | strings | select(test("^[a-fA-F0-9]{64}$"))' <<<"$UPLOAD")" || die "upload response has no valid sha256"
JOB="$(curl --connect-timeout 5 --max-time 30 -fsS -X POST "$BASE/documents/$SHA/analyses")" || die "enqueue failed"
JOB_ID="$(jq -er '.id | strings | select(length > 0)' <<<"$JOB")" || die "enqueue response has no job id"
echo "job $JOB_ID queued; logs: $LOG_DIR"

# max-time is a portable curl deadline on macOS; a transport or HTTP failure stays visible.
if ! curl --connect-timeout 5 --max-time "$TIMEOUT" -fsSN "$BASE/jobs/$JOB_ID/events" 2>"$LOG_DIR/events.err" \
    | tee "$LOG_DIR/events.sse" \
    | while IFS= read -r line; do
      [[ "$line" == data:* ]] || continue
      ev="${line#data: }"
      kind="$(jq -er '.kind | strings' <<<"$ev")" || { echo "malformed event payload" > "$LOG_DIR/event-error"; exit 1; }
      echo "event $kind $(jq -r '.stage // ""' <<<"$ev") $(jq -r '.detail // ""' <<<"$ev")"
      case "$kind" in completed|needs_review|failed) printf '%s\n' "$kind" > "$LOG_DIR/final-event" ;; esac
    done; then
  cat "$LOG_DIR/events.err" >&2
  [[ ! -f "$LOG_DIR/event-error" ]] || cat "$LOG_DIR/event-error" >&2
  die "event stream failed or exceeded ${TIMEOUT}s"
fi
FINAL=""
[[ ! -f "$LOG_DIR/final-event" ]] || FINAL="$(cat "$LOG_DIR/final-event")"
[[ -n "$FINAL" ]] || die "event stream ended without a final event"

FINAL_JOB="$(curl --connect-timeout 5 --max-time 15 -fsS "$BASE/jobs/$JOB_ID")" || die "final job fetch failed"
echo "$FINAL_JOB" | jq . > "$LOG_DIR/job.json" || die "malformed final job response"
STATUS="$(jq -er '.status | strings' <<<"$FINAL_JOB")" || die "final job has no status"
[[ "$STATUS" == "$FINAL" ]] || die "final event says $FINAL but job endpoint says $STATUS"
case "$FINAL" in
  completed|needs_review)
    curl --connect-timeout 5 --max-time 30 -fsS "$BASE/analyses/$JOB_ID" > "$LOG_DIR/analysis.response.json" || die "analysis fetch failed"
    jq -e --arg id "$JOB_ID" --arg sha "$SHA" 'type == "object" and .job_id == $id and .upload_sha256 == $sha and (.page_count | type == "number" and . >= 1) and (.language | type == "string") and (.statements | type == "array") and (.metrics | type == "array")' "$LOG_DIR/analysis.response.json" >/dev/null || die "analysis response is malformed or does not match this job and upload"
    alive "$API_PID" || { tail -20 "$LOG_DIR/api.log" >&2; die "API exited before final verdict"; }
    alive "$WORKER_PID" || { tail -20 "$LOG_DIR/worker.log" >&2; die "worker exited before final verdict"; }
    cp "$LOG_DIR/analysis.response.json" "$LOG_DIR/analysis.json"
    echo "analysis saved: $LOG_DIR/analysis.json"
    [[ "$FINAL" == completed ]] && exit 0
    exit 2
    ;;
  failed)
    echo "FAILED at stage $(jq -r '.stage // "?"' <<<"$FINAL_JOB"): $(jq -r '.error // "no message"' <<<"$FINAL_JOB")" >&2
    exit 1
    ;;
esac
