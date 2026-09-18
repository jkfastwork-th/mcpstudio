#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"

STATUS="$(curl -fsS "$BASE/api/status")"
echo "$STATUS" | jq -e '(.studio.version == "0.9.9-m6.2.4" or .studio.version == "0.9.10-m6.2.5") and .studio.production_mode == true' >/dev/null
echo M6_2_4_RUNTIME_PASS

echo "$STATUS" | jq -e '.operations.schema.current_version == 7 and .operations.schema.expected_version == 7 and .operations.schema.integrity == "ok"' >/dev/null
echo M6_2_4_SCHEMA_V7_PASS

echo "$STATUS" | jq -e '.studio.managed_session_enabled == true and .studio.managed_session_cutover_enabled == true' >/dev/null
echo M6_2_4_SESSION_MANAGER_PASS

SESS="$(curl -fsS "$BASE/api/managed/sessions")"
echo "$SESS" | jq -e '.status.history_limit >= 1 and (.status.idle_stop_seconds >= 0)' >/dev/null
echo "$SESS" | jq -e 'all(.sessions[]; has("lifecycle_state") and has("connected_transports") and has("ingress_providers") and has("use_count") and has("last_used_at"))' >/dev/null
echo M6_2_4_LIFECYCLE_VIEW_PASS

FIRST="$(echo "$SESS" | jq -r '.sessions[0].id // empty')"
if [[ -n "$FIRST" ]]; then
  HIST="$(curl -fsS "$BASE/api/managed/sessions/$FIRST/history")"
  echo "$HIST" | jq -e --arg id "$FIRST" '.session.id == $id and (.audit|type)=="array" and (.transports|type)=="array"' >/dev/null
  NAME="$(echo "$HIST" | jq -r '.session.name')"
  curl -fsS -X POST "$BASE/api/managed/sessions/$FIRST/rename" -H 'Content-Type: application/json' \
    --data "$(jq -cn --arg name "$NAME" '{name:$name}')" | jq -e --arg name "$NAME" '.session.name == $name' >/dev/null
  echo M6_2_4_HISTORY_RENAME_PASS
else
  echo M6_2_4_HISTORY_RENAME_PASS_NO_SESSIONS
fi

curl -fsS "$BASE/" | grep -q 'managedHistoryToggle'
curl -fsS "$BASE/static/app.js" | grep -q 'data-managed-resume'
curl -fsS "$BASE/static/app.js" | grep -q 'mcpstudio_session_history\|managedSessionHistoryPanel'
echo M6_2_4_UI_LIFECYCLE_PASS

echo
echo '============================================='
echo 'M6_2_4_SESSION_UX_LIFECYCLE_PASS'
echo '============================================='
