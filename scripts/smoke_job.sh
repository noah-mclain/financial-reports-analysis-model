#!/usr/bin/env bash
# Run one PDF through an isolated native API and worker. Requires curl, jq and a checkout venv.
# Exit: 0 completed, 2 needs_review, 1 failed or setup error.
set -euo pipefail

PDF="${1:?usage: $0 report.pdf}"
[[ -f "$PDF" ]] || { echo "no such file: $PDF" >&2; exit 1; }
for tool in curl jq; do command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }; done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIMEOUT="${TIMEOUT:-900}"
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
cp "$ROOT/configs/normalization.toml" "$STATE/configs/normalization.toml"
# Keep isolated job artifacts while sharing the server's exact resolved model identity.
# A dangling link is fine when mapping is disabled; enabled identity validation stays loud.
ln -s "$ROOT/var/models" "$STATE/var/models"

BASE="http://localhost"
SOCKET_DIR=""
API_PID=""; WORKER_PID=""; STREAM_PID=""
LAUNCHING=0; PENDING_SIGNAL=0; CLEANED=0
# All owned groups share this TERM grace period, then KILL and reaping complete cleanup.
CLEANUP_GRACE_SECONDS=2
cleanup() {
  trap '' INT TERM
  (( CLEANED == 0 )) || return 0
  CLEANED=1
  local p deadline groups_alive
  for p in "$STREAM_PID" "$WORKER_PID" "$API_PID"; do
    [[ -n "$p" ]] || continue
    kill -TERM -- "-$p" 2>/dev/null || true
  done
  deadline=$((SECONDS + CLEANUP_GRACE_SECONDS))
  while (( SECONDS < deadline )); do
    groups_alive=0
    for p in "$STREAM_PID" "$WORKER_PID" "$API_PID"; do
      if [[ -n "$p" ]] && kill -0 -- "-$p" 2>/dev/null; then groups_alive=1; fi
    done
    (( groups_alive )) || break
    sleep 0.1
  done
  for p in "$STREAM_PID" "$WORKER_PID" "$API_PID"; do
    [[ -n "$p" ]] || continue
    kill -KILL -- "-$p" 2>/dev/null || true
  done
  for p in "$STREAM_PID" "$WORKER_PID" "$API_PID"; do
    [[ -n "$p" ]] && wait "$p" 2>/dev/null || true
  done
  [[ -z "$SOCKET_DIR" ]] || rm -rf -- "$SOCKET_DIR"
}
trap cleanup EXIT
handle_signal() {
  (( PENDING_SIGNAL != 0 )) || PENDING_SIGNAL=$1
  (( LAUNCHING )) || exit "$PENDING_SIGNAL"
}
finish_launch() {
  LAUNCHING=0
  (( PENDING_SIGNAL == 0 )) || exit "$PENDING_SIGNAL"
}
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
die() { echo "ERROR: $* (logs: $LOG_DIR)" >&2; exit 1; }
alive() {
  # A jobs pipeline runs in a subshell and can retain stale parent job state.
  kill -0 "$1" 2>/dev/null
}
# Keep the endpoint outside LOG_DIR: macOS sun_path allows 104 bytes including NUL.
LAUNCHING=1
SOCKET_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fra-smoke.XXXXXX")" || die "cannot create private socket directory"
finish_launch
chmod 700 "$SOCKET_DIR" || die "cannot secure private socket directory with mode 0700"
SOCK="$SOCKET_DIR/api.sock"
SOCK_BYTES="$(LC_ALL=C printf '%s' "$SOCK" | wc -c)"
(( SOCK_BYTES <= 103 )) || die "socket path exceeds 103 bytes: $SOCK"

unset FRA_INGEST_CONFIG FRA_INGEST_EFFECTIVE_CONFIG
export FRA_ROOT="$STATE" FRA_PROFILE=native PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export PYTHONPATH="$ROOT/packages/core/src:$ROOT/packages/ingest/src:$ROOT/packages/analytics/src:$ROOT/packages/modeling/src:$ROOT/apps/api/src:$ROOT/apps/worker/src"
# Monitor mode gives each service a process group. Cleanup can therefore stop an owned
# service and its descendants without matching unrelated processes by name.
set -m

# A handled signal is deferred until $! is recorded; children keep normal signal handling.
LAUNCHING=1
"$VENV/bin/python" -m uvicorn fra_api.main:app --uds "$SOCK" >"$LOG_DIR/api.log" 2>&1 & API_PID=$!
finish_launch
LAUNCHING=1
"$VENV/bin/python" -m fra_worker.main >"$LOG_DIR/worker.log" 2>&1 & WORKER_PID=$!
finish_launch
ready=0
for _ in {1..60}; do
  alive "$API_PID" || { tail -20 "$LOG_DIR/api.log" >&2; die "API exited at start"; }
  alive "$WORKER_PID" || { tail -20 "$LOG_DIR/worker.log" >&2; die "worker exited at start"; }
  if curl --unix-socket "$SOCK" --connect-timeout 1 --max-time 2 -fsS "$BASE/health" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
(( ready )) || die "API not healthy after 30 seconds"
alive "$API_PID" || { tail -20 "$LOG_DIR/api.log" >&2; die "API exited during startup"; }
alive "$WORKER_PID" || { tail -20 "$LOG_DIR/worker.log" >&2; die "worker exited during startup"; }
echo "services ready on isolated state in $STATE"

NAME="$(basename "$PDF")"
UPLOAD="$(curl --unix-socket "$SOCK" --connect-timeout 5 --max-time 30 -fsS -X POST "$BASE/documents" -H 'Content-Type: application/pdf' -H "X-Filename: $NAME" --data-binary @"$PDF")" || die "upload failed"
SHA="$(jq -er '.sha256 | strings | select(test("^[a-fA-F0-9]{64}$"))' <<<"$UPLOAD")" || die "upload response has no valid sha256"
JOB="$(curl --unix-socket "$SOCK" --connect-timeout 5 --max-time 30 -fsS -X POST "$BASE/documents/$SHA/analyses")" || die "enqueue failed"
JOB_ID="$(jq -er '.id | strings | select(length > 0)' <<<"$JOB")" || die "enqueue response has no job id"
echo "job $JOB_ID queued; logs: $LOG_DIR"

# Wait on an owned group so INT/TERM can interrupt streaming, including curl and tee.
# max-time is a portable curl deadline on macOS; a transport or HTTP failure stays visible.
LAUNCHING=1
(
  set +m
  curl --unix-socket "$SOCK" --connect-timeout 5 --max-time "$TIMEOUT" -fsSN "$BASE/jobs/$JOB_ID/events" 2>"$LOG_DIR/events.err" \
    | tee "$LOG_DIR/events.sse" \
    | while IFS= read -r line; do
      [[ "$line" == data:* ]] || continue
      ev="${line#data: }"
      jq -e --arg id "$JOB_ID" '.job_id == $id' <<<"$ev" >/dev/null || { echo "event job_id missing or does not match $JOB_ID" > "$LOG_DIR/event-error"; exit 1; }
      kind="$(jq -er '.kind | strings' <<<"$ev")" || { echo "malformed event payload" > "$LOG_DIR/event-error"; exit 1; }
      case "$kind" in queued|started|finished|completed|needs_review|failed) ;; *) echo "invalid event kind: $kind" > "$LOG_DIR/event-error"; exit 1 ;; esac
      echo "event $kind $(jq -r '.stage // ""' <<<"$ev") $(jq -r '.detail // ""' <<<"$ev")"
      case "$kind" in completed|needs_review|failed) printf '%s\n' "$kind" > "$LOG_DIR/final-event" ;; esac
    done
) & STREAM_PID=$!
finish_launch
if ! wait "$STREAM_PID"; then
  cat "$LOG_DIR/events.err" >&2
  [[ ! -f "$LOG_DIR/event-error" ]] || cat "$LOG_DIR/event-error" >&2
  die "event stream failed or exceeded ${TIMEOUT}s"
fi
STREAM_PID=""
FINAL=""
[[ ! -f "$LOG_DIR/final-event" ]] || FINAL="$(cat "$LOG_DIR/final-event")"
[[ -n "$FINAL" ]] || die "event stream ended without a final event"

FINAL_JOB="$(curl --unix-socket "$SOCK" --connect-timeout 5 --max-time 15 -fsS "$BASE/jobs/$JOB_ID")" || die "final job fetch failed"
echo "$FINAL_JOB" | jq . > "$LOG_DIR/job.json" || die "malformed final job response"
jq -e --arg id "$JOB_ID" '.id == $id' <<<"$FINAL_JOB" >/dev/null || die "final job id missing or does not match $JOB_ID"
jq -e --arg sha "$SHA" '.upload_sha256 == $sha' <<<"$FINAL_JOB" >/dev/null || die "final job upload_sha256 missing or does not match upload"
STATUS="$(jq -er '.status | strings' <<<"$FINAL_JOB")" || die "final job has no status"
[[ "$STATUS" == "$FINAL" ]] || die "final event says $FINAL but job endpoint says $STATUS"
case "$FINAL" in
  completed|needs_review)
    curl --unix-socket "$SOCK" --connect-timeout 5 --max-time 30 -fsS "$BASE/analyses/$JOB_ID" > "$LOG_DIR/analysis.response.json" || die "analysis fetch failed"
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
