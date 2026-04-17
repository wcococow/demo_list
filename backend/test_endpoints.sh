#!/usr/bin/env bash

BASE="http://localhost:8000"
PASS=0
FAIL=0
TMP=$(mktemp)

# Unique suffix per run so re-running never hits duplicate username errors
RUN_ID=$(date +%s)
ALICE="alice_$RUN_ID"
BOB="bob_$RUN_ID"

# ── helpers ───────────────────────────────────────────────────────────────────

green() { echo -e "\033[32m✔ $*\033[0m"; }
red()   { echo -e "\033[31m✘ $*\033[0m"; }

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

# req METHOD URL [BODY] — sets $CODE and $BODY; uses $TOKEN if set
req() {
  local method="$1" url="$2" data="${3:-}"
  local args=(-sL -o "$TMP" -w "%{http_code}" -X "$method" "$url")
  [ -n "$TOKEN" ] && args+=(-H "Authorization: Bearer $TOKEN")
  if [ -n "$data" ]; then
    args+=(-H "Content-Type: application/json" -d "$data")
  fi
  CODE=$(curl "${args[@]}")
  BODY=$(cat "$TMP")
}

# req_form METHOD URL BODY — sends form-encoded data; uses $TOKEN if set
req_form() {
  local method="$1" url="$2" data="$3"
  local args=(-sL -o "$TMP" -w "%{http_code}" -X "$method" "$url")
  [ -n "$TOKEN" ] && args+=(-H "Authorization: Bearer $TOKEN")
  args+=(-H "Content-Type: application/x-www-form-urlencoded" -d "$data")
  CODE=$(curl "${args[@]}")
  BODY=$(cat "$TMP")
}

# login USERNAME PASSWORD — sets $TOKEN
login() {
  TOKEN=$(curl -s -X POST "$BASE/auth/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=$1&password=$2" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
}

trap 'rm -f "$TMP"' EXIT

echo ""
echo "Testing API at $BASE"
echo ""

# ── HEALTH ────────────────────────────────────────────────────────────────────

echo "── HEALTH ────────────────────────────────────────────────────────────────"

TOKEN=""
req GET "$BASE/health"
check "GET /health → 200"     "200" "$CODE"
check "GET /health → ok"      "ok"  "$BODY"

echo ""
# ── AUTH: REGISTER ────────────────────────────────────────────────────────────

echo "── AUTH: REGISTER ────────────────────────────────────────────────────────"

req POST "$BASE/auth/register" "{\"username\":\"$ALICE\",\"password\":\"secret123\"}"
check "Register $ALICE → 201"               "201" "$CODE"
check "Register $ALICE → username returned" "$ALICE" "$BODY"

req POST "$BASE/auth/register" "{\"username\":\"$BOB\",\"password\":\"secret456\"}"
check "Register $BOB → 201"                 "201" "$CODE"

req POST "$BASE/auth/register" "{\"username\":\"$ALICE\",\"password\":\"secret123\"}"
check "Register duplicate → 409"           "409" "$CODE"

req POST "$BASE/auth/register" '{"username":"x","password":"123"}'
check "Register short password → 422"      "422" "$CODE"

echo ""
# ── AUTH: LOGIN ───────────────────────────────────────────────────────────────

echo "── AUTH: LOGIN ───────────────────────────────────────────────────────────"

TOKEN=""
req_form POST "$BASE/auth/login" "username=$ALICE&password=secret123"
check "Login $ALICE → 200"           "200" "$CODE"
check "Login $ALICE → access_token"  "access_token" "$BODY"

TOKEN=""
req_form POST "$BASE/auth/login" "username=$ALICE&password=wrongpass"
check "Login wrong password → 401"  "401" "$CODE"

echo ""
# ── TASKS: CREATE ─────────────────────────────────────────────────────────────

echo "── TASKS: CREATE ─────────────────────────────────────────────────────────"

login $ALICE secret123
ALICE_TOKEN="$TOKEN"

req POST "$BASE/tasks" '{"title":"Buy milk"}'
check "POST /tasks → 201"            "201" "$CODE"
check "POST /tasks → title correct"  "Buy milk" "$BODY"
check "POST /tasks → is_done false"  "false" "$BODY"
check "POST /tasks → has owner_id"   "owner_id" "$BODY"
TASK_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
if [ -z "$TASK_ID" ]; then
  red "Could not extract TASK_ID — aborting"
  exit 1
fi

req POST "$BASE/tasks" '{"title":"Walk the dog"}'
check "POST second task → 201"       "201" "$CODE"

req POST "$BASE/tasks" '{"title":""}'
check "POST empty title → 422"       "422" "$CODE"

req POST "$BASE/tasks" '{"title":"   "}'
check "POST whitespace title → 422"  "422" "$CODE"

TOKEN=""
req POST "$BASE/tasks" '{"title":"No auth"}'
check "POST without token → 401"     "401" "$CODE"

echo ""
# ── TASKS: READ ───────────────────────────────────────────────────────────────

echo "── TASKS: READ ───────────────────────────────────────────────────────────"

TOKEN="$ALICE_TOKEN"
req GET "$BASE/tasks"
check "GET /tasks → 200"                   "200" "$CODE"
check "GET /tasks → contains Buy milk"     "Buy milk" "$BODY"
check "GET /tasks → contains Walk the dog" "Walk the dog" "$BODY"

req GET "$BASE/tasks?skip=1&limit=1"
check "GET /tasks?skip=1&limit=1 → 200"   "200" "$CODE"
COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
check "GET /tasks?skip=1&limit=1 → 1 item" "1" "$COUNT"

req GET "$BASE/tasks/$TASK_ID"
check "GET /tasks/:id → 200"               "200" "$CODE"
check "GET /tasks/:id → correct title"     "Buy milk" "$BODY"

req GET "$BASE/tasks/nonexistent-id"
check "GET /tasks/bad-id → 404"            "404" "$CODE"

echo ""
# ── TASKS: UPDATE ─────────────────────────────────────────────────────────────

echo "── TASKS: UPDATE ─────────────────────────────────────────────────────────"

TOKEN="$ALICE_TOKEN"
req PATCH "$BASE/tasks/$TASK_ID" '{"title":"Buy oat milk"}'
check "PATCH title → 200"              "200" "$CODE"
check "PATCH title → new title saved"  "Buy oat milk" "$BODY"

req PATCH "$BASE/tasks/$TASK_ID" '{"is_done":true}'
check "PATCH is_done → 200"            "200" "$CODE"
check "PATCH is_done → true"           "true" "$BODY"
check "PATCH is_done → title unchanged" "Buy oat milk" "$BODY"

req PATCH "$BASE/tasks/$TASK_ID" '{"title":""}'
check "PATCH empty title → 422"        "422" "$CODE"

req PATCH "$BASE/tasks/nonexistent-id" '{"is_done":true}'
check "PATCH bad-id → 404"             "404" "$CODE"

echo ""
# ── OWNERSHIP ─────────────────────────────────────────────────────────────────

echo "── OWNERSHIP ─────────────────────────────────────────────────────────────"

login $BOB secret456
BOB_TOKEN="$TOKEN"

TOKEN="$BOB_TOKEN"
req GET "$BASE/tasks"
check "Bob GET /tasks → sees 0 tasks (his own)" "\\[\\]" "$BODY"

req PATCH "$BASE/tasks/$TASK_ID" '{"title":"Hacked"}'
check "Bob PATCH alice task → 404"    "404" "$CODE"

req DELETE "$BASE/tasks/$TASK_ID"
check "Bob DELETE alice task → 404"   "404" "$CODE"

TOKEN="$ALICE_TOKEN"
req GET "$BASE/tasks/$TASK_ID"
check "Alice task still exists after Bob attempt" "Buy oat milk" "$BODY"

echo ""
# ── TASKS: DELETE ─────────────────────────────────────────────────────────────

echo "── TASKS: DELETE ─────────────────────────────────────────────────────────"

TOKEN="$ALICE_TOKEN"
req DELETE "$BASE/tasks/$TASK_ID"
check "DELETE /tasks/:id → 204"       "204" "$CODE"

req GET "$BASE/tasks/$TASK_ID"
check "GET after DELETE → 404"        "404" "$CODE"

req DELETE "$BASE/tasks/nonexistent-id"
check "DELETE bad-id → 404"           "404" "$CODE"

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "──────────────────────────────────────────────────────────────────────────"
echo -e "  Results:  \033[32m$PASS passed\033[0m   \033[31m$FAIL failed\033[0m"
echo "──────────────────────────────────────────────────────────────────────────"
echo ""

[ "$FAIL" -eq 0 ]
