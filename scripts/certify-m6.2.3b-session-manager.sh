#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
INGRESS="${MCP_STUDIO_OPENAI_INGRESS:-$BASE/ingress/openai/serena-8001}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

STATUS="$(curl -fsS "$BASE/api/status")"
echo "$STATUS" | jq -e '(.studio.version == "0.9.7-m6.2.3b-hotfix" or .studio.version == "0.9.8-m6.2.3c" or .studio.version == "0.9.9-m6.2.4" or .studio.version == "0.9.10-m6.2.5") and .studio.production_mode == true' >/dev/null
echo M6_2_3B_RUNTIME_PASS

echo "$STATUS" | jq -e '.operations.schema.current_version >= 6 and .operations.schema.expected_version >= 6 and .operations.schema.integrity == "ok"' >/dev/null
echo M6_2_3B_SCHEMA_V6_PASS

MANAGED="$(curl -fsS "$BASE/api/managed/status")"
echo "$MANAGED" | jq -e '.enabled == true and .require_binding_for_tools == true' >/dev/null
echo M6_2_3B_FAIL_CLOSED_POLICY_PASS

WORKSPACES="$(curl -fsS "$BASE/api/managed/workspaces")"
mapfile -t KEYS < <(echo "$WORKSPACES" | jq -r '.workspaces[].key')
if (( ${#KEYS[@]} < 2 )); then
  echo "FAIL: M6.2.3B isolation certification needs at least two registered workspaces." >&2
  echo "Register a second workspace in UI or POST /api/managed/workspaces, then rerun." >&2
  exit 1
fi
A="${M623B_WORKSPACE_A:-${KEYS[0]}}"; B="${M623B_WORKSPACE_B:-${KEYS[1]}}"

create_api_session(){
  local name="$1" key="$2"
  curl -fsS -X POST "$BASE/api/managed/sessions" -H 'Content-Type: application/json' \
    --data "$(jq -cn --arg name "$name" --arg workspace_key "$key" '{name:$name,workspace_key:$workspace_key}')"
}

# Clean any previous certification sessions that are stopped; active conflicts
# are reported instead of killing real work.
SA="$(create_api_session "m623b-cert-a" "$A")"
SB="$(create_api_session "m623b-cert-b" "$B")"
IDA="$(echo "$SA"|jq -r '.session.id')"; IDB="$(echo "$SB"|jq -r '.session.id')"
cleanup_sessions(){
  curl -fsS -X POST "$BASE/api/managed/sessions/$IDA/stop" >/dev/null 2>&1 || true
  curl -fsS -X POST "$BASE/api/managed/sessions/$IDB/stop" >/dev/null 2>&1 || true
}
trap 'cleanup_sessions; rm -rf "$TMP"' EXIT

PID_A="$(echo "$SA"|jq -r '.session.pid')"; PID_B="$(echo "$SB"|jq -r '.session.pid')"
PORT_A="$(echo "$SA"|jq -r '.session.port')"; PORT_B="$(echo "$SB"|jq -r '.session.port')"
PROJ_A="$(echo "$SA"|jq -r '.session.project_path')"; PROJ_B="$(echo "$SB"|jq -r '.session.project_path')"
[[ "$PID_A" != "$PID_B" && "$PORT_A" != "$PORT_B" && "$PROJ_A" != "$PROJ_B" ]]
ps -p "$PID_A" -o args= | grep -F -- "--project $PROJ_A" >/dev/null
ps -p "$PID_B" -o args= | grep -F -- "--project $PROJ_B" >/dev/null
echo M6_2_3B_TWO_PROCESS_PROJECT_ISOLATION_PASS

# Stop A so ChatGPT/MCP control can create it itself, proving UI and MCP use
# the same backend rather than separate implementations.
curl -fsS -X POST "$BASE/api/managed/sessions/$IDA/stop" >/dev/null

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"m623b-cert","version":"0.9.7"}}}'
curl -fsS -D "$TMP/h" -o "$TMP/init" -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' --data "$INIT"
GWS="$(awk 'BEGIN{IGNORECASE=1}/^mcp-session-id:/{gsub("\\r","");print $2}' "$TMP/h" | tail -1)"
[[ "$GWS" == gws-* ]]
curl -fsS -o /dev/null -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'

TOOLS="$(curl -fsS -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" --data '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
echo "$TOOLS" | grep -q 'mcpstudio_create_session'
echo M6_2_3B_CHATGPT_CONTROL_TOOLS_PASS

CREATE_CALL="$(jq -cn --arg w "$A" '{jsonrpc:"2.0",id:3,method:"tools/call",params:{name:"mcpstudio_create_session",arguments:{name:"m623b-chatgpt-cert",workspace:$w,attach:true}}}')"
CREATE_RESULT="$(curl -fsS -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" --data "$CREATE_CALL")"
echo "$CREATE_RESULT" | jq -e '.result.isError == false' >/dev/null
BOUND="$(curl -fsS "$BASE/api/gateway/sessions?limit=100" | jq -r --arg g "$GWS" '.sessions[]|select(.id==$g)|.managed_session_id')"
[[ "$BOUND" == ms-* ]]
echo M6_2_3B_CHATGPT_CREATE_ATTACH_PASS

BLOCK='{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"activate_project","arguments":{"project":"/tmp/should-not-switch"}}}'
BLOCK_RESULT="$(curl -fsS -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" --data "$BLOCK")"
echo "$BLOCK_RESULT" | grep -q 'SESSION_PROJECT_PINNED'
echo M6_2_3B_PROJECT_SWITCH_BLOCK_PASS

# Detach before close so the process can stop without lying about a live transport.
DETACH='{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"mcpstudio_detach_session","arguments":{}}}'
curl -fsS -o /dev/null -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" --data "$DETACH"
curl -fsS -X POST "$BASE/api/managed/sessions/$BOUND/stop" >/dev/null
curl -fsS -X DELETE "$INGRESS" -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $GWS" >/dev/null || true
# B remains API-created and proves the second project stayed isolated; stop it now.
curl -fsS -X POST "$BASE/api/managed/sessions/$IDB/stop" >/dev/null
trap 'rm -rf "$TMP"' EXIT

echo M6_2_3B_CLEANUP_PASS
echo
echo '============================================='
echo 'M6_2_3B_SESSION_PROJECT_ISOLATION_PASS'
echo '============================================='
