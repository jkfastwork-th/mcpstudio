#!/usr/bin/env bash
set -euo pipefail

BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
INGRESS="${MCP_STUDIO_OPENAI_INGRESS:-$BASE/ingress/openai/serena-8001}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"m625-control-tools-cert","version":"0.9.10"}}}'

curl -fsS -D "$TMP/headers" -o "$TMP/init" \
  -X POST "$INGRESS" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  --data "$INIT"

GWS="$(awk 'BEGIN{IGNORECASE=1}/^mcp-session-id:/{gsub("\r","");print $2}' "$TMP/headers" | tail -1)"
[[ "$GWS" == gws-* ]]

echo M6_2_5_INITIALIZE_PASS

curl -fsS -o /dev/null \
  -X POST "$INGRESS" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $GWS" \
  --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'

TOOLS="$(curl -fsS \
  -X POST "$INGRESS" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $GWS" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')"

# Works for both JSON and a one-message SSE tools/list response.
TOOLS_JSON="$(printf '%s\n' "$TOOLS" | sed -n 's/^data:[[:space:]]*//p' | tail -1)"
if [[ -z "$TOOLS_JSON" ]]; then TOOLS_JSON="$TOOLS"; fi

printf '%s' "$TOOLS_JSON" | jq -e '
  [.result.tools[].name][0:7] == [
    "mcpstudio_use_workspace",
    "mcpstudio_create_session",
    "mcpstudio_use_session",
    "mcpstudio_context_status",
    "mcpstudio_report_context_usage",
    "mcpstudio_handoff_session",
    "mcpstudio_accept_handoff"
  ]
' >/dev/null

echo M6_2_5_CONTROL_TOOLS_FIRST_PASS

for tool in   mcpstudio_use_workspace   mcpstudio_create_session   mcpstudio_use_session   mcpstudio_context_status   mcpstudio_report_context_usage   mcpstudio_handoff_session   mcpstudio_accept_handoff
do
  printf '%s' "$TOOLS_JSON" | jq -e --arg tool "$tool" '.result.tools | any(.name == $tool)' >/dev/null
  echo "M6_2_5_EXPOSE_${tool}_PASS"
done

CURRENT='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"mcpstudio_current_session","arguments":{}}}'
CURRENT_RESULT="$(curl -fsS \
  -X POST "$INGRESS" \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' \
  -H "Mcp-Session-Id: $GWS" \
  --data "$CURRENT")"
printf '%s' "$CURRENT_RESULT" | jq -e '.result.isError == false' >/dev/null

echo M6_2_5_UNBOUND_CONTROL_CALL_PASS

curl -fsS -X DELETE "$INGRESS" \
  -H 'Accept: application/json, text/event-stream' \
  -H "Mcp-Session-Id: $GWS" >/dev/null || true

echo M6_2_5_CHATGPT_CONTROL_TOOL_EXPOSURE_PASS
