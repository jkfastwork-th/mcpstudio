#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
SERVER_ID="${M623_SERVER_ID:-serena-8001}"
INGRESS_ID="${M623_OPENAI_INGRESS_ID:-openai-serena}"
ENDPOINT="$BASE/ingress/openai/$SERVER_ID"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }

STATUS="$(curl -fsS "$BASE/api/status")"
echo "$STATUS" | jq '.studio | {version,production_mode,openai_local_ingress_enabled,openai_local_ingress_id,openai_local_ingress_path}'
echo "$STATUS" | jq -e '(.studio.version == "0.9.5-m6.2.3a" or .studio.version == "0.9.7-m6.2.3b-hotfix" or .studio.version == "0.9.8-m6.2.3c" or .studio.version == "0.9.9-m6.2.4" or .studio.version == "0.9.10-m6.2.5")' >/dev/null || { echo "FAIL: wrong runtime version"; exit 1; }
echo "$STATUS" | jq -e '.studio.openai_local_ingress_enabled == true' >/dev/null || { echo "FAIL: openai_local_ingress_enabled=false"; exit 1; }
[[ "$(echo "$STATUS" | jq -r '.studio.openai_local_ingress_id')" == "$INGRESS_ID" ]] || { echo "FAIL: ingress id mismatch"; exit 1; }
echo "M6_2_3A_RUNTIME_PASS"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"openai-tunnel-client-cert","version":"m6.2.3a"}}}'
code="$(curl -sS -D "$TMP/init.headers" -o "$TMP/init.body" -w '%{http_code}' \
  -X POST "$ENDPOINT" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data "$INIT")"
[[ "$code" == "200" ]] || { echo "FAIL: initialize HTTP=$code"; cat "$TMP/init.body"; exit 1; }
GWS="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\r", "", $2); print $2; exit}' "$TMP/init.headers")"
[[ "$GWS" == gws-* ]] || { echo "FAIL: missing virtual gateway session id"; cat "$TMP/init.headers"; exit 1; }
echo "M6_2_3A_LOCAL_INITIALIZE_PASS gateway=$GWS"

notify_code="$(curl -sS -o "$TMP/notify.body" -w '%{http_code}' -X POST "$ENDPOINT" \
  -H "Mcp-Session-Id: $GWS" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","method":"notifications/initialized"}')"
case "$notify_code" in 200|202|204) ;; *) echo "FAIL: initialized notification HTTP=$notify_code"; cat "$TMP/notify.body"; exit 1;; esac

code="$(curl -sS -D "$TMP/tools.headers" -o "$TMP/tools.body" -w '%{http_code}' -X POST "$ENDPOINT" \
  -H "Mcp-Session-Id: $GWS" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
[[ "$code" == "200" ]] || { echo "FAIL: tools/list HTTP=$code"; cat "$TMP/tools.body"; exit 1; }
grep -q 'herdr_' "$TMP/tools.body" || { echo "FAIL: Herdr tools not observed"; cat "$TMP/tools.body"; exit 1; }
echo "M6_2_3A_LOCAL_TOOLS_PASS"

ROW="$(curl -fsS "$BASE/api/gateway/sessions?tunnel_id=$INGRESS_ID&limit=20" | jq -c --arg id "$GWS" '.sessions[] | select(.id==$id)')"
[[ -n "$ROW" ]] || { echo "FAIL: local OpenAI session missing from ingress filter"; exit 1; }
echo "$ROW" | jq '{id,status,studio_session_id,last_tunnel_id,ingress_provider,ingress_path,attribution_method,attribution_confidence}'
echo "$ROW" | jq -e --arg id "$INGRESS_ID" '.last_tunnel_id==$id and .ingress_provider=="openai" and .attribution_method=="loopback_openai_secure_tunnel" and .attribution_confidence=="high"' >/dev/null || { echo "FAIL: ingress attribution mismatch"; exit 1; }
echo "M6_2_3A_ATTRIBUTION_PASS"

curl -sS -o /dev/null -X DELETE "$ENDPOINT" \
  -H "Mcp-Session-Id: $GWS" -H 'Accept: application/json, text/event-stream' || true

echo "============================================="
echo "M6_2_3A_OPENAI_LOCAL_INGRESS_PASS"
echo "============================================="
