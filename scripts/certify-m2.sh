#!/usr/bin/env bash
set -euo pipefail

BASE="${MCP_STUDIO_BASE:-http://127.0.0.1:8100}"

need() { command -v "$1" >/dev/null || { echo "ERROR: missing $1" >&2; exit 1; }; }
need curl
need jq

wait_state() {
  local id="$1" expected="$2"
  for _ in $(seq 1 60); do
    state="$(curl -fsS "$BASE/api/work/$id" | jq -r '.state')"
    [[ "$state" == "$expected" ]] && return 0
    sleep .25
  done
  echo "FAIL: $id did not reach $expected" >&2
  curl -fsS "$BASE/api/work/$id" | jq . >&2 || true
  exit 1
}

submit() {
  local label="$1" workspace="$2" priority="$3"
  curl -fsS -X POST "$BASE/api/work" \
    -H 'Content-Type: application/json' \
    -d "{\"label\":\"$label\",\"workspace\":\"$workspace\",\"priority\":$priority,\"lease_mode\":\"write\",\"metadata\":{\"certification\":\"M2-live\"}}" \
    | jq -r '.id'
}

complete() {
  curl -fsS -X POST "$BASE/api/work/$1/complete" \
    -H 'Content-Type: application/json' \
    -d '{"result":{"certification":true}}' >/dev/null
}

clean="$(curl -fsS "$BASE/api/workers")"
idle="$(jq -r '.summary.counts.idle' <<<"$clean")"
busy="$(jq -r '.summary.counts.busy' <<<"$clean")"
leases="$(jq -r '.leases | length' <<<"$clean")"
if [[ "$idle" != "4" || "$busy" != "0" || "$leases" != "0" ]]; then
  echo "FAIL: M2 certification requires 4 idle workers and zero leases" >&2
  jq . <<<"$clean" >&2
  exit 1
fi

echo "=== Submit four runnable workspaces + two queued cases ==="
A1="$(submit 'M2 earth primary' '/data/earth-616' 90)"
A2="$(submit 'M2 earth same-workspace queued' '/data/earth-616' 80)"
B="$(submit 'M2 nova oracle' '/home/alfred/ghq/github.com/jkfastdevth/nova-oracle' 70)"
C="$(submit 'M2 mobility' '/home/alfred/ghq/github.com/jkfastdevth/nova-oracle-mobility-v1' 60)"
D="$(submit 'M2 local cloud' '/home/alfred/projects/local-cloud' 50)"
E="$(submit 'M2 Eden queued' '/home/alfred/projects/Eden' 40)"

wait_state "$A1" running
wait_state "$A2" queued
wait_state "$B" running
wait_state "$C" running
wait_state "$D" running
wait_state "$E" queued

echo "=== 4 workers busy; same-workspace + capacity queue verified ==="
curl -fsS "$BASE/api/status" | jq '{workers:.workers,work:.work}'

echo "=== Complete B while higher-priority A2 is lease-blocked ==="
complete "$B"
wait_state "$E" running
wait_state "$A2" queued

echo "=== Head-of-line bypass verified: E ran while A2 remained lease-blocked ==="

echo "=== Release earth primary; queued earth work must take over ==="
complete "$A1"
wait_state "$A2" running

echo "=== Workspace lease transfer verified ==="

complete "$A2"
complete "$C"
complete "$D"
complete "$E"

for _ in $(seq 1 40); do
  final="$(curl -fsS "$BASE/api/workers")"
  idle="$(jq -r '.summary.counts.idle' <<<"$final")"
  busy="$(jq -r '.summary.counts.busy' <<<"$final")"
  leases="$(jq -r '.leases | length' <<<"$final")"
  [[ "$idle" == "4" && "$busy" == "0" && "$leases" == "0" ]] && break
  sleep .25
done

if [[ "$idle" != "4" || "$busy" != "0" || "$leases" != "0" ]]; then
  echo "FAIL: cleanup did not return to clean worker state" >&2
  jq . <<<"$final" >&2
  exit 1
fi

echo
curl -fsS "$BASE/api/status" | jq '{workers:.workers,work:.work}'
echo "================================="
echo "M2_LIVE_CERTIFICATION_PASS"
echo "================================="
