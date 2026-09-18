#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
export MCP_STUDIO_CONFIG="${MCP_STUDIO_CONFIG:-$ROOT/config.yaml}"
"$PYTHON" - <<'PY'
import asyncio, os
from mcp_studio.settings import load_settings
from mcp_studio.db import Database
from mcp_studio.oauth import OAuthManager
async def main():
    s=load_settings(os.environ['MCP_STUDIO_CONFIG'])
    db=Database(s.studio.database)
    await db.init()
    o=OAuthManager(s,db)
    print(f"Server URL: {o.resource}")
    print("Authentication: OAuth")
    print("Registration method: User-Defined OAuth Client")
    clients=await db.list_oauth_clients()
    if not clients:
        print("Client ID: <none registered>")
    else:
        for c in clients:
            if c['enabled']:
                print(f"Client ID: {c['client_id']}")
                print("Client Secret: <blank>")
                print(f"Token endpoint auth method: {c['token_endpoint_auth_method']}")
                print("Default scopes: mcp:serena offline_access")
                print("Redirect URI(s): " + ", ".join(c['redirect_uris']))
                break
    print(f"Authorization metadata: {o.issuer}/.well-known/oauth-authorization-server")
    print(f"Protected resource metadata: {o.protected_resource_metadata_url}")
asyncio.run(main())
PY
