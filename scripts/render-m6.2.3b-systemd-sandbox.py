#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def quote_systemd_path(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("invalid path")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def main() -> int:
    config = Path(sys.argv[1] if len(sys.argv) > 1 else "config.yaml").expanduser().resolve()
    raw = yaml.safe_load(config.read_text()) or {}
    studio = raw.get("studio") or {}
    roots = [str(Path(x).expanduser().resolve()) for x in studio.get("managed_session_workspace_roots", [])]
    home = str(studio.get("managed_session_home") or "").strip()
    print("[Service]")
    print("# M6.2.3B: write carve-outs for child Serena instance pool.")
    for root in roots:
        print(f"ReadWritePaths=-{quote_systemd_path(root)}")
    if home:
        print(f"ReadWritePaths=-{quote_systemd_path(str(Path(home) / '.serena'))}")
        print(f"ReadWritePaths=-{quote_systemd_path(str(Path(home) / '.cache' / 'serena'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
