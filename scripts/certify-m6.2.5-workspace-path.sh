#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${MCP_STUDIO_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then PYTHON="${PYTHON_FALLBACK:-python3}"; fi

"$PYTHON" -m pytest -q tests/test_managed_sessions_m623b.py -k \
  'workspace_selector or config_seed_does_not_overwrite_runtime_workspace_registration or workspace_registry_is_confined_to_approved_roots'

echo M6_2_5_WORKSPACE_PATH_REGRESSION_PASS

# Optional live ingress certification. Supply an existing absolute directory under
# managed_session_workspace_roots. For a true auto-registration proof, use a path
# that is not already present in mcpstudio_list_workspaces.
if [[ -n "${MCP_STUDIO_LIVE_WORKSPACE_PATH:-}" ]]; then
  TARGET="$MCP_STUDIO_LIVE_WORKSPACE_PATH"
  [[ "$TARGET" = /* ]] || { echo "live workspace path must be absolute" >&2; exit 2; }
  [[ -d "$TARGET" ]] || { echo "live workspace path does not exist: $TARGET" >&2; exit 2; }
  BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
  INGRESS="${MCP_STUDIO_OPENAI_INGRESS:-$BASE/ingress/openai/serena-8001}"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"m625-workspace-path-cert","version":"0.9.10"}}}'
  curl -fsS -D "$TMP/headers" -o "$TMP/init" -X POST "$INGRESS" \
    -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' --data "$INIT"
  GWS="$(awk 'BEGIN{IGNORECASE=1}/^mcp-session-id:/{gsub("\r","");print $2}' "$TMP/headers" | tail -1)"
  [[ "$GWS" == gws-* ]]
  curl -fsS -o /dev/null -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" \
    --data '{"jsonrpc":"2.0","method":"notifications/initialized"}'

  call_use() {
    local id="$1"
    curl -fsS -X POST "$INGRESS" -H 'Accept: application/json, text/event-stream' \
      -H 'Content-Type: application/json' -H "Mcp-Session-Id: $GWS" \
      --data "$(jq -cn --argjson id "$id" --arg w "$TARGET" '{jsonrpc:"2.0",id:$id,method:"tools/call",params:{name:"mcpstudio_use_workspace",arguments:{workspace:$w}}}')"
  }
  FIRST="$(call_use 2)"; SECOND="$(call_use 3)"
  FIRST_JSON="$(printf '%s\n' "$FIRST" | sed -n 's/^data:[[:space:]]*//p' | tail -1)"; [[ -n "$FIRST_JSON" ]] || FIRST_JSON="$FIRST"
  SECOND_JSON="$(printf '%s\n' "$SECOND" | sed -n 's/^data:[[:space:]]*//p' | tail -1)"; [[ -n "$SECOND_JSON" ]] || SECOND_JSON="$SECOND"
  printf '%s' "$FIRST_JSON" | jq -e '.result.isError == false' >/dev/null
  printf '%s' "$SECOND_JSON" | jq -e '.result.isError == false' >/dev/null
  FIRST_TEXT="$(printf '%s' "$FIRST_JSON" | jq -r '.result.content[0].text')"
  SECOND_TEXT="$(printf '%s' "$SECOND_JSON" | jq -r '.result.content[0].text')"
  FIRST_ID="$(printf '%s' "$FIRST_TEXT" | jq -r '.managed_session.id')"
  SECOND_ID="$(printf '%s' "$SECOND_TEXT" | jq -r '.managed_session.id')"
  FIRST_PATH="$(printf '%s' "$FIRST_TEXT" | jq -r '.managed_session.project_path')"
  [[ -n "$FIRST_ID" && "$FIRST_ID" == "$SECOND_ID" ]]
  [[ "$(realpath "$TARGET")" == "$FIRST_PATH" ]]
  echo M6_2_5_WORKSPACE_PATH_LIVE_REUSE_PASS
fi

echo M6_2_5_WORKSPACE_PATH_CERTIFICATION_PASS
