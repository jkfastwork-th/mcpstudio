#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: $PYTHON missing" >&2
  exit 1
fi
if [[ -z "${M5_OAUTH_REDIRECT_URI:-}" ]]; then
  echo "ERROR: set M5_OAUTH_REDIRECT_URI to the exact Callback URL shown by ChatGPT" >&2
  exit 1
fi
export MCP_STUDIO_CONFIG="${MCP_STUDIO_CONFIG:-$ROOT/config.yaml}"
"$PYTHON" - <<'PY'
import asyncio, os
from mcp_studio.settings import load_settings
from mcp_studio.db import Database
from mcp_studio.oauth import OAuthManager

async def main():
    settings = load_settings(os.environ["MCP_STUDIO_CONFIG"])
    db = Database(settings.studio.database)
    await db.init()
    oauth = OAuthManager(settings, db)
    if not oauth.enabled:
        raise SystemExit("ERROR: studio.oauth_enabled must be true")
    item = await oauth.create_client(
        redirect_uris=[os.environ["M5_OAUTH_REDIRECT_URI"]],
        client_name=os.environ.get("M5_OAUTH_CLIENT_NAME", "ChatGPT Serena MCP"),
        token_endpoint_auth_method="none",
        scopes=["mcp:serena", "offline_access"],
        issue_secret=False,
    )
    print("M5_3_1_OAUTH_CLIENT_CREATED")
    print(f"client_id={item['client_id']}")
    print("client_secret=(none)")
    print("token_endpoint_auth_method=none")
    print("default_scopes=mcp:serena offline_access")
    print(f"redirect_uri={item['redirect_uris'][0]}")

asyncio.run(main())
PY
