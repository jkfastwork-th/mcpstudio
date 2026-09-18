#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
ID="${M623_OPENAI_INGRESS_ID:-openai-serena}"
SERVER_ID="${M623_SERVER_ID:-serena-8001}"
ENDPOINT="${M623_OPENAI_INGRESS_ENDPOINT:-http://127.0.0.1:8100/ingress/openai/$SERVER_ID}"
ORIGIN="${M623_OPENAI_ORIGIN:-http://127.0.0.1:8001/mcp}"

payload="$(jq -cn \
  --arg id "$ID" --arg endpoint "$ENDPOINT" --arg origin "$ORIGIN" --arg sid "$SERVER_ID" \
  '{id:$id,provider:"openai",name:"OpenAI Secure Tunnel · Serena",endpoint:$endpoint,origin:$origin,enabled:true,managed:false,autostart:false,auto_reconnect:false,metadata:{server_id:$sid,management_mode:"external_remote",process_owner:"systemd/tunnel-client.service",local_only:true}}')"

curl -fsS -X POST "$BASE/api/connectivity/tunnels" \
  -H 'Content-Type: application/json' --data "$payload" \
  | jq '{id,provider,name,endpoint,origin,managed,metadata}'

echo "M6_2_3A_OPENAI_INGRESS_REGISTERED"
