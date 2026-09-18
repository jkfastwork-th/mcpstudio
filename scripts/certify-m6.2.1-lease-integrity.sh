#!/usr/bin/env bash
set -euo pipefail
BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"

status="$(curl -fsS "$BASE/api/status")"
jq -e '.studio.production_mode == true and (.operations.schema.current_version >= 4)' >/dev/null <<<"$status"
echo "M6_2_1_RUNTIME_PASS"

schema="$(curl -fsS "$BASE/api/operations" | jq '.schema')"
jq -e '.current_version >= 4 and .expected_version >= 4 and .integrity == "ok"' >/dev/null <<<"$schema"
echo "M6_2_1_SCHEMA_V4_PASS"

# Run the conservative local reconciliation once so legacy idle leases from
# pre-M6.2.1 are removed before checking invariants.
curl -fsS -X POST "$BASE/api/operations/run" >/dev/null
workers="$(curl -fsS "$BASE/api/workers")"
running="$(curl -fsS "$BASE/api/work?state=running&limit=1000")"

# No active write lease may be owned by an idle/dead worker or by a worker
# bound to another workspace. Degraded is intentionally valid while work is
# in flight; dropping that lock was the collision bug fixed in M6.2.1.
jq -e '
  . as $root |
  [ .leases[] as $l |
    ($root.workers[] | select(.id == $l.worker_id)) as $w |
    select(
      ($w.state == "idle") or ($w.state == "dead") or
      ($w.workspace != $l.workspace) or ($w.lease_mode != "write")
    )
  ] | length == 0
' >/dev/null <<<"$workers"
echo "M6_2_1_NO_IDLE_OR_MISMATCHED_LEASE_PASS"

# Work-owned leases must map to exactly one running work row on the same
# worker/workspace. Manual busy leases have work_id=null and are exempt.
jq -n -e --argjson wd "$workers" --argjson rd "$running" '
  [ $wd.leases[] | select(.work_id != null) as $l |
    [ $rd.work[] | select(.id == $l.work_id and .worker_id == $l.worker_id and .workspace == $l.workspace) ] | length
  ] | all(. == 1)
' >/dev/null
echo "M6_2_1_WORK_OWNERSHIP_PASS"

# A degraded running worker must retain the lease. This check is passive: if
# there are no degraded workers, it vacuously passes.
jq -n -e --argjson wd "$workers" --argjson rd "$running" '
  [ $wd.workers[] | select(.state == "degraded" and .lease_mode == "write") as $w |
    [ $rd.work[] | select(.worker_id == $w.id and .workspace == $w.workspace) ] as $rw |
    if ($rw|length) > 0 then
      [ $wd.leases[] | select(.worker_id == $w.id and .workspace == $w.workspace) ] | length == 1
    else true end
  ] | all
' >/dev/null
echo "M6_2_1_DEGRADED_FENCING_PASS"

printf '\n=================================\nM6_2_1_LEASE_INTEGRITY_PASS\n=================================\n'
