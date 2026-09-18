#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
status="$(curl -fsS "$BASE/api/status")"
jq -e '(.studio.version == "0.9.4-m6.2.2" or .studio.version == "0.9.5-m6.2.3a" or .studio.version == "0.9.7-m6.2.3b-hotfix" or .studio.version == "0.9.8-m6.2.3c" or .studio.version == "0.9.9-m6.2.4" or .studio.version == "0.9.10-m6.2.5") and .studio.production_mode == true' >/dev/null <<<"$status"
echo M6_2_2_RUNTIME_PASS
jq -e '.operations.schema.current_version >= 5 and .operations.schema.expected_version >= 5 and .operations.schema.integrity == "ok"' >/dev/null <<<"$status"
echo M6_2_2_SCHEMA_V5_PASS
sessions="$(curl -fsS "$BASE/api/gateway/sessions?limit=500")"
jq -e '.tunnel_summary.by_tunnel | type == "object"' >/dev/null <<<"$sessions"
echo M6_2_2_TUNNEL_SUMMARY_PASS
tunnels="$(jq -r '.tunnels[].id' <<<"$status")"
while IFS= read -r tid; do
  [[ -z "$tid" ]] && continue
  curl -fsS "$BASE/api/connectivity/tunnels/$tid/sessions?limit=10" | jq -e --arg tid "$tid" '.tunnel.id == $tid and (.sessions|type=="array") and (.summary|type=="object")' >/dev/null
done <<<"$tunnels"
echo M6_2_2_PER_TUNNEL_API_PASS
# Session attribution must never expose authorization/header values; only host/path/provider/method/confidence.
if jq -e '.. | objects | has("authorization") or has("cf_ray") or has("cf_connecting_ip")' >/dev/null <<<"$sessions"; then
  echo "Sensitive ingress value key detected" >&2; exit 1
fi
echo M6_2_2_ATTRIBUTION_PRIVACY_PASS
printf '\n=============================================\nM6_2_2_TUNNEL_AWARE_SESSION_PASS\n=============================================\n'
