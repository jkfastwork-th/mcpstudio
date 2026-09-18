#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
status="$(curl -fsS "$BASE/api/status")"
jq -e '.studio.production_mode == true' >/dev/null <<<"$status"
jq -e '.studio.gateway_enabled == true' >/dev/null <<<"$status"
jq -e '.studio.gateway_session_enabled == true and .studio.gateway_session_auto_reconnect == true' >/dev/null <<<"$status"
jq -e '.studio.gateway_session_test_mode == false and .studio.resilience_test_mode == false and .studio.connectivity_test_mode == false' >/dev/null <<<"$status"
jq -e '.studio.oauth_enabled == true and .studio.oauth_owner_token_configured == true' >/dev/null <<<"$status"
echo "PRODUCTION_RUNTIME_FLAGS_PASS"
curl -fsS "$BASE/healthz" | jq -e '.ok == true and .production_mode == true' >/dev/null
echo "PRODUCTION_HEALTH_PASS"
# Fault routes must disappear at the route boundary in production.
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/gateway/sessions/does-not-exist/fault/drop-upstream")"
[[ "$code" == "404" ]] || { echo "Expected gateway fault route 404, got $code" >&2; exit 1; }
code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/connectivity/tunnels/does-not-exist/fault/terminate")"
[[ "$code" == "404" ]] || { echo "Expected tunnel fault route 404, got $code" >&2; exit 1; }
echo "PRODUCTION_FAULT_SURFACE_DISABLED_PASS"
issuer="$(jq -r '.studio.oauth_issuer' <<<"$status")"
resource="$(jq -r '.studio.oauth_resource' <<<"$status")"
curl -fsS "$issuer/.well-known/oauth-authorization-server" | jq -e '.authorization_endpoint and .token_endpoint' >/dev/null
echo "PRODUCTION_OAUTH_METADATA_PASS"
unauth="$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$resource")"
[[ "$unauth" == "401" ]] || { echo "Expected public MCP unauthenticated 401, got $unauth" >&2; exit 1; }
echo "PRODUCTION_PUBLIC_MCP_AUTH_PASS"
rootcode="$(curl -sS -o /dev/null -w '%{http_code}' "$issuer/")"
[[ "$rootcode" == "404" ]] || echo "WARN: public root returned HTTP $rootcode (expected path-scoped 404)" >&2
printf '\n=================================\nM6_0_PRODUCTION_BASELINE_PASS\n=================================\n'
