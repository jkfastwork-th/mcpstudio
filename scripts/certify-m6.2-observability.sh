#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

status="$(curl -fsS "$BASE/api/status")"
jq -e '.studio.production_mode == true and .studio.observability_enabled == true' >/dev/null <<<"$status"
jq -e '.studio.gateway_session_test_mode == false and .studio.resilience_test_mode == false and .studio.connectivity_test_mode == false' >/dev/null <<<"$status"
echo "M6_2_OBSERVABILITY_RUNTIME_PASS"

report="$(curl -fsS -X POST "$BASE/api/observability/run")"
jq -e '.manager.status == "healthy" or .manager.status == "degraded"' >/dev/null <<<"$report"
jq -e '.window_minutes >= 5 and (.objectives | type == "object") and (.metrics | type == "object")' >/dev/null <<<"$report"
echo "M6_2_METRICS_SAMPLING_PASS"

jq -e '.objectives.availability and .objectives.mcp_success and .objectives.queue_p95_ms and .objectives.worker_saturation and .objectives.reconnect_rate and .objectives.oauth_refresh_failures and .objectives.orphan_events' >/dev/null <<<"$report"
jq -e '[.objectives[].state] | all(. == "healthy" or . == "degraded" or . == "down" or . == "unknown")' >/dev/null <<<"$report"
echo "M6_2_SLO_EVALUATION_PASS"

schema="$(curl -fsS "$BASE/api/operations" | jq '.schema')"
jq -e '.current_version >= 3 and .expected_version >= 3 and .integrity == "ok"' >/dev/null <<<"$schema"
echo "M6_2_SCHEMA_COMPAT_PASS"

# Refresh the non-destructive restore evidence so the SLO page can surface it.
./.venv/bin/python scripts/backup-production.py >/dev/null
./.venv/bin/python scripts/restore-drill.py | tee /tmp/mcp-studio-m6.2-restore.out >/dev/null
grep -q 'RESTORE_DRILL_PASS' /tmp/mcp-studio-m6.2-restore.out
report="$(curl -fsS -X POST "$BASE/api/observability/run")"
jq -e '.restore_drill.status == "pass"' >/dev/null <<<"$report"
echo "M6_2_RESTORE_SIGNAL_PASS"

curl -fsS "$BASE/api/alerts?limit=50" | jq -e '.alerts | type == "array"' >/dev/null
curl -fsS "$BASE/api/audit?limit=20" | jq -e '.audit | type == "array"' >/dev/null
echo "M6_2_ALERT_PIPELINE_PASS"

systemctl is-active --quiet mcp-studio
systemctl is-enabled --quiet mcp-studio-backup.timer
systemctl is-enabled --quiet mcp-studio-restore-drill.timer
echo "M6_2_PRODUCTION_SERVICE_PASS"

printf '\n=================================\nM6_2_OBSERVABILITY_SLO_PASS\n=================================\n'
