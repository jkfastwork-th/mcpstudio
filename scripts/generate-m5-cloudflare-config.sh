#!/usr/bin/env bash
set -euo pipefail

SERVER_ID="${M5_CF_SERVER_ID:-serena-8001}"
TUNNEL_UUID="${M5_CF_TUNNEL_UUID:-}"
TUNNEL_NAME="${M5_CF_TUNNEL_NAME:-}"
HOSTNAME="${M5_CF_HOSTNAME:-}"
CREDENTIALS="${M5_CF_CREDENTIALS_FILE:-}"
CONFIG_FILE="${M5_CF_CONFIG_FILE:-$HOME/.cloudflared/mcp-studio.yml}"
ORIGIN="${M5_CF_ORIGIN:-http://127.0.0.1:8100}"

[[ -n "$TUNNEL_UUID" ]] || { echo "FAIL: set M5_CF_TUNNEL_UUID"; exit 1; }
[[ -n "$TUNNEL_NAME" ]] || { echo "FAIL: set M5_CF_TUNNEL_NAME"; exit 1; }
[[ -n "$HOSTNAME" ]] || { echo "FAIL: set M5_CF_HOSTNAME (hostname only, no https://)"; exit 1; }
[[ -n "$CREDENTIALS" ]] || CREDENTIALS="$HOME/.cloudflared/$TUNNEL_UUID.json"
[[ -f "$CREDENTIALS" ]] || { echo "FAIL: credentials file not found: $CREDENTIALS"; exit 1; }
command -v cloudflared >/dev/null || { echo "FAIL: cloudflared is not installed/in PATH"; exit 1; }

mkdir -p "$(dirname "$CONFIG_FILE")"
umask 077
cat > "$CONFIG_FILE" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $CREDENTIALS

ingress:
  - hostname: $HOSTNAME
    path: ^/mcp/$SERVER_ID/?$
    service: $ORIGIN
  - service: http_status:404
EOF
chmod 600 "$CONFIG_FILE"

echo "=== Generated path-scoped Cloudflare config ==="
cat "$CONFIG_FILE"
echo

echo "=== cloudflared ingress validate ==="
cloudflared tunnel --config "$CONFIG_FILE" ingress validate

echo "=== cloudflared ingress rule (MCP path) ==="
cloudflared tunnel --config "$CONFIG_FILE" ingress rule "https://$HOSTNAME/mcp/$SERVER_ID"

echo "=== cloudflared ingress rule (Studio UI must hit 404 catch-all) ==="
cloudflared tunnel --config "$CONFIG_FILE" ingress rule "https://$HOSTNAME/"

echo
cat <<EOF
M5.1_CONFIG_READY

Next, create/update the DNS route (this mutates Cloudflare DNS):
  cloudflared tunnel route dns $TUNNEL_NAME $HOSTNAME

Then register the tunnel with Studio:
  export M5_CF_TUNNEL_NAME='$TUNNEL_NAME'
  export M5_CF_HOSTNAME='$HOSTNAME'
  export M5_CF_CONFIG_FILE='$CONFIG_FILE'
  ./scripts/register-m5-cloudflare.sh
EOF
