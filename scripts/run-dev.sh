#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
fi

export MCP_STUDIO_CONFIG="${MCP_STUDIO_CONFIG:-$ROOT/config.yaml}"

# Load local secret files when the corresponding environment variables are not
# already set. Files are intentionally outside config.yaml so secrets do not
# end up in copied configuration or logs.
if [[ -z "${MCP_STUDIO_GATEWAY_TOKEN:-}" && -f "$ROOT/.gateway-token" ]]; then
  export MCP_STUDIO_GATEWAY_TOKEN="$(cat "$ROOT/.gateway-token")"
fi
if [[ -z "${MCP_STUDIO_OAUTH_SIGNING_SECRET:-}" && -f "$ROOT/.oauth-signing-secret" ]]; then
  export MCP_STUDIO_OAUTH_SIGNING_SECRET="$(cat "$ROOT/.oauth-signing-secret")"
fi
if [[ -z "${MCP_STUDIO_OAUTH_OWNER_TOKEN:-}" && -f "$ROOT/.oauth-owner-token" ]]; then
  export MCP_STUDIO_OAUTH_OWNER_TOKEN="$(cat "$ROOT/.oauth-owner-token")"
fi

PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: virtualenv not found: $PYTHON" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'" >&2
  exit 1
fi

readarray -t LISTEN < <("$PYTHON" - <<'PY'
import os
from mcp_studio.settings import load_settings
s = load_settings(os.environ['MCP_STUDIO_CONFIG'])
print(s.studio.bind_host)
print(s.studio.bind_port)
PY
)
HOST="${LISTEN[0]}"
PORT="${LISTEN[1]}"

echo "MCP Studio starting on http://${HOST}:${PORT}"
exec "$PYTHON" -m uvicorn mcp_studio.main:app --host "$HOST" --port "$PORT"
