#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
TUNNEL_ID="${M5_TUNNEL_ID:-cf-serena-existing}"
TOKEN="${MCP_STUDIO_GATEWAY_TOKEN:-}"
CLIENT_ID="${M5_2_CLIENT_ID:-m5.2-chatgpt-cert-$$}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }
[[ -n "$TOKEN" ]] || { echo "FAIL: MCP_STUDIO_GATEWAY_TOKEN is not set"; exit 1; }

STATUS="$(curl -fsS "$BASE/api/status")"
echo "=== M5.2 safety state ==="
echo "$STATUS" | jq '.studio | {version,gateway_enabled,gateway_session_enabled,gateway_session_auto_reconnect,gateway_session_replay_on_404,gateway_session_test_mode}'
[[ "$(echo "$STATUS" | jq -r '.studio.gateway_enabled')" == "true" ]] || { echo "FAIL: gateway_enabled must be true"; exit 1; }
[[ "$(echo "$STATUS" | jq -r '.studio.gateway_session_enabled')" == "true" ]] || { echo "FAIL: gateway_session_enabled must be true"; exit 1; }
[[ "$(echo "$STATUS" | jq -r '.studio.gateway_session_test_mode')" == "true" ]] || { echo "FAIL: gateway_session_test_mode must be true during certification"; exit 1; }

ITEM="$(curl -fsS "$BASE/api/connectivity" | jq -c --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id)')"
[[ -n "$ITEM" ]] || { echo "FAIL: tunnel $TUNNEL_ID not registered"; exit 1; }
ENDPOINT="$(echo "$ITEM" | jq -r '.endpoint')"
[[ "$ENDPOINT" == https://* ]] || { echo "FAIL: public endpoint is not https://"; exit 1; }
echo "Endpoint: $ENDPOINT"

snapshot_execution() {
  curl -fsS "$BASE/api/workers" | jq -cS '{workers:[.workers[]|{id,state,workspace,pane,agent,owner_session_id,lease_mode,work_label}],leases:[.leases[]|{workspace,worker_id,session_id}]}'
}
BEFORE="$(snapshot_execution)"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-studio-m5.2-cert","version":"0.7.0"}}}'

initialize_transport() {
  local prefix="$1"
  : > "$TMP/$prefix.headers"
  local code
  code="$(curl -sS -D "$TMP/$prefix.headers" -o "$TMP/$prefix.body" -w '%{http_code}' \
    -X POST "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-MCP-Studio-Client-Id: $CLIENT_ID" \
    -H 'X-MCP-Studio-Client-Type: chatgpt' \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    --data "$INIT")"
  [[ "$code" == "200" ]] || { echo "FAIL: initialize HTTP=$code"; cat "$TMP/$prefix.body"; exit 1; }
  local gws studio reclaimed generation
  gws="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/$prefix.headers")"
  studio="$(awk 'BEGIN{IGNORECASE=1} /^x-mcp-studio-session-id:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/$prefix.headers")"
  reclaimed="$(awk 'BEGIN{IGNORECASE=1} /^x-mcp-studio-reclaimed:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/$prefix.headers")"
  generation="$(awk 'BEGIN{IGNORECASE=1} /^x-mcp-studio-generation:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/$prefix.headers")"
  [[ "$gws" == gws-* ]] || { echo "FAIL: client-visible session was not virtualized"; cat "$TMP/$prefix.headers"; exit 1; }
  [[ "$studio" == studio-* ]] || { echo "FAIL: missing logical Studio session id"; cat "$TMP/$prefix.headers"; exit 1; }
  printf '%s|%s|%s|%s\n' "$gws" "$studio" "$reclaimed" "$generation"
}

notify_initialized() {
  local sid="$1"
  local code
  code="$(curl -sS -o "$TMP/notify.body" -w '%{http_code}' -X POST "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" -H "X-MCP-Studio-Client-Id: $CLIENT_ID" \
    -H "Mcp-Session-Id: $sid" -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","method":"notifications/initialized"}')"
  case "$code" in 200|202|204) ;; *) echo "FAIL: initialized notification HTTP=$code"; cat "$TMP/notify.body"; exit 1;; esac
}

tools_list() {
  local sid="$1" prefix="$2"
  : > "$TMP/$prefix.headers"
  local code
  code="$(curl -sS -D "$TMP/$prefix.headers" -o "$TMP/$prefix.body" -w '%{http_code}' -X POST "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" -H "X-MCP-Studio-Client-Id: $CLIENT_ID" \
    -H "Mcp-Session-Id: $sid" -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
  [[ "$code" == "200" ]] || { echo "FAIL: tools/list HTTP=$code"; cat "$TMP/$prefix.body"; exit 1; }
  grep -q 'herdr_' "$TMP/$prefix.body" || { echo "FAIL: tools/list missing Herdr tools"; cat "$TMP/$prefix.body"; exit 1; }
  local returned
  returned="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/$prefix.headers")"
  [[ "$returned" == "$sid" ]] || { echo "FAIL: gateway session id changed unexpectedly ($sid -> $returned)"; exit 1; }
}

echo "=== Client transport #1 ==="
IFS='|' read -r GWS1 STUDIO1 RECLAIMED1 GEN1 <<<"$(initialize_transport first)"
echo "gateway=$GWS1 studio=$STUDIO1 reclaimed=$RECLAIMED1 generation=$GEN1"
[[ "$RECLAIMED1" == "false" ]] || { echo "FAIL: fresh certification identity unexpectedly reclaimed an existing session"; exit 1; }
notify_initialized "$GWS1"
tools_list "$GWS1" first_tools

GW_ROW="$(curl -fsS "$BASE/api/gateway/sessions" | jq -c --arg id "$GWS1" '.sessions[]|select(.id==$id)')"
[[ "$(echo "$GW_ROW" | jq -r '.studio_session_id')" == "$STUDIO1" ]] || { echo "FAIL: gateway->Studio session mapping mismatch"; exit 1; }
[[ "$(echo "$GW_ROW" | jq -r '.generation')" == "1" ]] || { echo "FAIL: initial generation is not 1"; exit 1; }

echo "=== Drop only the upstream MCP transport session ==="
curl -fsS -X POST "$BASE/api/gateway/sessions/$GWS1/fault/drop-upstream" | jq .

echo "=== Same client-visible gateway id must auto-heal ==="
tools_list "$GWS1" healed_tools
GW_ROW2="$(curl -fsS "$BASE/api/gateway/sessions" | jq -c --arg id "$GWS1" '.sessions[]|select(.id==$id)')"
echo "$GW_ROW2" | jq '{id,studio_session_id,status,generation,reconnect_count,last_reconnected_at,error}'
[[ "$(echo "$GW_ROW2" | jq -r '.generation')" -ge 2 ]] || { echo "FAIL: upstream session generation did not advance"; exit 1; }
[[ "$(echo "$GW_ROW2" | jq -r '.reconnect_count')" -ge 1 ]] || { echo "FAIL: reconnect_count did not advance"; exit 1; }

echo "UPSTREAM_SESSION_AUTO_RECONNECT_PASS gateway=$GWS1"

# Close client transport #1. This marks the logical session disconnected but
# intentionally leaves work/worker/workspace runtime untouched.
DEL_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H "X-MCP-Studio-Client-Id: $CLIENT_ID" \
  -H "Mcp-Session-Id: $GWS1" -H 'Accept: application/json, text/event-stream')"
case "$DEL_CODE" in 200|202|204|404|405) ;; *) echo "FAIL: transport DELETE HTTP=$DEL_CODE"; exit 1;; esac

echo "=== Client transport #2: reclaim logical Studio session ==="
IFS='|' read -r GWS2 STUDIO2 RECLAIMED2 GEN2 <<<"$(initialize_transport second)"
echo "gateway=$GWS2 studio=$STUDIO2 reclaimed=$RECLAIMED2 generation=$GEN2"
[[ "$GWS2" != "$GWS1" ]] || { echo "FAIL: new transport should have a new gateway id"; exit 1; }
[[ "$STUDIO2" == "$STUDIO1" ]] || { echo "FAIL: logical Studio session id was not preserved"; exit 1; }
[[ "$RECLAIMED2" == "true" ]] || { echo "FAIL: new client transport did not reclaim logical session"; exit 1; }
notify_initialized "$GWS2"
tools_list "$GWS2" second_tools

echo "LOGICAL_STUDIO_SESSION_RECLAIM_PASS studio=$STUDIO2"

AFTER="$(snapshot_execution)"
[[ "$AFTER" == "$BEFORE" ]] || {
  echo "FAIL: session reconnect mutated worker/lease execution state"
  echo "BEFORE=$BEFORE"
  echo "AFTER=$AFTER"
  exit 1
}
echo "EXECUTION_ISOLATION_PASS"

# Clean up only certification transport. Logical session remains in history and
# can be reclaimed; no worker/work cleanup is necessary.
curl -sS -o /dev/null -X DELETE "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H "X-MCP-Studio-Client-Id: $CLIENT_ID" \
  -H "Mcp-Session-Id: $GWS2" -H 'Accept: application/json, text/event-stream' || true

echo "================================="
echo "M5_2_CLIENT_SESSION_CONTINUITY_PASS"
echo "================================="
