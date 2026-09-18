#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
TUNNEL_ID="${M5_TUNNEL_ID:-}"

[[ -n "$TUNNEL_ID" ]] || { echo "FAIL: set M5_TUNNEL_ID to a registered managed Cloudflare tunnel"; exit 1; }
command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }

ITEM="$(curl -fsS "$BASE/api/connectivity" | jq -c --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id)')"
[[ -n "$ITEM" ]] || { echo "FAIL: tunnel $TUNNEL_ID not found"; exit 1; }
echo "$ITEM" | jq '{id,provider,name,endpoint,origin,managed,desired_state,status,tunnel_name,metadata}'
[[ "$(echo "$ITEM" | jq -r '.provider')" == "cloudflare" ]] || { echo "FAIL: provider is not cloudflare"; exit 1; }
[[ "$(echo "$ITEM" | jq -r '.managed')" == "true" ]] || { echo "FAIL: tunnel is not managed"; exit 1; }
[[ "$(echo "$ITEM" | jq -r '.metadata.path_scoped // false')" == "true" ]] || { echo "FAIL: metadata.path_scoped must be true"; exit 1; }

BEFORE="$(curl -fsS "$BASE/api/workers" | jq -c '{summary,leases}')"
OLD_RESTARTS="$(echo "$ITEM" | jq -r '.restart_count // 0')"

echo "=== Start managed Cloudflare tunnel ==="
curl -fsS -X POST "$BASE/api/connectivity/tunnels/$TUNNEL_ID/start" | jq '{id,status,pid,endpoint,desired_state,error}'

for _ in $(seq 1 30); do
  SNAP="$(curl -fsS -X POST "$BASE/api/connectivity/run")"
  STATUS="$(echo "$SNAP" | jq -r --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id) | .status')"
  [[ "$STATUS" == "healthy" ]] && break
  sleep 1
done
[[ "${STATUS:-}" == "healthy" ]] || { echo "$SNAP" | jq .; echo "FAIL: Cloudflare tunnel did not become healthy"; exit 1; }

echo "=== Restart managed Cloudflare tunnel ==="
curl -fsS -X POST "$BASE/api/connectivity/tunnels/$TUNNEL_ID/restart" | jq '{id,status,pid,restart_count,desired_state,error}'
for _ in $(seq 1 30); do
  SNAP="$(curl -fsS -X POST "$BASE/api/connectivity/run")"
  STATUS="$(echo "$SNAP" | jq -r --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id) | .status')"
  [[ "$STATUS" == "healthy" ]] && break
  sleep 1
done
[[ "${STATUS:-}" == "healthy" ]] || { echo "$SNAP" | jq .; echo "FAIL: Cloudflare tunnel did not recover after restart"; exit 1; }
NEW_RESTARTS="$(echo "$SNAP" | jq -r --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id) | .restart_count // 0')"
if (( NEW_RESTARTS <= OLD_RESTARTS )); then
  echo "FAIL: restart_count did not increase"
  exit 1
fi

AFTER="$(curl -fsS "$BASE/api/workers" | jq -c '{summary,leases}')"
if [[ "$BEFORE" != "$AFTER" ]]; then
  echo "FAIL: tunnel restart changed worker/lease state"
  echo "BEFORE=$BEFORE"
  echo "AFTER=$AFTER"
  exit 1
fi

echo "================================="
echo "M5_CLOUDFLARE_LIVE_CERTIFICATION_PASS"
echo "================================="
