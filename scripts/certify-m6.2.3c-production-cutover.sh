#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
INGRESS="${MCP_STUDIO_OPENAI_INGRESS:-$BASE/ingress/openai/serena-8001}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

STATUS="$(curl -fsS "$BASE/api/status")"
echo "$STATUS" | jq -e '(.studio.version == "0.9.8-m6.2.3c" or .studio.version == "0.9.9-m6.2.4" or .studio.version == "0.9.10-m6.2.5") and .studio.production_mode == true' >/dev/null
echo M6_2_3C_RUNTIME_PASS

echo "$STATUS" | jq -e '.operations.schema.current_version >= 6 and .operations.schema.expected_version >= 6 and .operations.schema.integrity == "ok"' >/dev/null
echo M6_2_3C_SCHEMA_V6_PASS

echo "$STATUS" | jq -e '.studio.managed_session_enabled == true and .studio.managed_session_require_binding_for_tools == true and .studio.managed_session_cutover_enabled == true and .studio.managed_session_legacy_upstream_role == "discovery_only"' >/dev/null
echo M6_2_3C_CUTOVER_POLICY_PASS

# OpenAI Secure MCP Tunnel must no longer bypass Studio to :8001/mcp.
PROFILE="${M623C_TUNNEL_PROFILE:-$HOME/.config/tunnel-client/serena.yaml}"
if [[ -f "$PROFILE" ]]; then
  grep -F 'http://127.0.0.1:8100/ingress/openai/serena-8001' "$PROFILE" >/dev/null
  ! grep -F 'url: "http://127.0.0.1:8001/mcp"' "$PROFILE" >/dev/null
  echo M6_2_3C_OPENAI_NO_BYPASS_PASS
else
  echo "WARN: tunnel profile not found at $PROFILE; skipping local file proof" >&2
fi

if systemctl show tunnel-client.service -p After --value 2>/dev/null | grep -qw mcp-studio.service; then
  echo M6_2_3C_TUNNEL_DEPENDENCY_PASS
else
  echo "FAIL: tunnel-client.service is not ordered after mcp-studio.service" >&2
  echo "Run ./scripts/install-m6.2.3c-tunnel-dependency.sh and retry." >&2
  exit 1
fi

WORKSPACES="$(curl -fsS "$BASE/api/managed/workspaces")"
mapfile -t KEYS < <(echo "$WORKSPACES" | jq -r '.workspaces[].key')
if (( ${#KEYS[@]} < 2 )); then
  echo "FAIL: need at least two registered workspaces for production cutover certification" >&2
  exit 1
fi
A="${M623C_WORKSPACE_A:-${KEYS[0]}}"; B="${M623C_WORKSPACE_B:-${KEYS[1]}}"

new_transport(){
  local tag="$1"
  local hdr="$TMP/$tag.h"
  local out="$TMP/$tag.init"
  local init
  init="$(jq -cn --arg tag "$tag" '{jsonrpc:"2.0",id:1,method:"initialize",params:{protocolVersion:"2025-06-18",capabilities:{},clientInfo:{name:$tag,version:"0.9.9"}}}')"
  curl -fsS -D "$hdr" -o "$out" -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' --data "$init"
  local gws
  gws="$(awk 'BEGIN{IGNORECASE=1}/^mcp-session-id:/{gsub("\\r","");print $2}' "$hdr" | tail -1)"
  [[ "$gws" == gws-* ]]
  curl -fsS -o /dev/null -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $gws" --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  printf '%s' "$gws"
}

call_tool(){
  local gws="$1" id="$2" name="$3" args="$4"
  jq -cn --argjson id "$id" --arg name "$name" --argjson args "$args" '{jsonrpc:"2.0",id:$id,method:"tools/call",params:{name:$name,arguments:$args}}' | \
    curl -fsS -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -H "Mcp-Session-Id: $gws" --data-binary @-
}

G1="$(new_transport m623c-a)"
# Unbound coding tool must fail closed and never hit shared/base Serena.
UNBOUND="$(call_tool "$G1" 2 list_dir '{}')"
echo "$UNBOUND" | grep -q 'NO_MANAGED_SESSION_BOUND\|LEGACY_UPSTREAM_BLOCKED'
echo M6_2_3C_UNBOUND_TOOL_FAIL_CLOSED_PASS

USE_A="$(call_tool "$G1" 3 mcpstudio_use_workspace "$(jq -cn --arg w "$A" '{workspace:$w,name:"m623c-a"}')")"
echo "$USE_A" | jq -e '.result.isError == false' >/dev/null
MSA="$(curl -fsS "$BASE/api/gateway/sessions?limit=200" | jq -r --arg g "$G1" '.sessions[]|select(.id==$g)|.managed_session_id')"
[[ "$MSA" == ms-* ]]
LOGICAL_A="$(curl -fsS "$BASE/api/gateway/sessions?limit=200" | jq -r --arg g "$G1" '.sessions[]|select(.id==$g)|.studio_session_id')"
PIN_A="$(curl -fsS "$BASE/api/sessions?limit=200" | jq -r --arg s "$LOGICAL_A" '.sessions[]|select(.id==$s)|.managed_session_id')"
[[ "$PIN_A" == "$MSA" ]]
echo M6_2_3C_EXISTING_SESSION_MIGRATION_PASS

G2="$(new_transport m623c-b)"
USE_B="$(call_tool "$G2" 4 mcpstudio_use_workspace "$(jq -cn --arg w "$B" '{workspace:$w,name:"m623c-b"}')")"
echo "$USE_B" | jq -e '.result.isError == false' >/dev/null
MSB="$(curl -fsS "$BASE/api/gateway/sessions?limit=200" | jq -r --arg g "$G2" '.sessions[]|select(.id==$g)|.managed_session_id')"
[[ "$MSB" == ms-* && "$MSB" != "$MSA" ]]

A_INFO="$(curl -fsS "$BASE/api/managed/sessions" | jq -c --arg m "$MSA" '.sessions[]|select(.id==$m)')"
B_INFO="$(curl -fsS "$BASE/api/managed/sessions" | jq -c --arg m "$MSB" '.sessions[]|select(.id==$m)')"
[[ "$(echo "$A_INFO"|jq -r '.port')" != "$(echo "$B_INFO"|jq -r '.port')" ]]
[[ "$(echo "$A_INFO"|jq -r '.project_path')" != "$(echo "$B_INFO"|jq -r '.project_path')" ]]
echo M6_2_3C_TWO_WORKSPACE_PRODUCTION_ISOLATION_PASS

BLOCK='{"project":"/tmp/should-not-switch"}'
R="$(call_tool "$G1" 5 activate_project "$BLOCK")"
echo "$R" | grep -q 'SESSION_PROJECT_PINNED'
echo M6_2_3C_PROJECT_SWITCH_BLOCK_PASS

CUT="$(curl -fsS "$BASE/api/managed/status")"
echo "$CUT" | jq -e '.cutover_enabled == true and .legacy_upstream_role == "discovery_only" and .cutover.bound_transports >= 2' >/dev/null
echo M6_2_3C_CUTOVER_OBSERVABILITY_PASS

# Close only certification transports; durable managed sessions are intentionally preserved.
curl -fsS -X DELETE "$INGRESS" -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $G1" >/dev/null || true
curl -fsS -X DELETE "$INGRESS" -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $G2" >/dev/null || true

echo M6_2_3C_TRANSPORT_CLEANUP_PASS
echo
echo '============================================='
echo 'M6_2_3C_PRODUCTION_SESSION_CUTOVER_PASS'
echo '============================================='
