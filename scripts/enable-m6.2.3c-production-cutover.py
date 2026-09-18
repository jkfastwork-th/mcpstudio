#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser(description="Enable M6.2.3C production managed-session cutover")
    ap.add_argument("config", nargs="?", default="config.yaml")
    args = ap.parse_args()

    path = Path(args.config).expanduser().resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    studio = raw.setdefault("studio", {})
    required = {
        "managed_session_enabled": True,
        "managed_session_require_binding_for_tools": True,
        "gateway_session_enabled": True,
    }
    missing = [k for k, expected in required.items() if studio.get(k) is not expected]
    if missing:
        raise SystemExit(
            "M6.2.3C requires M6.2.3B first; missing/disabled: " + ", ".join(missing)
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-m6.2.3c-{stamp}.bak")
    shutil.copy2(path, backup)

    studio["managed_session_cutover_enabled"] = True
    studio["managed_session_cutover_block_legacy_tools"] = True
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    print(f"backup={backup}")
    print("managed_session_cutover_enabled=true")
    print("managed_session_cutover_block_legacy_tools=true")
    print("legacy_serena_role=discovery_only")
    print("NEXT: sudo systemctl restart mcp-studio && ./scripts/certify-m6.2.3c-production-cutover.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
