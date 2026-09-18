#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
umask 077
for file in .oauth-signing-secret .oauth-owner-token; do
  if [[ ! -s "$file" ]]; then
    openssl rand -hex 32 > "$file"
    chmod 600 "$file"
    echo "created $ROOT/$file"
  else
    chmod 600 "$file"
    echo "kept existing $ROOT/$file"
  fi
done
printf '\nOwner approval token (enter this only on the MCP Studio authorization page):\n'
cat "$ROOT/.oauth-owner-token"
printf '\n\nDo not paste either secret into chat or ChatGPT plugin configuration.\n'
