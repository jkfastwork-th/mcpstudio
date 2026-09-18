#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml


def parse_pair(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected KEY=/absolute/path")
    key, path = value.split("=", 1)
    key, path = key.strip(), path.strip()
    if not key or not path.startswith("/"):
        raise argparse.ArgumentTypeError("expected KEY=/absolute/path")
    return key, path


def main() -> int:
    ap = argparse.ArgumentParser(description="Enable M6.2.3B isolated managed Serena sessions")
    ap.add_argument("config", nargs="?", default="config.yaml")
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--workspace", action="append", type=parse_pair, default=[])
    args = ap.parse_args()

    path = Path(args.config).expanduser().resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    studio = raw.setdefault("studio", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-m6.2.3b-{stamp}.bak")
    shutil.copy2(path, backup)

    default_roots = ["/home/alfred/projects", "/home/alfred/ghq/github.com", "/data"]
    roots = list(dict.fromkeys([*(studio.get("managed_session_workspace_roots") or []), *(args.root or default_roots)]))
    workspaces = dict(studio.get("managed_session_workspaces") or {})
    for key, project in args.workspace:
        workspaces[key] = project
    if not workspaces and Path("/home/alfred/projects/flowpilot-ai").is_dir():
        workspaces["flowpilot-ai"] = "/home/alfred/projects/flowpilot-ai"

    studio.update({
        "managed_session_enabled": True,
        "managed_session_require_binding_for_tools": True,
        "managed_session_workspace_roots": roots,
        "managed_session_workspaces": workspaces,
        "managed_session_serena_executable": "/home/alfred/ghq/github.com/oraios/serena/.venv/bin/serena",
        "managed_session_serena_working_directory": "/home/alfred/ghq/github.com/oraios/serena",
        "managed_session_context": "chatgpt",
        "managed_session_port_start": int(studio.get("managed_session_port_start", 8210)),
        "managed_session_port_end": int(studio.get("managed_session_port_end", 8299)),
        "managed_session_start_timeout_seconds": float(studio.get("managed_session_start_timeout_seconds", 30)),
        "managed_session_stop_timeout_seconds": float(studio.get("managed_session_stop_timeout_seconds", 8)),
        "managed_session_monitor_interval_seconds": float(studio.get("managed_session_monitor_interval_seconds", 5)),
        "managed_session_auto_restore": True,
        "managed_session_auto_restart": True,
        "managed_session_require_existing_path": True,
        "managed_session_home": "/home/alfred",
        "managed_session_path": "/home/alfred/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin",
        "managed_session_log_dir": "./data/managed-sessions",
        "managed_session_blocked_tools": ["activate_project"],
    })
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    print(f"backup={backup}")
    print("managed_session_enabled=true")
    print("require_binding_for_tools=true")
    print("roots=" + ",".join(roots))
    print("workspaces=" + ",".join(sorted(workspaces)))
    print("NEXT: run ./scripts/install-production.sh so systemd installs writable root carve-outs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
