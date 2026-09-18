#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -f config.yaml ]] || { echo "ERROR: $ROOT/config.yaml missing" >&2; exit 1; }
[[ -x .venv/bin/python ]] || { echo "ERROR: .venv missing" >&2; exit 1; }

./.venv/bin/python scripts/enable-production-config.py config.yaml
mkdir -p data backups
./.venv/bin/python scripts/backup-production.py

# Fresh installs can seed the root-only environment from the legacy dot-secret
# files. Upgrades may already have removed those files after M6.0; in that case
# keep the existing /etc secret environment intact.
DOT_SECRETS=1
for f in .gateway-token .oauth-signing-secret .oauth-owner-token; do
  [[ -s "$f" ]] || DOT_SECRETS=0
done

sudo install -d -m 700 -o root -g root /etc/mcp-studio
if [[ "$DOT_SECRETS" == "1" ]]; then
  GW="$(cat .gateway-token)"; SIGN="$(cat .oauth-signing-secret)"; OWNER="$(cat .oauth-owner-token)"
  TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
  {
    printf 'MCP_STUDIO_GATEWAY_TOKEN=%s\n' "$GW"
    printf 'MCP_STUDIO_OAUTH_SIGNING_SECRET=%s\n' "$SIGN"
    printf 'MCP_STUDIO_OAUTH_OWNER_TOKEN=%s\n' "$OWNER"
  } > "$TMP"
  sudo install -m 600 -o root -g root "$TMP" /etc/mcp-studio/mcp-studio.env
  echo "production_secrets=seeded_from_legacy_files"
elif sudo test -s /etc/mcp-studio/mcp-studio.env; then
  echo "production_secrets=reusing_existing_root_env"
else
  echo "ERROR: neither legacy dot-secret files nor /etc/mcp-studio/mcp-studio.env are available" >&2
  exit 1
fi

sudo install -m 644 systemd/mcp-studio.service /etc/systemd/system/mcp-studio.service
# Managed Serena child processes inherit MCP Studio's systemd sandbox. Add only
# the explicitly approved project roots as writable carve-outs.
DROPIN_TMP="$(mktemp)"
./.venv/bin/python scripts/render-m6.2.3b-systemd-sandbox.py config.yaml > "$DROPIN_TMP"
sudo install -d -m 755 /etc/systemd/system/mcp-studio.service.d
sudo install -m 644 "$DROPIN_TMP" /etc/systemd/system/mcp-studio.service.d/30-managed-session-roots.conf
rm -f "$DROPIN_TMP"
sudo install -m 644 systemd/mcp-studio-backup.service /etc/systemd/system/mcp-studio-backup.service
sudo install -m 644 systemd/mcp-studio-backup.timer /etc/systemd/system/mcp-studio-backup.timer
sudo install -m 644 systemd/mcp-studio-restore-drill.service /etc/systemd/system/mcp-studio-restore-drill.service
sudo install -m 644 systemd/mcp-studio-restore-drill.timer /etc/systemd/system/mcp-studio-restore-drill.timer
sudo systemctl daemon-reload
sudo systemctl enable mcp-studio.service
sudo systemctl enable --now mcp-studio-backup.timer
sudo systemctl enable --now mcp-studio-restore-drill.timer
sudo systemctl restart mcp-studio.service
sleep 2
sudo systemctl --no-pager --full status mcp-studio.service || true

echo
echo "PRODUCTION_INSTALL_COMPLETE"
echo "Secrets are root-only in /etc/mcp-studio/mcp-studio.env."
echo "M6.2 production observability runs in-process; the M6.1 weekly non-destructive restore drill timer remains enabled."
