#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export MCP_STUDIO_CONFIG="${MCP_STUDIO_CONFIG:-$ROOT/config.yaml}"
PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "ERROR: virtualenv missing: $PYTHON" >&2; exit 1; }
readarray -t LISTEN < <("$PYTHON" - <<'PY'
import os
from mcp_studio.settings import load_settings
s=load_settings(os.environ['MCP_STUDIO_CONFIG'])
print(s.studio.bind_host)
print(s.studio.bind_port)
print('true' if s.studio.production_mode else 'false')
PY
)
HOST="${LISTEN[0]}"; PORT="${LISTEN[1]}"; PROD="${LISTEN[2]}"
[[ "$PROD" == "true" ]] || { echo "ERROR: production_mode must be true" >&2; exit 1; }
echo "MCP Studio production starting on http://${HOST}:${PORT}"
exec "$PYTHON" -m uvicorn mcp_studio.main:app \
  --host "$HOST" --port "$PORT" --workers 1 \
  --proxy-headers --forwarded-allow-ips="127.0.0.1,::1" \
  --no-access-log
