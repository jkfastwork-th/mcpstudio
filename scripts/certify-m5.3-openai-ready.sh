#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
TUNNEL_ID="${M5_TUNNEL_ID:-cf-serena-existing}"
TOKEN="${MCP_STUDIO_GATEWAY_TOKEN:-}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }
[[ -n "$TOKEN" ]] || { echo "FAIL: MCP_STUDIO_GATEWAY_TOKEN is not set"; exit 1; }

STATUS="$(curl -fsS "$BASE/api/status")"
echo "=== M5.3 server readiness ==="
echo "$STATUS" | jq '.studio | {version,gateway_enabled,gateway_session_enabled,gateway_session_auto_reconnect,openai_compatibility_enabled,openai_connector_reclaim_enabled}'
[[ "$(echo "$STATUS" | jq -r '.studio.gateway_enabled')" == "true" ]] || { echo "FAIL: gateway_enabled must be true"; exit 1; }
[[ "$(echo "$STATUS" | jq -r '.studio.gateway_session_enabled')" == "true" ]] || { echo "FAIL: gateway_session_enabled must be true"; exit 1; }
[[ "$(echo "$STATUS" | jq -r '.studio.openai_compatibility_enabled')" == "true" ]] || { echo "FAIL: openai_compatibility_enabled must be true"; exit 1; }

ITEM="$(curl -fsS "$BASE/api/connectivity" | jq -c --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id)')"
[[ -n "$ITEM" ]] || { echo "FAIL: tunnel $TUNNEL_ID not registered"; exit 1; }
ENDPOINT="$(echo "$ITEM" | jq -r '.endpoint')"
[[ "$ENDPOINT" == https://* ]] || { echo "FAIL: public endpoint is not https://"; exit 1; }
echo "Endpoint: $ENDPOINT"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ChatGPT M5.3 readiness probe","version":"0.8.0"}}}'

# Deliberately do NOT send X-MCP-Studio-Client-Id or Client-Type. This models
# the conservative real-client path where only the configured bearer credential
# supplies stable connector identity.
CODE="$(curl -sS -D "$TMP/init.headers" -o "$TMP/init.body" -w '%{http_code}' \
  -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: ChatGPT-MCP-M5.3-Readiness' \
  --data "$INIT")"
[[ "$CODE" == "200" ]] || { echo "FAIL: initialize HTTP=$CODE"; cat "$TMP/init.body"; exit 1; }

SID="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/init.headers")"
SCOPE="$(awk 'BEGIN{IGNORECASE=1} /^x-mcp-studio-identity-scope:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/init.headers")"
CLASS="$(awk 'BEGIN{IGNORECASE=1} /^x-mcp-studio-client-class:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/init.headers")"
STUDIO="$(awk 'BEGIN{IGNORECASE=1} /^x-mcp-studio-session-id:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/init.headers")"
[[ "$SID" == gws-* ]] || { echo "FAIL: gateway session missing"; cat "$TMP/init.headers"; exit 1; }
[[ "$STUDIO" == studio-* ]] || { echo "FAIL: logical Studio session missing"; exit 1; }
[[ "$SCOPE" == "connector" ]] || { echo "FAIL: expected connector identity scope, got $SCOPE"; exit 1; }
[[ "$CLASS" == "openai-like" ]] || { echo "FAIL: OpenAI-like client classification missing ($CLASS)"; exit 1; }
echo "OPENAI_SERVER_IDENTITY_MODEL_PASS gateway=$SID studio=$STUDIO scope=$SCOPE"

NOTIFY_CODE="$(curl -sS -o "$TMP/notify.body" -w '%{http_code}' -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SID" \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"notifications/initialized"}')"
case "$NOTIFY_CODE" in 200|202|204) ;; *) echo "FAIL: initialized notification HTTP=$NOTIFY_CODE"; cat "$TMP/notify.body"; exit 1;; esac

TOOLS_CODE="$(curl -sS -o "$TMP/tools.body" -w '%{http_code}' -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SID" \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
[[ "$TOOLS_CODE" == "200" ]] || { echo "FAIL: tools/list HTTP=$TOOLS_CODE"; cat "$TMP/tools.body"; exit 1; }
grep -q 'herdr_' "$TMP/tools.body" || { echo "FAIL: Herdr tools missing"; cat "$TMP/tools.body"; exit 1; }
echo "OPENAI_SERVER_TOOL_SCAN_COMPAT_PASS"

COMPAT="$(curl -fsS "$BASE/api/openai/compatibility")"
echo "$COMPAT" | jq '{enabled,connector_reclaim_enabled,summary,last:(.observations[0] // null)}'
MATCH="$(echo "$COMPAT" | jq -r --arg id "$SID" '[.observations[] | select(.gateway_session_id==$id and .identity_scope=="connector" and .openai_like==true)] | length')"
[[ "$MATCH" -ge 1 ]] || { echo "FAIL: readiness observation not recorded"; exit 1; }
echo "OPENAI_COMPATIBILITY_OBSERVATION_PASS"

curl -sS -o /dev/null -X DELETE "$ENDPOINT" \
  -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SID" \
  -H 'Accept: application/json, text/event-stream' || true

echo "================================="
echo "M5_3_OPENAI_SERVER_READY_PASS"
echo "================================="
