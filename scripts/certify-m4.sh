#!/usr/bin/env bash
set -euo pipefail

BASE="${MCP_STUDIO_URL:-http://127.0.0.1:8100}"
PROMPT_PREFIX="MCP Studio M4 certification only. Do not modify files, run commands, commit, or change external state. Reply exactly"

jget() { curl -fsS "$@"; }
post_json() {
  local url="$1" body="$2"
  curl -fsS -X POST "$url" -H 'Content-Type: application/json' -d "$body"
}

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

echo "=== M4 live certification: preflight ==="
STATUS="$(jget "$BASE/api/status")"
echo "$STATUS" | jq '{version:.studio.version, workers:.workers, work:.work, execution:.execution, resilience_test_mode:.studio.resilience_test_mode}'

[[ "$(echo "$STATUS" | jq -r '.studio.version')" == "0.5.3-m4" ]] || fail "MCP Studio v0.5.3-m4 is not running"
[[ "$(echo "$STATUS" | jq -r '.workers.counts.busy // 0')" == "0" ]] || fail "busy workers exist; certification will not disturb them"
[[ "$(echo "$STATUS" | jq -r '.workers.bound // 0')" == "0" ]] || fail "bound workers exist; certification will not disturb them"
[[ "$(echo "$STATUS" | jq -r '.work.active // 0')" == "0" ]] || fail "active work exists"
[[ "$(echo "$STATUS" | jq -r '.work.queue // 0')" == "0" ]] || fail "queued work exists"
[[ "$(echo "$STATUS" | jq -r '.studio.resilience_test_mode')" == "true" ]] || fail "set studio.resilience_test_mode: true in config.yaml, restart Studio, then rerun M4 certification"

echo "=== Refresh Herdr and choose 4 idle/done agent panes ==="
HERDR="$(post_json "$BASE/api/herdr/refresh" '{}')"
echo "$HERDR" | jq '{status,agent_count,pane_count,error}'
[[ "$(echo "$HERDR" | jq -r '.status')" == "healthy" ]] || fail "Herdr is not healthy"

mapfile -t TARGETS < <(echo "$HERDR" | jq -r '
  [.panes.panes[]
   | select(.agent != null)
   | select((.agent_status == "idle") or (.agent_status == "done"))]
  | unique_by(.cwd)
  | .[:4][]
  | [.pane_id,.agent,.cwd] | @tsv')

[[ "${#TARGETS[@]}" -eq 4 ]] || {
  echo "$HERDR" | jq '.panes.panes[] | {pane_id,agent,agent_status,cwd}'
  fail "need 4 idle/done agent panes on 4 distinct workspaces; found ${#TARGETS[@]}"
}

for i in "${!TARGETS[@]}"; do
  IFS=$'\t' read -r pane agent workspace <<< "${TARGETS[$i]}"
  printf 'target[%d] pane=%s agent=%s workspace=%s\n' "$((i+1))" "$pane" "$agent" "$workspace"
done

submit_work() {
  local label="$1" pane="$2" agent="$3" workspace="$4" reply="$5" priority="${6:-50}"
  local body
  body="$(jq -nc \
    --arg label "$label" --arg pane "$pane" --arg agent "$agent" \
    --arg workspace "$workspace" --arg instruction "$PROMPT_PREFIX $reply" \
    --argjson priority "$priority" \
    '{label:$label,workspace:$workspace,priority:$priority,lease_mode:"write",pane:$pane,agent:$agent,dispatch_mode:"herdr",instruction:$instruction,metadata:{certification:"M4-live"}}')"
  post_json "$BASE/api/work" "$body" | jq -r '.id'
}

WORK_IDS=()
for i in 0 1 2 3; do
  IFS=$'\t' read -r pane agent workspace <<< "${TARGETS[$i]}"
  id="$(submit_work "M4 parallel $((i+1))" "$pane" "$agent" "$workspace" "M4_CERT_$((i+1))" $((80-i)))"
  WORK_IDS+=("$id")
  echo "created $id"
done

# A fifth writer targets the first workspace and must remain queued while its
# first job owns the workspace lease.
IFS=$'\t' read -r pane1 agent1 workspace1 <<< "${TARGETS[0]}"
DUP_ID="$(submit_work "M4 workspace serialization" "$pane1" "$agent1" "$workspace1" "M4_SERIAL_OK" 100)"
echo "created duplicate workspace work $DUP_ID"

post_json "$BASE/api/scheduler/run" '{}' >/dev/null
sleep 0.2

echo "=== Verify 4 workers active + duplicate workspace queued ==="
RUNNING=0
for id in "${WORK_IDS[@]}"; do
  state="$(jget "$BASE/api/work/$id" | jq -r '.state')"
  echo "$id $state"
  [[ "$state" == "running" ]] && RUNNING=$((RUNNING+1))
done
[[ "$RUNNING" -eq 4 ]] || fail "expected four parallel running jobs, got $RUNNING"
DUP_STATE="$(jget "$BASE/api/work/$DUP_ID" | jq -r '.state')"
echo "$DUP_ID $DUP_STATE"
[[ "$DUP_STATE" == "queued" ]] || fail "same-workspace fifth job must be queued while lease is held"

echo "=== Explicit parallel dispatch of the four workers ==="
IDS_JSON="$(printf '%s\n' "${WORK_IDS[@]}" | jq -R . | jq -s .)"
BATCH="$(post_json "$BASE/api/execution/dispatch-ready" "$(jq -nc --argjson ids "$IDS_JSON" '{work_ids:$ids}')")"
echo "$BATCH" | jq .
[[ "$(echo "$BATCH" | jq -r '.succeeded')" == "4" ]] || fail "all four parallel dispatches did not succeed"
PEAK="$(echo "$BATCH" | jq -r '.peak_parallel_dispatches // 0')"
[[ "$PEAK" -ge 2 ]] || fail "parallel dispatch was not observed (peak=$PEAK)"

# M4 certifies the Studio execution fabric, not the upstream model provider's
# concurrency quota. M3 already certified a full prompt -> completion path.
# Here we require four Herdr prompt submissions to be accepted concurrently,
# then test lease isolation and reconnect behavior without waiting for all four
# models to finish. This avoids false failures when a provider/account reaches
# a temporary model concurrency or usage limit.

work_snapshot() {
  local id="$1"
  jget "$BASE/api/work/$id" | jq '{id,state,execution_state,failure_class,dispatch_attempts,recovery_count,worker_id,workspace,pane,agent,last_agent_status}'
}

cancel_work() {
  local id="$1"
  curl -fsS -X POST "$BASE/api/work/$id/cancel" >/dev/null 2>&1 || true
}

echo "=== Verify four prompt submissions were accepted ==="
for id in "${WORK_IDS[@]}"; do
  item="$(jget "$BASE/api/work/$id")"
  echo "$item" | jq '{id,state,execution_state,failure_class,dispatch_attempts,worker_id,pane,agent,last_agent_status}'
  attempts="$(echo "$item" | jq -r '.dispatch_attempts // 0')"
  exec_state="$(echo "$item" | jq -r '.execution_state // "none"')"
  [[ "$attempts" == "1" ]] || fail "$id was not dispatched exactly once"
  [[ "$exec_state" != "dispatch_uncertain" && "$exec_state" != "stalled" ]] || fail "$id entered $exec_state after dispatch"
done

echo "M4_EXECUTION_FABRIC_PARALLEL_PASS peak_parallel_dispatches=$PEAK"
echo "NOTE: completion latency/provider model concurrency is intentionally not a pass/fail condition in M4."

echo "=== Verify same-workspace job remains queued while first lease is held ==="
DUP_ITEM="$(jget "$BASE/api/work/$DUP_ID")"
echo "$DUP_ITEM" | jq '{id,state,worker_id,workspace,execution_state}'
[[ "$(echo "$DUP_ITEM" | jq -r '.state')" == "queued" ]] || fail "same-workspace fifth job must remain queued while first writer lease is held"

# Release only the Studio work/lease. The already-submitted certification
# prompt may still settle in the upstream model, but it is read-only and we do
# not re-send it.
echo "=== Release first workspace lease and verify queued work can acquire it ==="
cancel_work "${WORK_IDS[0]}"
post_json "$BASE/api/scheduler/run" '{}' >/dev/null
for _ in {1..30}; do
  DUP_ITEM="$(jget "$BASE/api/work/$DUP_ID")"
  [[ "$(echo "$DUP_ITEM" | jq -r '.state')" == "running" ]] && break
  sleep 0.2
  post_json "$BASE/api/scheduler/run" '{}' >/dev/null
 done
DUP_ITEM="$(jget "$BASE/api/work/$DUP_ID")"
echo "$DUP_ITEM" | jq '{id,state,worker_id,workspace,execution_state}'
[[ "$(echo "$DUP_ITEM" | jq -r '.state')" == "running" ]] || fail "serialized work did not acquire a worker after lease release"
# No need to submit a fifth model prompt merely to certify the lease scheduler.
cancel_work "$DUP_ID"

# Clean up the remaining parallel certification work in Studio before the
# resilience pair. We deliberately do not wait for model completion here.
for id in "${WORK_IDS[@]:1}"; do
  cancel_work "$id"
done
post_json "$BASE/api/scheduler/run" '{}' >/dev/null || true

echo "=== Resilience isolation: two post-dispatch works ==="
# Refresh after the parallel submissions. Prefer two targets that Herdr still
# considers usable. The prompt submission itself is the live boundary under
# test; model completion is provider-capacity dependent and is not required.
HERDR2="$(post_json "$BASE/api/herdr/refresh" '{}')"
mapfile -t RTARGETS < <(echo "$HERDR2" | jq -r '
  [.panes.panes[]
   | select(.agent != null)
   | select((.agent_status == "idle") or (.agent_status == "done") or (.agent_status == "working") or (.agent_status == "blocked"))]
  | unique_by(.cwd)
  | .[:2][]
  | [.pane_id,.agent,.cwd] | @tsv')
[[ "${#RTARGETS[@]}" -ge 2 ]] || fail "need two Herdr agent panes for resilience certification"
IFS=$'\t' read -r rpane1 ragent1 rws1 <<< "${RTARGETS[0]}"
IFS=$'\t' read -r rpane2 ragent2 rws2 <<< "${RTARGETS[1]}"
FAULT_ID="$(submit_work "M4 resilience fault" "$rpane1" "$ragent1" "$rws1" "M4_RECOVERY_OK" 70)"
SIBLING_ID="$(submit_work "M4 resilience sibling" "$rpane2" "$ragent2" "$rws2" "M4_SIBLING_OK" 69)"
post_json "$BASE/api/scheduler/run" '{}' >/dev/null

for id in "$FAULT_ID" "$SIBLING_ID"; do
  [[ "$(jget "$BASE/api/work/$id" | jq -r '.state')" == "running" ]] || fail "$id did not start"
done

post_json "$BASE/api/work/$FAULT_ID/fault" '{"kind":"pane_missing","ticks":1,"stage":"post_dispatch","note":"M4 transient supervisor-only certification fault"}' | jq .
RES_BATCH="$(post_json "$BASE/api/execution/dispatch-ready" "$(jq -nc --arg a "$FAULT_ID" --arg b "$SIBLING_ID" '{work_ids:[$a,$b]}')")"
echo "$RES_BATCH" | jq .
[[ "$(echo "$RES_BATCH" | jq -r '.succeeded')" == "2" ]] || {
  echo "The Studio fabric is healthy, but the upstream model/provider may be refusing new work due to a capacity/usage limit." >&2
  work_snapshot "$FAULT_ID" || true
  work_snapshot "$SIBLING_ID" || true
  cancel_work "$FAULT_ID"; cancel_work "$SIBLING_ID"
  fail "resilience pair prompt submission was not accepted; inspect Herdr panes for provider/model limit messages"
}

FAULT_PRE="$(jget "$BASE/api/work/$FAULT_ID")"
SIBLING_PRE="$(jget "$BASE/api/work/$SIBLING_ID")"
echo "$FAULT_PRE" | jq '{id,state,execution_state,failure_class,dispatch_attempts,recovery_count,worker_id}'
echo "$SIBLING_PRE" | jq '{id,state,execution_state,failure_class,dispatch_attempts,recovery_count,worker_id}'
[[ "$(echo "$FAULT_PRE" | jq -r '.dispatch_attempts')" == "1" ]] || fail "fault target was not dispatched exactly once before resilience fault"
[[ "$(echo "$SIBLING_PRE" | jq -r '.dispatch_attempts')" == "1" ]] || fail "sibling was not dispatched exactly once"

post_json "$BASE/api/execution/run" '{}' >/dev/null
FAULT_ITEM="$(jget "$BASE/api/work/$FAULT_ID")"
SIBLING_ITEM="$(jget "$BASE/api/work/$SIBLING_ID")"
echo "$FAULT_ITEM" | jq '{id,state,execution_state,failure_class,dispatch_attempts,recovery_count,worker_id}'
echo "$SIBLING_ITEM" | jq '{id,state,execution_state,failure_class,dispatch_attempts,recovery_count,worker_id}'
[[ "$(echo "$FAULT_ITEM" | jq -r '.execution_state')" == "reconnecting" ]] || fail "faulted work did not enter reconnecting"
[[ "$(echo "$SIBLING_ITEM" | jq -r '.execution_state')" != "reconnecting" ]] || fail "fault leaked into sibling worker"

post_json "$BASE/api/execution/run" '{}' >/dev/null
FAULT_ITEM="$(jget "$BASE/api/work/$FAULT_ID")"
echo "$FAULT_ITEM" | jq '{id,state,execution_state,failure_class,dispatch_attempts,recovery_count,worker_id}'
ATTEMPTS_AFTER="$(echo "$FAULT_ITEM" | jq -r '.dispatch_attempts')"
[[ "$ATTEMPTS_AFTER" == "1" ]] || fail "no-redispatch invariant violated: dispatch_attempts=$ATTEMPTS_AFTER (expected 1)"
[[ "$(echo "$FAULT_ITEM" | jq -r '.recovery_count')" -ge 1 ]] || fail "recovery_count did not advance"

# The recovery invariant is now certified. Do not wait on upstream model
# completion; clean up Studio state deterministically.
cancel_work "$FAULT_ID"
cancel_work "$SIBLING_ID"
post_json "$BASE/api/scheduler/run" '{}' >/dev/null || true

echo "=== Final cleanup/state ==="
FINAL="$(jget "$BASE/api/status")"
echo "$FINAL" | jq '{workers:.workers,work:.work,execution:.execution,telemetry:.telemetry}'
LEASES="$(jget "$BASE/api/workers" | jq '.leases | length')"
[[ "$(echo "$FINAL" | jq -r '.workers.counts.busy // 0')" == "0" ]] || fail "busy workers remain"
[[ "$(echo "$FINAL" | jq -r '.workers.bound // 0')" == "0" ]] || fail "bound workers remain"
[[ "$(echo "$FINAL" | jq -r '.work.active // 0')" == "0" ]] || fail "active work remains"
[[ "$(echo "$FINAL" | jq -r '.work.queue // 0')" == "0" ]] || fail "queue remains"
[[ "$LEASES" == "0" ]] || fail "workspace leases remain"

echo
echo "================================="
echo "M4_LIVE_CERTIFICATION_PASS"
echo "================================="
