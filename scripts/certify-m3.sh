#!/usr/bin/env bash
set -euo pipefail

BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
TIMEOUT_SECONDS="${M3_TIMEOUT_SECONDS:-120}"
PANE="${M3_PANE:-}"
AGENT="${M3_AGENT:-}"
WORKSPACE="${M3_WORKSPACE:-}"

command -v jq >/dev/null || { echo "ERROR: jq is required" >&2; exit 1; }

echo "=== M3 live certification: refresh Herdr ==="
HERDR="$(curl -fsS -X POST "$BASE/api/herdr/refresh")"
echo "$HERDR" | jq '{status,agent_count,pane_count,execution_tools,error}'

if [[ "$(echo "$HERDR" | jq -r '.status')" == "down" ]]; then
  echo "FAIL: Herdr is down" >&2
  exit 1
fi
if [[ "$(echo "$HERDR" | jq -r '.execution_tools.herdr_prompt_agent // false')" != "true" ]]; then
  echo "FAIL: herdr_prompt_agent is not available" >&2
  exit 1
fi

# Prefer a currently idle agent pane. The caller may pin M3_PANE/M3_WORKSPACE.
if [[ -z "$PANE" ]]; then
  PANE="$(echo "$HERDR" | jq -r '
    [.panes.panes[]? | select(.agent != null and (.agent_status == "idle" or .agent_status == "done"))]
    | sort_by(if .agent_status == "idle" then 0 else 1 end, .pane_id)
    | .[0].pane_id // empty')"
fi
if [[ -z "$PANE" ]]; then
  echo "FAIL: no idle/done Herdr agent pane found. Set M3_PANE explicitly." >&2
  exit 1
fi

PANE_JSON="$(echo "$HERDR" | jq -c --arg pane "$PANE" '.panes.panes[]? | select(.pane_id == $pane)' | head -n1)"
if [[ -z "$PANE_JSON" ]]; then
  echo "FAIL: pane $PANE not found" >&2
  exit 1
fi

if [[ -z "$WORKSPACE" ]]; then
  WORKSPACE="$(echo "$PANE_JSON" | jq -r '.cwd // .foreground_cwd // empty')"
fi
if [[ -z "$AGENT" ]]; then
  AGENT="$(echo "$PANE_JSON" | jq -r '.agent // empty')"
fi
if [[ -z "$WORKSPACE" || -z "$AGENT" ]]; then
  echo "FAIL: target pane must have workspace and agent. pane=$PANE workspace=$WORKSPACE agent=$AGENT" >&2
  exit 1
fi

echo "Target: pane=$PANE agent=$AGENT workspace=$WORKSPACE"
echo "Certification prompt is read-only: no files, commands, commits, or external state changes."

PAYLOAD="$(jq -nc \
  --arg ws "$WORKSPACE" \
  --arg pane "$PANE" \
  --arg agent "$AGENT" \
  '{
    label:"M3 live certification",
    workspace:$ws,
    priority:100,
    lease_mode:"write",
    pane:$pane,
    agent:$agent,
    dispatch_mode:"herdr",
    instruction:"MCP Studio M3 certification only. Do not modify files, run commands, create commits, or change external state. Reply exactly: M3_CERT_OK",
    metadata:{certification:"M3-live",side_effects:"forbidden"}
  }')"

WORK="$(curl -fsS -X POST "$BASE/api/work" -H 'Content-Type: application/json' -d "$PAYLOAD")"
WORK_ID="$(echo "$WORK" | jq -r '.id')"
echo "Created $WORK_ID"

curl -fsS -X POST "$BASE/api/scheduler/run" | jq .
CURRENT="$(curl -fsS "$BASE/api/work/$WORK_ID")"
if [[ "$(echo "$CURRENT" | jq -r '.state')" != "running" ]]; then
  echo "$CURRENT" | jq .
  echo "FAIL: work was not assigned by scheduler" >&2
  exit 1
fi

echo "=== Explicitly dispatch once ==="
HTTP_CODE="$(curl -sS -o /tmp/mcp-studio-m3-dispatch.json -w '%{http_code}' \
  -X POST "$BASE/api/work/$WORK_ID/dispatch")"
cat /tmp/mcp-studio-m3-dispatch.json | jq .
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "FAIL: dispatch HTTP=$HTTP_CODE" >&2
  echo "Work left intact for inspection: $WORK_ID" >&2
  exit 1
fi

echo "=== Monitor execution ==="
START="$(date +%s)"
while true; do
  curl -fsS -X POST "$BASE/api/execution/run" >/dev/null
  CURRENT="$(curl -fsS "$BASE/api/work/$WORK_ID")"
  STATE="$(echo "$CURRENT" | jq -r '.state')"
  EXEC_STATE="$(echo "$CURRENT" | jq -r '.execution_state')"
  AGENT_STATUS="$(echo "$CURRENT" | jq -r '.last_agent_status // "—"')"
  echo "state=$STATE execution=$EXEC_STATE agent=$AGENT_STATUS"

  if [[ "$STATE" == "completed" ]]; then
    echo "$CURRENT" | jq '{id,state,execution_state,worker_id,pane,agent,dispatch_attempts,recovery_count,result}'
    echo
    echo "================================="
    echo "M3_LIVE_CERTIFICATION_PASS"
    echo "================================="
    exit 0
  fi
  if [[ "$STATE" == "failed" || "$EXEC_STATE" == "stalled" || "$EXEC_STATE" == "dispatch_uncertain" ]]; then
    echo "$CURRENT" | jq .
    echo "FAIL: execution requires inspection; no automatic redispatch was attempted" >&2
    exit 1
  fi
  NOW="$(date +%s)"
  if (( NOW - START >= TIMEOUT_SECONDS )); then
    echo "$CURRENT" | jq .
    echo "FAIL: timeout after ${TIMEOUT_SECONDS}s. Work left intact: $WORK_ID" >&2
    exit 1
  fi
  sleep 2
done
