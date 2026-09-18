#!/usr/bin/env bash
set -euo pipefail
BASE="${M5_BASE:-http://127.0.0.1:8100}"
WAIT="${M5_3_WAIT_SECONDS:-180}"
INTERVAL="${M5_3_POLL_SECONDS:-2}"
command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }

START="$(date +%s)"
BASELINE="$(curl -fsS "$BASE/api/openai/compatibility" | jq -r '.summary.observed // 0')"
echo "Watching for a new remote MCP client observation for up to ${WAIT}s."
echo "Baseline observations: $BASELINE"
echo "Now trigger ChatGPT/App 'Scan Tools' or use the MCP app in a new chat."

while true; do
  DATA="$(curl -fsS "$BASE/api/openai/compatibility")"
  COUNT="$(echo "$DATA" | jq -r '.summary.observed // 0')"
  if [[ "$COUNT" -gt "$BASELINE" ]]; then
    echo "=== New client observation ==="
    echo "$DATA" | jq '.observations[0]'
    OPENAI="$(echo "$DATA" | jq -r '.observations[0].openai_like // false')"
    SCOPE="$(echo "$DATA" | jq -r '.observations[0].identity_scope // "unknown"')"
    if [[ "$SCOPE" == "connector" || "$SCOPE" == "explicit" ]]; then
      echo "REMOTE_CLIENT_STABLE_IDENTITY_PASS scope=$SCOPE"
    else
      echo "REMOTE_CLIENT_IDENTITY_WARNING scope=$SCOPE"
    fi
    if [[ "$OPENAI" == "true" ]]; then
      echo "OPENAI_CHATGPT_CLIENT_OBSERVED_PASS"
    else
      echo "REMOTE_MCP_CLIENT_OBSERVED_BUT_NOT_CLASSIFIED_OPENAI"
      echo "Inspect client_info_name/user_agent above; no secret header values were stored."
    fi
    exit 0
  fi
  NOW="$(date +%s)"
  if (( NOW - START >= WAIT )); then
    echo "FAIL: no new client observation within ${WAIT}s"
    curl -fsS "$BASE/api/openai/compatibility" | jq '{summary,observations:(.observations[:5])}'
    exit 1
  fi
  sleep "$INTERVAL"
done
