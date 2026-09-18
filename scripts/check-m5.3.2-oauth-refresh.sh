#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${M5_LOCAL_BASE:-http://127.0.0.1:8100}"
ROW="$(curl -fsS "$BASE/api/certification/m5.3.2")"
echo "$ROW" | jq '{version,oauth}'
ROT="$(jq -r '.oauth.refresh_tokens_rotated' <<<"$ROW")"
if [[ "$ROT" -ge 1 ]]; then
  echo "M5_3_2_OAUTH_REFRESH_ROTATION_PASS rotated=$ROT"
else
  echo 'M5_3_2_OAUTH_REFRESH_ROTATION_PENDING'
  exit 2
fi
