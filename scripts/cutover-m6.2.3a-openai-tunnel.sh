#!/usr/bin/env bash
set -euo pipefail
PROFILE="${M623_OPENAI_PROFILE:-$HOME/.config/tunnel-client/serena.yaml}"
OLD='http://127.0.0.1:8001/mcp'
NEW='http://127.0.0.1:8100/ingress/openai/serena-8001'
[[ -f "$PROFILE" ]] || { echo "FAIL: profile not found: $PROFILE"; exit 1; }
grep -Fq "$OLD" "$PROFILE" || { echo "FAIL: expected old upstream not found; refusing to edit"; exit 1; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${PROFILE}.pre-m6.2.3a-${STAMP}.bak"
cp -a "$PROFILE" "$BACKUP"
python3 - "$PROFILE" "$OLD" "$NEW" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); old=sys.argv[2]; new=sys.argv[3]
s=p.read_text()
if s.count(old) != 1:
    raise SystemExit(f"expected exactly one old upstream occurrence, found {s.count(old)}")
p.write_text(s.replace(old,new))
PY

echo "profile_backup=$BACKUP"
echo "new_upstream=$NEW"
sudo systemctl restart tunnel-client.service
sudo systemctl is-active --quiet tunnel-client.service || { echo "FAIL: tunnel-client did not become active"; exit 1; }
echo "M6_2_3A_OPENAI_TUNNEL_CUTOVER_DONE"
echo "Rollback if needed: sudo cp '$BACKUP' '$PROFILE' && sudo systemctl restart tunnel-client.service"
