#!/usr/bin/env bash
set -euo pipefail

BASE="${M5_BASE:-http://127.0.0.1:8100}"
SERVER_ID="${M5_CF_SERVER_ID:-serena-8001}"
TUNNEL_ID="${M5_CF_TUNNEL_ID:-cf-serena-existing}"
TUNNEL_NAME="${M5_CF_TUNNEL_NAME:-Existing Cloudflare Tunnel}"
HOSTNAME="${M5_CF_HOSTNAME:-}"
ORIGIN="${M5_CF_ORIGIN:-http://127.0.0.1:8100}"

[[ -n "$HOSTNAME" ]] || { echo "FAIL: set M5_CF_HOSTNAME"; exit 1; }
command -v jq >/dev/null || { echo "FAIL: jq is required"; exit 1; }

PAYLOAD="$(jq -n \
  --arg id "$TUNNEL_ID" \
  --arg name "$TUNNEL_NAME" \
  --arg endpoint "https://$HOSTNAME/mcp/$SERVER_ID" \
  --arg origin "$ORIGIN" \
  --arg server_id "$SERVER_ID" \
  '{id:$id,provider:"cloudflare",name:$name,endpoint:$endpoint,origin:$origin,health_url:$endpoint,managed:false,autostart:false,auto_reconnect:false,metadata:{path_scoped:true,server_id:$server_id,management_mode:"external_remote",process_owner:"systemd/cloudflare"},enabled:true,desired_state:"running"}')"

curl -fsS -X POST "$BASE/api/connectivity/tunnels" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" | jq .

curl -fsS -X POST "$BASE/api/connectivity/run" | jq --arg id "$TUNNEL_ID" '.tunnels[] | select(.id==$id) | {id,provider,name,endpoint,origin,managed,status,error,metadata}'

echo "M5_1_EXISTING_TUNNEL_REGISTERED"
