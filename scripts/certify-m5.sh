#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
CLIENT="m5-cert-$(date +%s)-$$"

command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }

echo "=== M5 live certification: connectivity poll ==="
POLL="$(curl -fsS -X POST "$BASE/api/connectivity/run")"
echo "$POLL" | jq '{status, summary, stale_sessions_marked, tunnels: [.tunnels[] | {id,provider,status,desired_state,managed,error}]}'

LOCAL_STATUS="$(echo "$POLL" | jq -r '.tunnels[] | select(.id=="local-serena") | .status' | head -n1)"
if [[ "$LOCAL_STATUS" != "healthy" ]]; then
  echo "FAIL: local-serena connectivity is not healthy (got: ${LOCAL_STATUS:-missing})"
  exit 1
fi

echo "=== Capture execution state; connectivity must not mutate it ==="
BEFORE="$(curl -fsS "$BASE/api/workers" | jq -c '{summary,leases}')"
echo "$BEFORE" | jq .

echo "=== Logical client session reclaim ==="
FIRST="$(curl -fsS -X POST "$BASE/api/sessions/reclaim" -H 'Content-Type: application/json' -d "{\"client_id\":\"$CLIENT\",\"client_type\":\"chatgpt\",\"server_id\":\"serena-8001\",\"metadata\":{\"certification\":\"M5\"}}")"
echo "$FIRST" | jq .
SID="$(echo "$FIRST" | jq -r '.session.id')"
[[ -n "$SID" && "$SID" != "null" ]] || { echo "FAIL: no Studio session id"; exit 1; }

curl -fsS -X DELETE "$BASE/api/sessions/$SID" | jq '{id,status,last_seen_at}'
SECOND="$(curl -fsS -X POST "$BASE/api/sessions/reclaim" -H 'Content-Type: application/json' -d "{\"client_id\":\"$CLIENT\",\"client_type\":\"chatgpt\",\"server_id\":\"serena-8001\",\"metadata\":{\"certification\":\"M5-reclaim\"}}")"
echo "$SECOND" | jq .
SID2="$(echo "$SECOND" | jq -r '.session.id')"
RECLAIMED="$(echo "$SECOND" | jq -r '.reclaimed')"
if [[ "$SID2" != "$SID" || "$RECLAIMED" != "true" ]]; then
  echo "FAIL: Studio session reclaim did not preserve identity"
  exit 1
fi

echo "=== Poll again and verify execution isolation ==="
curl -fsS -X POST "$BASE/api/connectivity/run" >/dev/null
AFTER="$(curl -fsS "$BASE/api/workers" | jq -c '{summary,leases}')"
echo "$AFTER" | jq .
if [[ "$BEFORE" != "$AFTER" ]]; then
  echo "FAIL: connectivity poll changed worker/lease state"
  echo "BEFORE=$BEFORE"
  echo "AFTER=$AFTER"
  exit 1
fi

echo "=== Cloudflare provider inventory ==="
CF="$(curl -fsS "$BASE/api/connectivity" | jq -c '[.tunnels[] | select(.provider=="cloudflare")]')"
echo "$CF" | jq .
if [[ "$(echo "$CF" | jq 'length')" -eq 0 ]]; then
  echo "M5_CLOUDFLARE_LIVE_NOT_CONFIGURED"
else
  echo "M5_CLOUDFLARE_PROVIDER_REGISTERED"
fi

echo "================================="
echo "M5_CORE_LIVE_CERTIFICATION_PASS"
echo "================================="
