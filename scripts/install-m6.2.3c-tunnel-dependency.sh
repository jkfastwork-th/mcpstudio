#!/usr/bin/env bash
set -euo pipefail
SERVICE="${M623C_TUNNEL_SERVICE:-tunnel-client.service}"
DROPIN_DIR="/etc/systemd/system/${SERVICE}.d"
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
cat >"$TMP" <<'EOF'
[Unit]
After=
After=network-online.target mcp-studio.service
Wants=
Wants=network-online.target mcp-studio.service
EOF
sudo install -d -m 755 "$DROPIN_DIR"
sudo install -m 644 "$TMP" "$DROPIN_DIR/20-mcp-studio-cutover.conf"
sudo systemctl daemon-reload
sudo systemctl restart mcp-studio.service
sudo systemctl restart "$SERVICE"
echo "M6_2_3C_TUNNEL_DEPENDENCY_INSTALLED"
systemctl show "$SERVICE" -p After -p Wants --no-pager
