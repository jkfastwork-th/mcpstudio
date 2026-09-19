#!/usr/bin/env bash
set -euo pipefail

BASE="${HIRDA_BASE_URL:-http://127.0.0.1:8100}"
PASS=0
FAIL=0

check_code() {
  local method="$1"
  local path="$2"
  local expected="$3"
  local body="${4:-}"
  local code
  if [[ "$method" == "GET" ]]; then
    code="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$path")"
  else
    code="$(curl -sS -o /dev/null -w '%{http_code}' -X "$method" -H 'Content-Type: application/json' ${body:+--data "$body"} "$BASE$path")"
  fi
  if [[ "$code" =~ ^($expected)$ ]]; then
    printf 'PASS %-4s %-62s %s\n' "$method" "$path" "$code"
    PASS=$((PASS+1))
  else
    printf 'FAIL %-4s %-62s got=%s expected=%s\n' "$method" "$path" "$code" "$expected"
    FAIL=$((FAIL+1))
  fi
}

# Page + data dependencies used by Dashboard and all menu pages.
check_code GET "/" "200"
check_code GET "/static/app.js" "200"
check_code GET "/api/status" "200"
check_code GET "/api/workers" "200"
check_code GET "/api/work?limit=1" "200"
check_code GET "/api/sessions" "200"
check_code GET "/api/gateway/sessions" "200"
check_code GET "/api/managed/sessions" "200"
check_code GET "/api/managed/workspaces" "200"
check_code GET "/api/openai/compatibility" "200"
check_code GET "/api/operations" "200"
check_code GET "/api/observability" "200"
check_code GET "/api/alerts?status=open&limit=1" "200"
check_code GET "/api/audit?limit=1" "200"
check_code GET "/api/events?limit=1" "200"
check_code GET "/api/capsules?limit=1" "200"
check_code GET "/api/agents/runtimes" "200"
check_code GET "/api/computer/status" "200"

# Safe operator buttons: these perform a refresh/poll, not a destructive mutation.
check_code POST "/api/health/poll" "200"
check_code POST "/api/herdr/refresh" "200"

# Mutation forms: invalid bodies must reach the real route and be rejected
# by validation without creating anything.
check_code POST "/api/managed/workspaces" "422" "{}"
check_code POST "/api/managed/sessions" "422" "{}"

# Mutation buttons: fake IDs exercise route matching without touching real state.
check_code POST "/api/managed/sessions/__ui_action_smoke__/rename" "404|422" '{"name":"noop"}'
check_code POST "/api/managed/sessions/__ui_action_smoke__/resume" "404|409|422"
check_code GET  "/api/managed/sessions/__ui_action_smoke__/history" "404"
check_code POST "/api/managed/sessions/__ui_action_smoke__/restart" "404|409|422"
check_code POST "/api/managed/sessions/__ui_action_smoke__/stop" "404|409|422"
check_code POST "/api/gateway/sessions/__ui_action_smoke__/managed/attach" "404|409|422" '{"managed_session_id":"__ui_action_smoke__"}'
check_code POST "/api/gateway/sessions/__ui_action_smoke__/managed/detach" "404|409|422"
check_code GET  "/api/computer/descriptor/__ui_action_smoke__" "404|409"
check_code GET  "/api/computer/re-pair-targets/__ui_action_smoke__" "404|409"
check_code POST "/api/computer/re-pair/__ui_action_smoke__" "404|409|422" '{"mode":"keep","target_display":null}'

printf '\nUI API smoke: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
