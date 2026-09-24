#!/usr/bin/env bash
set -euo pipefail

BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
INGRESS="${MCP_STUDIO_OPENAI_INGRESS:-$BASE/ingress/openai/serena-8001}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"world-authoring-audit-live-cert","version":"1.0"}}}'

curl -fsS -D "$TMP/headers" -o "$TMP/init"   -X POST "$INGRESS"   -H 'Accept: application/json, text/event-stream'   -H 'Content-Type: application/json'   --data "$INIT"

GWS="$(awk 'BEGIN{IGNORECASE=1}/^mcp-session-id:/{gsub("\r","");print $2}' "$TMP/headers" | tail -1)"
[[ "$GWS" == gws-* ]]

curl -fsS -o /dev/null   -X POST "$INGRESS"   -H 'Accept: application/json, text/event-stream'   -H 'Content-Type: application/json'   -H "Mcp-Session-Id: $GWS"   --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'

TOOLS="$(curl -fsS   -X POST "$INGRESS"   -H 'Accept: application/json, text/event-stream'   -H 'Content-Type: application/json'   -H "Mcp-Session-Id: $GWS"   --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')"
TOOLS_JSON="$(printf '%s\n' "$TOOLS" | sed -n 's/^data:[[:space:]]*//p' | tail -1)"
[[ -n "$TOOLS_JSON" ]] || TOOLS_JSON="$TOOLS"
printf '%s' "$TOOLS_JSON" | jq -e '.result.tools | any(.name == "mcpstudio_world_authoring_audit")' >/dev/null
echo HIRDA_WORLD_AUTHORING_AUDIT_TOOL_EXPOSE_PASS

CALL="$(jq -cn '{
  jsonrpc:"2.0",id:3,method:"tools/call",params:{
    name:"mcpstudio_world_authoring_audit",
    arguments:{
      workspace:"earth-616-pixi-world",
      events:[
        {eventId:"live-audit-1",kind:"job_created",recordedAt:"2026-09-24T04:45:00Z",providerId:"live-cert",jobId:"job-live-1",requestId:"proposal-live-1",logicalId:"prop.live_lantern",details:{progress:0}},
        {eventId:"live-audit-2",kind:"asset_intake",recordedAt:"2026-09-24T04:45:01Z",providerId:"live-cert",jobId:"job-live-1",requestId:"proposal-live-1",logicalId:"prop.live_lantern"},
        {eventId:"live-audit-3",kind:"earth_validation_attached",recordedAt:"2026-09-24T04:45:02Z",jobId:"job-live-1",requestId:"proposal-live-1",logicalId:"prop.live_lantern",details:{validationId:"earth-live-validation-1",valid:true,mutationAuthorized:false,worldAuthority:"earth-616"}},
        {eventId:"live-audit-4",kind:"asset_promoted",recordedAt:"2026-09-24T04:45:03Z",jobId:"job-live-1",requestId:"proposal-live-1",logicalId:"prop.live_lantern",details:{validationId:"earth-live-validation-1"}}
      ]
    }
  }
}')"

RESULT="$(curl -fsS   -X POST "$INGRESS"   -H 'Accept: application/json, text/event-stream'   -H 'Content-Type: application/json'   -H "Mcp-Session-Id: $GWS"   --data "$CALL")"
RESULT_JSON="$(printf '%s\n' "$RESULT" | sed -n 's/^data:[[:space:]]*//p' | tail -1)"
[[ -n "$RESULT_JSON" ]] || RESULT_JSON="$RESULT"
printf '%s' "$RESULT_JSON" | jq -e '.result.isError == false' >/dev/null
TEXT="$(printf '%s' "$RESULT_JSON" | jq -r '.result.content[0].text')"
printf '%s' "$TEXT" | jq -e '.accepted == 4 and .audit_only == true and .world_authority_changed == false and .asset_promoted_by_hirda == false' >/dev/null
echo HIRDA_WORLD_AUTHORING_AUDIT_CALL_PASS

AUDIT="$(curl -fsS "$BASE/api/audit?limit=50")"
for action in world_authoring.job_created world_authoring.asset_intake world_authoring.earth_validation_attached world_authoring.asset_promoted; do
  printf '%s' "$AUDIT" | jq -e --arg action "$action" '.audit | any(.action == $action and .target_id == "earth-616-pixi-world")' >/dev/null
done
printf '%s' "$AUDIT" | jq -e '.audit | any(.action == "world_authoring.earth_validation_attached" and .data.trace_id == "proposal-live-1:job-live-1:prop.live_lantern" and .data.details.validationId == "earth-live-validation-1" and .data.world_authority_changed == false)' >/dev/null
printf '%s' "$AUDIT" | jq -e '.audit | any(.action == "world_authoring.asset_promoted" and .data.trace_id == "proposal-live-1:job-live-1:prop.live_lantern" and .data.details.validationId == "earth-live-validation-1" and .data.world_authority_changed == false)' >/dev/null
echo HIRDA_WORLD_AUTHORING_AUDIT_PERSISTENCE_PASS

curl -fsS -X DELETE "$INGRESS"   -H 'Accept: application/json, text/event-stream'   -H "Mcp-Session-Id: $GWS" >/dev/null || true

echo HIRDA_WORLD_AUTHORING_AUDIT_LIVE_CERT_PASS
