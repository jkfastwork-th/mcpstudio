#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BASE="${M5_LOCAL_BASE:-http://127.0.0.1:8100}"
POLL="${M5_3_2_POLL_SECONDS:-2}"
WAIT="${M5_3_2_WAIT_SECONDS:-180}"

json() { curl -fsS "$@"; }
latest_chatgpt() { json "$BASE/api/certification/m5.3.2" | jq -c '.chatgpt_sessions[0] // empty'; }
wait_for() {
  local desc="$1" start now row expr
  shift
  local -a jq_args=()
  while (( $# > 1 )); do
    jq_args+=("$1")
    shift
  done
  expr="$1"
  start="$(date +%s)"
  while true; do
    row="$(json "$BASE/api/certification/m5.3.2")"
    if jq -e "${jq_args[@]}" "$expr" >/dev/null <<<"$row"; then printf '%s\n' "$row"; return 0; fi
    now="$(date +%s)"; (( now-start < WAIT )) || { echo "TIMEOUT: $desc" >&2; return 1; }
    sleep "$POLL"
  done
}

printf '%s\n' '=== M5.3.2 Real ChatGPT Reconnect Certification ==='
STATUS="$(json "$BASE/api/certification/m5.3.2")"
jq '{milestone,version,gateway_session_test_mode,gateway_session_auto_reconnect,gateway_session_replay_on_404,openai_connector_reclaim_enabled,oauth}' <<<"$STATUS"
jq -e '.gateway_session_auto_reconnect == true and .gateway_session_replay_on_404 == true and .openai_connector_reclaim_enabled == true' >/dev/null <<<"$STATUS" || {
  echo 'FAIL: reconnect/replay/reclaim safety settings are not enabled' >&2; exit 1;
}

BEFORE_EXEC="$(json "$BASE/api/status" | jq -c '{workers,work,execution}')"
BASE_TS="$(date -u +%Y-%m-%dT%H:%M:%S)"
echo
printf '%s\n' 'STEP 1/4 — In ChatGPT, run any Serena MCP tool now (for example: list panes).'
R1="$(wait_for 'new ChatGPT MCP session' --arg ts "$BASE_TS" '.chatgpt_sessions | any(.last_seen_at >= $ts)')"
S1="$(jq -c --arg ts "$BASE_TS" '.chatgpt_sessions | map(select(.last_seen_at >= $ts)) | sort_by(.last_seen_at) | last' <<<"$R1")"
GWS1="$(jq -r '.gateway_session_id' <<<"$S1")"; STUDIO1="$(jq -r '.studio_session_id' <<<"$S1")"; CLIENT1="$(jq -r '.client_id' <<<"$S1")"
echo "$S1" | jq '{gateway_session_id,studio_session_id,client_id,status,generation,reconnect_count,reclaimed_studio_session,identity_scope,client_info_name}'
echo "REAL_CHATGPT_SESSION_OBSERVED_PASS gateway=$GWS1 studio=$STUDIO1"

TEST_MODE="$(jq -r '.gateway_session_test_mode' <<<"$STATUS")"
if [[ "$TEST_MODE" == "true" ]]; then
  echo
  printf '%s\n' 'STEP 2/4 — Injecting loss of ONLY the upstream Serena MCP session.'
  json -X POST "$BASE/api/gateway/sessions/$GWS1/fault/drop-upstream" | jq .
  GEN1="$(jq -r '.generation' <<<"$S1")"
  printf '%s\n' 'Now run another Serena MCP tool in the SAME ChatGPT connection.'
  R2="$(wait_for 'same gateway session auto-heal' --arg g "$GWS1" --argjson gen "$GEN1" '.chatgpt_sessions | any(.gateway_session_id==$g and .generation>$gen and .reconnect_count>=1)')"
  S2="$(jq -c --arg g "$GWS1" '.chatgpt_sessions[] | select(.gateway_session_id==$g)' <<<"$R2")"
  echo "$S2" | jq '{gateway_session_id,studio_session_id,generation,reconnect_count,last_seen_at}'
  echo "REAL_CHATGPT_UPSTREAM_AUTO_HEAL_PASS gateway=$GWS1"
else
  echo
  echo 'STEP 2/4 — SKIP controlled upstream-drop: gateway_session_test_mode=false.'
  echo 'For full M5.3.2 certification, temporarily set gateway_session_test_mode: true, restart Studio, and rerun this script.'
fi

echo
printf '%s\n' 'STEP 3/4 — Create a NEW ChatGPT MCP transport now: use Scan Tools/reconnect the app, then run a Serena tool again.'
R3="$(wait_for 'new ChatGPT gateway transport reclaiming same Studio session' --arg old "$GWS1" --arg studio "$STUDIO1" --arg client "$CLIENT1" '.chatgpt_sessions | any(.gateway_session_id!=$old and .studio_session_id==$studio and .client_id==$client and .reclaimed_studio_session==true)')"
S3="$(jq -c --arg old "$GWS1" --arg studio "$STUDIO1" --arg client "$CLIENT1" '.chatgpt_sessions | map(select(.gateway_session_id!=$old and .studio_session_id==$studio and .client_id==$client and .reclaimed_studio_session==true)) | sort_by(.last_seen_at) | last' <<<"$R3")"
echo "$S3" | jq '{gateway_session_id,studio_session_id,client_id,reclaimed_studio_session,identity_scope,created_at,last_seen_at}'
echo "REAL_CHATGPT_LOGICAL_SESSION_RECLAIM_PASS studio=$STUDIO1"

AFTER_EXEC="$(json "$BASE/api/status" | jq -c '{workers,work,execution}')"
[[ "$AFTER_EXEC" == "$BEFORE_EXEC" ]] || {
  echo 'FAIL: ChatGPT transport recovery changed worker/work/execution state' >&2
  echo "BEFORE=$BEFORE_EXEC" >&2; echo "AFTER=$AFTER_EXEC" >&2; exit 1;
}
echo 'EXECUTION_ISOLATION_PASS'

echo
printf '%s\n' 'STEP 4/4 — OAuth refresh evidence.'
OAUTH="$(json "$BASE/api/certification/m5.3.2" | jq '.oauth')"
echo "$OAUTH" | jq .
if [[ "$(jq -r '.refresh_tokens_rotated' <<<"$OAUTH")" -ge 1 ]]; then
  echo 'OAUTH_REFRESH_ROTATION_OBSERVED_PASS'
else
  echo 'OAUTH_REFRESH_ROTATION_PENDING'
  echo 'No refresh grant has been observed yet. This does not invalidate reconnect/reclaim; leave the connector running until ChatGPT refreshes the token, then rerun scripts/check-m5.3.2-oauth-refresh.sh.'
fi

echo '============================================='
if [[ "$TEST_MODE" == "true" ]]; then
  echo 'M5_3_2_REAL_CHATGPT_RECONNECT_PASS'
else
  echo 'M5_3_2_REAL_CHATGPT_RECONNECT_PARTIAL_PASS'
fi
echo '============================================='
