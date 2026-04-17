#!/usr/bin/env bash
# =============================================================================
# load_test.sh — concurrent load test with Jaeger trace propagation
#
# Each "user" goes through the full lifecycle:
#   register → login → create task (async, poll until done)
#   → randomly: update title | mark done | delete
#
# Trace visibility in Jaeger
# ──────────────────────────
#   Per-user trace  : every request for user N shares one trace_id
#                     → open Jaeger and paste the trace_id to see all spans
#
#   Whole-run search: every span is also tagged  load_test.run_id=<RUN_ID>
#                     → Jaeger search → tag: load_test.run_id=<RUN_ID>
#                     → see ALL 10 000 users' spans in one query
#
# Usage:
#   ./load_test.sh                        # 10 000 users, 100 concurrent
#   TOTAL=500   CONCURRENCY=50  ./load_test.sh
#   BASE=http://myserver:8000   ./load_test.sh
#
# Jaeger UI: http://localhost:16686
# =============================================================================

set -euo pipefail

# ── config ────────────────────────────────────────────────────────────────────

TOTAL="${TOTAL:-10000}"
CONCURRENCY="${CONCURRENCY:-100}"
BASE="${BASE:-http://localhost:8000}"
PASSWORD="Loadtest1!"
RUN_ID=$(date +%s%3N)           # millisecond timestamp — unique per run, used as Jaeger tag
POLL_RETRIES=20                 # how many times to poll /jobs/:id
POLL_SLEEP=0.3                  # seconds between polls

# ── output dirs ───────────────────────────────────────────────────────────────

WORK_DIR=$(mktemp -d)
OK_DIR="$WORK_DIR/ok"
FAIL_DIR="$WORK_DIR/fail"
LOG_FILE="$WORK_DIR/trace.log"
mkdir -p "$OK_DIR" "$FAIL_DIR"
touch "$LOG_FILE"

# ── colours ───────────────────────────────────────────────────────────────────

GRN="\033[32m"; RED="\033[31m"; YLW="\033[33m"; CYN="\033[36m"; RST="\033[0m"

# ── helpers ───────────────────────────────────────────────────────────────────

# generate N random hex bytes (uses openssl if available, falls back to python3)
hex_rand() {
  if command -v openssl &>/dev/null; then
    openssl rand -hex "$1"
  else
    python3 -c "import os; print(os.urandom($1).hex())"
  fi
}

# extract a top-level JSON string value without spawning python3
# json_get KEY JSON
json_get() {
  echo "$2" | grep -oP "\"$1\"\s*:\s*\"\K[^\"]+"  | head -1
}

# extract result.id from a job SUCCESS response (needs nested parsing)
job_result_id() {
  echo "$1" | python3 -c \
    "import sys,json; r=json.load(sys.stdin).get('result'); print(r['id'] if r else '')" \
    2>/dev/null
}

# atomic log line — concurrent writes to the same file stay readable because
# each line fits well under PIPE_BUF (4 KB on Linux)
log() { echo -e "$*" >> "$LOG_FILE"; }

# ── per-user worker ───────────────────────────────────────────────────────────

run_user() {
  local n="$1"
  local username="lu_${RUN_ID}_${n}"

  # W3C traceparent: 00-<trace-id 32hex>-<span-id 16hex>-01
  local trace_id; trace_id=$(hex_rand 16)
  local root_span; root_span=$(hex_rand 8)
  local tp="00-${trace_id}-${root_span}-01"

  # helper: new child span, same trace
  new_span() { echo "00-${trace_id}-$(hex_rand 8)-01"; }

  # Common headers added to every request:
  #   traceparent        — W3C trace context; links spans per user in Jaeger
  #   X-Load-Test-Run-Id — stamped as span attribute; lets you search ALL users
  #                        in Jaeger with tag: load_test.run_id=<RUN_ID>
  #   X-Load-Test-User   — span attribute showing which user number this is
  #   X-Load-Test-Op     — span attribute showing which operation is in progress
  lt_headers() {
    local op="${1:-}"
    echo -e \
      "-H\0traceparent: $(new_span)" \
      "-H\0X-Load-Test-Run-Id: ${RUN_ID}" \
      "-H\0X-Load-Test-User: ${n}" \
      "-H\0X-Load-Test-Op: ${op}"
  }

  # ── register ────────────────────────────────────────────────────────────────
  local reg
  reg=$(curl -sf -X POST "$BASE/auth/register" \
    -H "Content-Type: application/json" \
    -H "traceparent: $tp" \
    -H "X-Load-Test-Run-Id: $RUN_ID" \
    -H "X-Load-Test-User: $n" \
    -H "X-Load-Test-Op: register" \
    -d "{\"username\":\"$username\",\"password\":\"$PASSWORD\"}" 2>/dev/null) || true

  local user_id; user_id=$(json_get "id" "$reg")
  if [ -z "$user_id" ]; then
    touch "$FAIL_DIR/$n"
    log "${RED}[FAIL]${RST} #${n} ${username} — register failed | trace: ${trace_id}"
    return
  fi

  # ── login ────────────────────────────────────────────────────────────────────
  local login_resp
  login_resp=$(curl -sf -X POST "$BASE/auth/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -H "traceparent: $(new_span)" \
    -H "X-Load-Test-Run-Id: $RUN_ID" \
    -H "X-Load-Test-User: $n" \
    -H "X-Load-Test-Op: login" \
    -d "username=${username}&password=${PASSWORD}" 2>/dev/null) || true

  local token; token=$(json_get "access_token" "$login_resp")
  if [ -z "$token" ]; then
    touch "$FAIL_DIR/$n"
    log "${RED}[FAIL]${RST} #${n} ${username} — login failed | trace: ${trace_id}"
    return
  fi

  # ── create task (async — returns job_id) ─────────────────────────────────────
  local create_resp
  create_resp=$(curl -sf -X POST "$BASE/tasks" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -H "traceparent: $(new_span)" \
    -H "X-Load-Test-Run-Id: $RUN_ID" \
    -H "X-Load-Test-User: $n" \
    -H "X-Load-Test-Op: create_task" \
    -d "{\"title\":\"Task by ${username}\"}" 2>/dev/null) || true

  local job_id; job_id=$(json_get "job_id" "$create_resp")
  if [ -z "$job_id" ]; then
    touch "$FAIL_DIR/$n"
    log "${RED}[FAIL]${RST} #${n} ${username} — create task failed | trace: ${trace_id}"
    return
  fi

  # ── poll /jobs/:id until success ─────────────────────────────────────────────
  local task_id="" status=""
  for _ in $(seq 1 "$POLL_RETRIES"); do
    local poll_resp
    poll_resp=$(curl -sf "$BASE/jobs/$job_id" \
      -H "Authorization: Bearer $token" \
      -H "traceparent: $(new_span)" \
      -H "X-Load-Test-Run-Id: $RUN_ID" \
      -H "X-Load-Test-User: $n" \
      -H "X-Load-Test-Op: poll_job" 2>/dev/null) || true

    status=$(json_get "status" "$poll_resp")
    if [ "$status" = "success" ]; then
      task_id=$(job_result_id "$poll_resp")
      break
    elif [ "$status" = "failed" ]; then
      break
    fi
    sleep "$POLL_SLEEP"
  done

  if [ -z "$task_id" ]; then
    touch "$FAIL_DIR/$n"
    log "${RED}[FAIL]${RST} #${n} ${username} — job ${job_id} never succeeded (status=${status}) | trace: ${trace_id}"
    return
  fi

  # ── random operation: update | complete | delete ──────────────────────────────
  local op=$(( RANDOM % 3 ))
  local op_name=""

  case $op in
    0) # update title
      op_name="update"
      curl -sf -X PATCH "$BASE/tasks/$task_id" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -H "traceparent: $(new_span)" \
        -H "X-Load-Test-Run-Id: $RUN_ID" \
        -H "X-Load-Test-User: $n" \
        -H "X-Load-Test-Op: update_task" \
        -d '{"title":"Updated by load test"}' > /dev/null 2>&1 || true
      ;;
    1) # mark done (complete / confirm)
      op_name="complete"
      curl -sf -X PATCH "$BASE/tasks/$task_id" \
        -H "Authorization: Bearer $token" \
        -H "Content-Type: application/json" \
        -H "traceparent: $(new_span)" \
        -H "X-Load-Test-Run-Id: $RUN_ID" \
        -H "X-Load-Test-User: $n" \
        -H "X-Load-Test-Op: complete_task" \
        -d '{"is_done":true}' > /dev/null 2>&1 || true
      ;;
    2) # delete
      op_name="delete"
      curl -sf -X DELETE "$BASE/tasks/$task_id" \
        -H "Authorization: Bearer $token" \
        -H "traceparent: $(new_span)" \
        -H "X-Load-Test-Run-Id: $RUN_ID" \
        -H "X-Load-Test-User: $n" \
        -H "X-Load-Test-Op: delete_task" \
        > /dev/null 2>&1 || true
      ;;
  esac

  touch "$OK_DIR/$n"
  log "${GRN}[OK]${RST}   #${n} ${username} — ${op_name} | trace: ${CYN}${trace_id}${RST} | jaeger: ${BASE%:8000*}:16686/trace/${trace_id}"
}

# ── preflight check ───────────────────────────────────────────────────────────

echo ""
echo -e "${CYN}══════════════════════════════════════════════════════════════${RST}"
echo -e "${CYN}  Task-API Load Test${RST}"
echo -e "${CYN}══════════════════════════════════════════════════════════════${RST}"
echo -e "  Target    : ${BASE}"
echo -e "  Users     : ${TOTAL}"
echo -e "  Concurrent: ${CONCURRENCY}"
echo -e "  Run ID    : ${CYN}${RUN_ID}${RST}  ← use this to search in Jaeger"
echo -e "  Jaeger UI : ${BASE%:8000*}:16686"
echo -e "  Log file  : ${LOG_FILE}"
echo -e "${CYN}══════════════════════════════════════════════════════════════${RST}"
echo ""

health=$(curl -sf "$BASE/health" 2>/dev/null | grep -c "ok" || true)
if [ "$health" -eq 0 ]; then
  echo -e "${RED}✘ API not reachable at ${BASE} — is it running?${RST}"
  exit 1
fi
echo -e "${GRN}✔ API is healthy${RST}"
echo ""

START_TS=$(date +%s)

# ── progress reporter (background) ───────────────────────────────────────────
(
  while true; do
    ok=$(ls "$OK_DIR" 2>/dev/null | wc -l)
    fail=$(ls "$FAIL_DIR" 2>/dev/null | wc -l)
    done=$((ok + fail))
    pct=$(( done * 100 / TOTAL ))
    running=$(jobs -r 2>/dev/null | wc -l)
    printf "\r  Progress: %5d / %d  (%3d%%)  ✔ %-6d  ✘ %-6d  running: %-4d" \
      "$done" "$TOTAL" "$pct" "$ok" "$fail" "$running"
    [ "$done" -ge "$TOTAL" ] && break
    sleep 1
  done
) &
REPORTER_PID=$!

# ── main loop ─────────────────────────────────────────────────────────────────

for i in $(seq 1 "$TOTAL"); do
  # throttle: wait if too many background jobs are running
  while [ "$(jobs -r | wc -l)" -ge "$CONCURRENCY" ]; do
    sleep 0.05
  done
  run_user "$i" &
done

wait
kill "$REPORTER_PID" 2>/dev/null || true

# ── summary ───────────────────────────────────────────────────────────────────

END_TS=$(date +%s)
ELAPSED=$(( END_TS - START_TS ))
OK_COUNT=$(ls "$OK_DIR" | wc -l)
FAIL_COUNT=$(ls "$FAIL_DIR" | wc -l)

echo ""
echo ""
echo -e "${CYN}══════════════════════════════════════════════════════════════${RST}"
echo -e "${CYN}  Results${RST}"
echo -e "${CYN}══════════════════════════════════════════════════════════════${RST}"
echo -e "  Total users : ${TOTAL}"
echo -e "  ${GRN}Passed      : ${OK_COUNT}${RST}"
echo -e "  ${RED}Failed      : ${FAIL_COUNT}${RST}"
echo -e "  Duration    : ${ELAPSED}s"
echo -e "  Throughput  : $(( TOTAL / (ELAPSED + 1) )) users/sec"
echo -e "${CYN}══════════════════════════════════════════════════════════════${RST}"
echo ""
echo -e "  Full log    : ${LOG_FILE}"
JAEGER="${BASE%:8000*}:16686"
echo -e "  Jaeger UI   : ${JAEGER}"
echo -e "  Run ID      : ${CYN}${RUN_ID}${RST}"
echo ""
echo -e "  ${YLW}── See ALL ${TOTAL} users in one Jaeger search ──────────────────${RST}"
echo -e "  1. Open   ${CYN}${JAEGER}${RST}"
echo -e "  2. Service  →  task-api"
echo -e "  3. Tags     →  ${CYN}load_test.run_id=${RUN_ID}${RST}"
echo -e "  4. Click  Find Traces  (every span from every user appears here)"
echo ""
echo -e "  ${YLW}── Follow a single user (one trace per user) ────────────────${RST}"
echo -e "  Copy any trace_id below → open ${CYN}${JAEGER}/trace/<trace_id>${RST}"
echo -e "  All spans for that user (register→login→create→op) are linked."
echo ""

echo -e "  ${YLW}Last 5 successful traces:${RST}"
grep "\[OK\]" "$LOG_FILE" 2>/dev/null | tail -5 | sed 's/^/    /' || echo "    (none)"
echo ""

rm -rf "$WORK_DIR"
[ "$FAIL_COUNT" -eq 0 ]
