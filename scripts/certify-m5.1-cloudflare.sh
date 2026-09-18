#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
TUNNEL_ID="${M5_TUNNEL_ID:-cf-serena-primary}"
TOKEN="${MCP_STUDIO_GATEWAY_TOKEN:-}"
TMP="$(mktemp -d)"
CERT_WORKER=""
CERT_WORKSPACE="m5.1://connectivity-cert-$(date +%s)-$$"

cleanup() {
  if [[ -n "$CERT_WORKER" ]]; then
    curl -sS -X POST "$BASE/api/workers/$CERT_WORKER/release" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }
[[ -n "$TOKEN" ]] || { echo "FAIL: MCP_STUDIO_GATEWAY_TOKEN is not set in this shell"; exit 1; }

echo "=== M5.1 Studio safety state ==="
STATUS="$(curl -fsS "$BASE/api/status")"
echo "$STATUS" | jq '.studio | {version,gateway_enabled,gateway_allowed_servers,connectivity_test_mode,connectivity_auto_reconnect}'
[[ "$(echo "$STATUS" | jq -r '.studio.gateway_enabled')" == "true" ]] || { echo "FAIL: studio.gateway_enabled must be true"; exit 1; }
[[ "$(echo "$STATUS" | jq -r '.studio.connectivity_test_mode')" == "true" ]] || { echo "FAIL: connectivity_test_mode must be true for controlled tunnel-loss certification"; exit 1; }

echo "=== Cloudflare tunnel inventory ==="
ITEM="$(curl -fsS "$BASE/api/connectivity" | jq -c --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id)')"
[[ -n "$ITEM" ]] || { echo "FAIL: tunnel $TUNNEL_ID not registered"; exit 1; }
echo "$ITEM" | jq '{id,provider,name,endpoint,origin,managed,desired_state,status,pid,restart_count,tunnel_name,config_file,metadata}'
[[ "$(echo "$ITEM" | jq -r '.provider')" == "cloudflare" ]] || { echo "FAIL: provider is not cloudflare"; exit 1; }
[[ "$(echo "$ITEM" | jq -r '.managed')" == "true" ]] || { echo "FAIL: tunnel is not managed"; exit 1; }
ENDPOINT="$(echo "$ITEM" | jq -r '.endpoint')"
[[ "$ENDPOINT" == https://* ]] || { echo "FAIL: public endpoint must be https://"; exit 1; }
ROOT="${ENDPOINT%%/mcp/*}/"

echo "=== Static + cloudflared ingress preflight ==="
PREFLIGHT="$(curl -fsS "$BASE/api/connectivity/tunnels/$TUNNEL_ID/preflight")"
echo "$PREFLIGHT" | jq .
[[ "$(echo "$PREFLIGHT" | jq -r '.ok')" == "true" ]] || { echo "FAIL: Cloudflare preflight failed"; exit 1; }

echo "=== Reserve one synthetic worker lease; tunnel loss must not mutate it ==="
CERT_WORKER="$(curl -fsS "$BASE/api/workers" | jq -r '.workers[] | select(.state=="idle") | .id' | head -n1)"
[[ -n "$CERT_WORKER" ]] || { echo "FAIL: no idle worker available for isolation certification"; exit 1; }
curl -fsS -X POST "$BASE/api/workers/$CERT_WORKER/bind" \
  -H 'Content-Type: application/json' \
  -d "{\"workspace\":\"$CERT_WORKSPACE\",\"lease_mode\":\"write\",\"metadata\":{\"certification\":\"M5.1-connectivity\"}}" >/dev/null
curl -fsS -X POST "$BASE/api/workers/$CERT_WORKER/state" \
  -H 'Content-Type: application/json' \
  -d '{"state":"busy","work_label":"M5.1 connectivity isolation"}' >/dev/null

check_cert_worker() {
  local data
  data="$(curl -fsS "$BASE/api/workers")"
  local state workspace holder
  state="$(echo "$data" | jq -r --arg id "$CERT_WORKER" '.workers[] | select(.id==$id) | .state')"
  workspace="$(echo "$data" | jq -r --arg id "$CERT_WORKER" '.workers[] | select(.id==$id) | .workspace')"
  holder="$(echo "$data" | jq -r --arg ws "$CERT_WORKSPACE" '.leases[] | select(.workspace==$ws) | .worker_id')"
  [[ "$state" == "busy" && "$workspace" == "$CERT_WORKSPACE" && "$holder" == "$CERT_WORKER" ]]
}
check_cert_worker || { echo "FAIL: could not establish synthetic worker/lease"; exit 1; }

echo "=== Start/confirm managed Cloudflare tunnel ==="
curl -fsS -X POST "$BASE/api/connectivity/tunnels/$TUNNEL_ID/start" | jq '{id,status,pid,desired_state,restart_count,error}'
for _ in $(seq 1 45); do
  SNAP="$(curl -fsS -X POST "$BASE/api/connectivity/run")"
  TITEM="$(echo "$SNAP" | jq -c --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id)')"
  TSTATUS="$(echo "$TITEM" | jq -r '.status')"
  [[ "$TSTATUS" == "healthy" ]] && break
  sleep 1
done
[[ "${TSTATUS:-}" == "healthy" ]] || { echo "$SNAP" | jq .; echo "FAIL: tunnel did not become healthy"; exit 1; }
OLD_PID="$(echo "$TITEM" | jq -r '.pid')"
OLD_RESTARTS="$(echo "$TITEM" | jq -r '.restart_count // 0')"

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-studio-m5.1-cert","version":"0.6.2"}}}'

echo "=== Public surface: Studio root must NOT be exposed ==="
ROOT_CODE="$(curl -sS -o "$TMP/root.body" -w '%{http_code}' "$ROOT")"
echo "root HTTP=$ROOT_CODE"
if [[ "$ROOT_CODE" == "200" ]] && grep -qi 'MCP Studio' "$TMP/root.body"; then
  echo "FAIL: Studio UI is publicly exposed through the Cloudflare tunnel"
  exit 1
fi

echo "=== Public MCP: unauthenticated request must fail closed ==="
UNAUTH_CODE="$(curl -sS -o "$TMP/unauth.body" -w '%{http_code}' \
  -X POST "$ENDPOINT" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data "$INIT")"
echo "unauth HTTP=$UNAUTH_CODE"
[[ "$UNAUTH_CODE" == "401" ]] || { cat "$TMP/unauth.body"; echo "FAIL: expected HTTP 401 without bearer token"; exit 1; }

remote_probe() {
  : > "$TMP/headers"
  local code sid notify_code tools_code delete_code
  code="$(curl -sS -D "$TMP/headers" -o "$TMP/init.body" -w '%{http_code}' \
    -X POST "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    --data "$INIT")"
  [[ "$code" == "200" ]] || { echo "authenticated initialize HTTP=$code"; cat "$TMP/init.body"; return 1; }
  grep -Eq '"serverInfo"|"protocolVersion"' "$TMP/init.body" || { echo "initialize response did not contain MCP result"; cat "$TMP/init.body"; return 1; }
  sid="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {gsub("\\r", "", $2); print $2; exit}' "$TMP/headers")"
  [[ -n "$sid" ]] || { echo "missing Mcp-Session-Id"; cat "$TMP/headers"; return 1; }

  notify_code="$(curl -sS -o "$TMP/notify.body" -w '%{http_code}' \
    -X POST "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Mcp-Session-Id: $sid" \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    --data '{"jsonrpc":"2.0","method":"notifications/initialized"}')"
  case "$notify_code" in 200|202|204) ;; *) echo "initialized notification HTTP=$notify_code"; return 1;; esac

  tools_code="$(curl -sS -o "$TMP/tools.body" -w '%{http_code}' \
    -X POST "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Mcp-Session-Id: $sid" \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
  [[ "$tools_code" == "200" ]] || { echo "tools/list HTTP=$tools_code"; cat "$TMP/tools.body"; return 1; }
  grep -q 'herdr_' "$TMP/tools.body" || { echo "tools/list did not expose expected Herdr tools"; cat "$TMP/tools.body"; return 1; }

  delete_code="$(curl -sS -o /dev/null -w '%{http_code}' \
    -X DELETE "$ENDPOINT" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Mcp-Session-Id: $sid" \
    -H 'Accept: application/json, text/event-stream')"
  case "$delete_code" in 200|202|204|404|405) ;; *) echo "session DELETE HTTP=$delete_code"; return 1;; esac
  echo "REMOTE_MCP_PROBE_PASS session=$sid"
}

echo "=== Authenticated public MCP round-trip ==="
remote_probe
check_cert_worker || { echo "FAIL: public MCP round-trip mutated execution state"; exit 1; }

echo "=== Controlled cloudflared process loss; desired_state stays running ==="
FAULT="$(curl -fsS -X POST "$BASE/api/connectivity/tunnels/$TUNNEL_ID/fault/terminate")"
echo "$FAULT" | jq .
[[ "$(echo "$FAULT" | jq -r '.pid')" == "$OLD_PID" ]] || { echo "FAIL: fault targeted unexpected PID"; exit 1; }

NEW_PID=""
NEW_RESTARTS="$OLD_RESTARTS"
for _ in $(seq 1 60); do
  sleep 1
  SNAP="$(curl -fsS -X POST "$BASE/api/connectivity/run")"
  TITEM="$(echo "$SNAP" | jq -c --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id)')"
  TSTATUS="$(echo "$TITEM" | jq -r '.status')"
  NEW_PID="$(echo "$TITEM" | jq -r '.pid // empty')"
  NEW_RESTARTS="$(echo "$TITEM" | jq -r '.restart_count // 0')"
  if [[ "$TSTATUS" == "healthy" && -n "$NEW_PID" && "$NEW_PID" != "$OLD_PID" ]] && (( NEW_RESTARTS > OLD_RESTARTS )); then
    break
  fi
done

echo "$TITEM" | jq '{id,status,pid,restart_count,desired_state,error,last_healthy_at}'
[[ "$TSTATUS" == "healthy" ]] || { echo "FAIL: tunnel did not recover to healthy"; exit 1; }
[[ -n "$NEW_PID" && "$NEW_PID" != "$OLD_PID" ]] || { echo "FAIL: cloudflared PID did not rotate after controlled loss"; exit 1; }
(( NEW_RESTARTS > OLD_RESTARTS )) || { echo "FAIL: auto-reconnect restart_count did not increase"; exit 1; }
check_cert_worker || { echo "FAIL: tunnel loss/reconnect mutated worker or workspace lease"; exit 1; }

echo "=== Public MCP after auto-reconnect ==="
remote_probe
check_cert_worker || { echo "FAIL: post-reconnect public MCP probe mutated execution state"; exit 1; }

echo "=== Cleanup synthetic execution state ==="
curl -fsS -X POST "$BASE/api/workers/$CERT_WORKER/release" | jq '{id,state,workspace,lease_mode}'
CERT_WORKER=""

echo "================================="
echo "M5_1_CLOUDFLARE_LIVE_CERTIFICATION_PASS"
echo "================================="
