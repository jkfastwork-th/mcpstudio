#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

status="$(curl -fsS "$BASE/api/status")"
jq -e '.studio.production_mode == true and .studio.operations_enabled == true' >/dev/null <<<"$status"
jq -e '.studio.gateway_session_test_mode == false and .studio.resilience_test_mode == false and .studio.connectivity_test_mode == false' >/dev/null <<<"$status"
echo "M6_1_OPERATIONS_RUNTIME_PASS"

ops="$(curl -fsS -X POST "$BASE/api/operations/run")"
jq -e '.supervisor.status == "healthy"' >/dev/null <<<"$ops"
echo "M6_1_RECONCILIATION_PASS"

schema="$(curl -fsS "$BASE/api/operations" | jq '.schema')"
jq -e '.current_version >= 2 and .expected_version >= 2 and .integrity == "ok"' >/dev/null <<<"$schema"
echo "M6_1_SCHEMA_MIGRATION_PASS"

curl -fsS "$BASE/api/audit?limit=5" | jq -e '.audit | type == "array"' >/dev/null
curl -fsS "$BASE/api/alerts?limit=5" | jq -e '.alerts | type == "array"' >/dev/null
echo "M6_1_AUDIT_ALERTS_PASS"

./.venv/bin/python scripts/backup-production.py >/dev/null
./.venv/bin/python scripts/restore-drill.py | tee /tmp/mcp-studio-restore-drill.out >/dev/null
grep -q 'RESTORE_DRILL_PASS' /tmp/mcp-studio-restore-drill.out
echo "M6_1_BACKUP_RESTORE_DRILL_PASS"

systemctl is-enabled --quiet mcp-studio-backup.timer
systemctl is-enabled --quiet mcp-studio-restore-drill.timer
systemctl is-active --quiet mcp-studio-restore-drill.timer
echo "M6_1_SYSTEMD_MAINTENANCE_TIMERS_PASS"

printf '\n=================================\nM6_1_OPERATIONS_HARDENING_PASS\n=================================\n'
