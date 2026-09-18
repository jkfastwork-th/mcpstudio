#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
SERVER_ID="${M5_CF_SERVER_ID:-serena-8001}"
TUNNEL_ID="${M5_CF_TUNNEL_ID:-cf-serena-primary}"
TUNNEL_NAME="${M5_CF_TUNNEL_NAME:-}"
HOSTNAME="${M5_CF_HOSTNAME:-}"
CONFIG_FILE="${M5_CF_CONFIG_FILE:-$HOME/.cloudflared/mcp-studio.yml}"
ORIGIN="${M5_CF_ORIGIN:-http://127.0.0.1:8100}"

[[ -n "$TUNNEL_NAME" ]] || { echo "FAIL: set M5_CF_TUNNEL_NAME"; exit 1; }
[[ -n "$HOSTNAME" ]] || { echo "FAIL: set M5_CF_HOSTNAME"; exit 1; }
[[ -f "$CONFIG_FILE" ]] || { echo "FAIL: config not found: $CONFIG_FILE"; exit 1; }
command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }

PAYLOAD="$(jq -n \
  --arg id "$TUNNEL_ID" \
  --arg name "Cloudflare Serena Primary" \
  --arg endpoint "https://$HOSTNAME/mcp/$SERVER_ID" \
  --arg origin "$ORIGIN" \
  --arg tunnel_name "$TUNNEL_NAME" \
  --arg config_file "$CONFIG_FILE" \
  --arg server_id "$SERVER_ID" \
  '{id:$id,provider:"cloudflare",name:$name,endpoint:$endpoint,origin:$origin,managed:true,autostart:false,auto_reconnect:true,tunnel_name:$tunnel_name,config_file:$config_file,executable:"cloudflared",metadata:{path_scoped:true,server_id:$server_id},enabled:true,desired_state:"running"}')"

curl -fsS -X POST "$BASE/api/connectivity/tunnels" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" | jq .

echo "=== Preflight ==="
curl -fsS "$BASE/api/connectivity/tunnels/$TUNNEL_ID/preflight" | jq .

echo "M5.1_TUNNEL_REGISTERED"
