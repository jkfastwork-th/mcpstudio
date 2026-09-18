#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BASE="${M5_LOCAL_BASE:-http://127.0.0.1:8100}"
PYTHON="$ROOT/.venv/bin/python"
export MCP_STUDIO_CONFIG="${MCP_STUDIO_CONFIG:-$ROOT/config.yaml}"

readarray -t PUBLIC < <("$PYTHON" - <<'PY'
import os
from mcp_studio.settings import load_settings
from mcp_studio.oauth import OAuthManager
from mcp_studio.db import Database
s=load_settings(os.environ['MCP_STUDIO_CONFIG'])
o=OAuthManager(s, Database(s.studio.database))
print(o.issuer)
print(o.resource)
print(o.protected_resource_metadata_url)
PY
)
ISSUER="${PUBLIC[0]}"
RESOURCE="${PUBLIC[1]}"
PRM="${PUBLIC[2]}"

echo "=== M5.3.1 OAuth local safety state ==="
curl -fsS "$BASE/api/status" | jq '.studio | {version,gateway_enabled,gateway_session_enabled,openai_compatibility_enabled,oauth_enabled,oauth_issuer,oauth_resource,oauth_owner_token_configured}'

ENABLED="$(curl -fsS "$BASE/api/status" | jq -r '.studio.oauth_enabled')"
[[ "$ENABLED" == "true" ]] || { echo "FAIL: oauth_enabled is not true" >&2; exit 1; }
OWNER="$(curl -fsS "$BASE/api/status" | jq -r '.studio.oauth_owner_token_configured')"
[[ "$OWNER" == "true" ]] || { echo "FAIL: OAuth owner token is not configured in Studio process" >&2; exit 1; }
CLIENTS="$(curl -fsS "$BASE/api/oauth/status" | jq '.clients | length')"
[[ "$CLIENTS" -ge 1 ]] || { echo "FAIL: no OAuth client registered; run scripts/create-m5.3.1-oauth-client.sh" >&2; exit 1; }
echo "OAUTH_LOCAL_CONFIG_PASS clients=$CLIENTS"

echo "=== Public authorization-server metadata ==="
curl -fsS "$ISSUER/.well-known/oauth-authorization-server" | tee /tmp/m5oauth-as.json | jq '{issuer,authorization_endpoint,token_endpoint,response_types_supported,grant_types_supported,code_challenge_methods_supported,token_endpoint_auth_methods_supported,scopes_supported}'
jq -e --arg issuer "$ISSUER" '.issuer==$issuer and (.grant_types_supported|index("refresh_token")) != null and (.code_challenge_methods_supported|index("S256")) != null and (.scopes_supported|index("offline_access")) != null' /tmp/m5oauth-as.json >/dev/null || { echo "FAIL: authorization metadata mismatch" >&2; exit 1; }
echo "OAUTH_AUTHORIZATION_METADATA_PASS"

echo "=== Public protected-resource metadata ==="
curl -fsS "$PRM" | tee /tmp/m5oauth-prm.json | jq '{resource,authorization_servers,scopes_supported,bearer_methods_supported}'
jq -e --arg resource "$RESOURCE" --arg issuer "$ISSUER" '.resource==$resource and (.authorization_servers|index($issuer)) != null and (.scopes_supported|index("mcp:serena")) != null' /tmp/m5oauth-prm.json >/dev/null || { echo "FAIL: protected resource metadata mismatch" >&2; exit 1; }
echo "OAUTH_PROTECTED_RESOURCE_METADATA_PASS"

echo "=== Public MCP challenge ==="
HDR=/tmp/m5oauth-headers.txt
CODE="$(curl -sS -D "$HDR" -o /tmp/m5oauth-body.txt -w '%{http_code}' "$RESOURCE")"
echo "unauth HTTP=$CODE"
[[ "$CODE" == "401" ]] || { echo "FAIL: unauthenticated resource expected HTTP 401" >&2; cat /tmp/m5oauth-body.txt >&2; exit 1; }
grep -qi 'www-authenticate:.*resource_metadata=' "$HDR" || { echo "FAIL: WWW-Authenticate resource_metadata challenge missing" >&2; cat "$HDR" >&2; exit 1; }
echo "OAUTH_RESOURCE_CHALLENGE_PASS"

echo "=== Public UI remains out of tunnel surface ==="
ROOT_CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$ISSUER/")"
echo "root HTTP=$ROOT_CODE"
[[ "$ROOT_CODE" == "404" ]] || { echo "FAIL: public root must remain 404" >&2; exit 1; }
echo "PUBLIC_UI_ISOLATION_PASS"

echo "================================="
echo "M5_3_1_OAUTH_SERVER_READY_PASS"
echo "================================="
