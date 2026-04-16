

#!/usr/bin/env bash

BASE="http://localhost:8000"
PASS=0
FAIL=0
TMP=$(mktemp)

# ── helpers ───────────────────────────────────────────────────────────────────

green() { echo -e "\033[32m✔ $*\033[0m"; }
red()   { echo -e "\033[31m✘ $*\033[0m"; }

# check LABEL EXPECTED_SUBSTRING ACTUAL_STRING
check() {
  local label="$1" expected="$2" actual="$3"
  if echo "$actual" | grep -q "$expected"; then
    green "$label"
    PASS=$((PASS + 1))
  else
    red "$label  (want: '$expected'  got: '$actual')"
    FAIL=$((FAIL + 1))
  fi
}

# req METHOD URL [BODY]  — writes HTTP status to $CODE, body to $BODY
# -L follows redirects (FastAPI 307s /tasks/ → /tasks when trailing slash present)
req() {
  local method="$1" url="$2" data="${3:-}"
  if [ -n "$data" ]; then
    CODE=$(curl -sL -o "$TMP" -w "%{http_code}" -X "$method" "$url" \
      -H "Content-Type: application/json" -d "$data")
  else
    CODE=$(curl -sL -o "$TMP" -w "%{http_code}" -X "$method" "$url")
  fi
  BODY=$(cat "$TMP")
}

# ── start server ──────────────────────────────────────────────────────────────

echo ""
echo "Starting server..."
rm -f tasks.db

cd "$(dirname "$0")"

# Fail fast if port 8000 is already in use — stale server would cause false results
if curl -s "$BASE/tasks" >/dev/null 2>&1; then
  echo "ERROR: port 8000 already in use. Kill the existing server and re-run."
  exit 1
fi

uvicorn main:app --port 8000 --log-level error &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null; rm -f tasks.db "$TMP"' EXIT

# Wait up to 10s for server to be ready
for i in $(seq 1 20); do
  curl -s "$BASE/tasks" >/dev/null 2>&1 && break
  sleep 0.5
done
echo ""

# ── CREATE ────────────────────────────────────────────────────────────────────

echo "── CREATE ────────────────────────────────────────────────────────────────"

req POST "$BASE/tasks" '{"title":"Buy milk"}'
check "POST /tasks → 201"                   "201" "$CODE"
check "POST /tasks → title correct"         "Buy milk" "$BODY"
check "POST /tasks → is_done false"         "false" "$BODY"
TASK_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
if [ -z "$TASK_ID" ]; then
  red "Could not extract TASK_ID from POST response — server returned: $BODY"
  echo "Aborting: remaining :id tests will be skipped."
  exit 1
fi

req POST "$BASE/tasks" '{"title":""}'
check "POST empty title → 422"              "422" "$CODE"

req POST "$BASE/tasks" '{"title":"   "}'
check "POST whitespace title → 422"         "422" "$CODE"

# second task for list tests
req POST "$BASE/tasks" '{"title":"Walk the dog"}'

echo ""
# ── READ ALL ──────────────────────────────────────────────────────────────────

echo "── READ ALL ──────────────────────────────────────────────────────────────"

req GET "$BASE/tasks"
check "GET /tasks → 200"                    "200" "$CODE"
check "GET /tasks → contains Buy milk"      "Buy milk" "$BODY"
check "GET /tasks → contains Walk the dog"  "Walk the dog" "$BODY"

req GET "$BASE/tasks?skip=1&limit=1"
check "GET /tasks?skip=1&limit=1 → 200"     "200" "$CODE"
COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
check "GET /tasks?skip=1&limit=1 → 1 item"  "1" "$COUNT"

echo ""
# ── READ ONE ──────────────────────────────────────────────────────────────────

echo "── READ ONE ──────────────────────────────────────────────────────────────"

req GET "$BASE/tasks/$TASK_ID"
check "GET /tasks/:id → 200"                "200" "$CODE"
check "GET /tasks/:id → correct title"      "Buy milk" "$BODY"

req GET "$BASE/tasks/nonexistent-id"
check "GET /tasks/bad-id → 404"             "404" "$CODE"

echo ""
# ── UPDATE ────────────────────────────────────────────────────────────────────

echo "── UPDATE ────────────────────────────────────────────────────────────────"

req PATCH "$BASE/tasks/$TASK_ID" '{"title":"Buy oat milk"}'
check "PATCH title → 200"                   "200" "$CODE"
check "PATCH title → new title saved"       "Buy oat milk" "$BODY"

req PATCH "$BASE/tasks/$TASK_ID" '{"is_done":true}'
check "PATCH is_done → 200"                 "200" "$CODE"
check "PATCH is_done → true"                "true" "$BODY"
check "PATCH is_done → title unchanged"     "Buy oat milk" "$BODY"

req PATCH "$BASE/tasks/$TASK_ID" '{"title":""}'
check "PATCH empty title → 422"             "422" "$CODE"

req PATCH "$BASE/tasks/nonexistent-id" '{"is_done":true}'
check "PATCH bad-id → 404"                  "404" "$CODE"

echo ""
# ── DELETE ────────────────────────────────────────────────────────────────────

echo "── DELETE ────────────────────────────────────────────────────────────────"

req DELETE "$BASE/tasks/$TASK_ID"
check "DELETE /tasks/:id → 204"             "204" "$CODE"

req GET "$BASE/tasks/$TASK_ID"
check "GET after DELETE → 404"              "404" "$CODE"

req DELETE "$BASE/tasks/nonexistent-id"
check "DELETE bad-id → 404"                 "404" "$CODE"

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "──────────────────────────────────────────────────────────────────────────"
echo -e "  Results:  \033[32m$PASS passed\033[0m   \033[31m$FAIL failed\033[0m"
echo "──────────────────────────────────────────────────────────────────────────"
echo ""

[ "$FAIL" -eq 0 ]
