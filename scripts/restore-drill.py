#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mcp_studio.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]
CFG = Path(os.environ.get("MCP_STUDIO_CONFIG", ROOT / "config.yaml")).resolve()
BACKUP_DIR = ROOT / "backups"
REPORT_DIR = ROOT / "backups" / "restore-drills"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

settings = load_settings(CFG)
backups = sorted(BACKUP_DIR.glob("mcp-studio-*.sqlite3"), reverse=True)
if not backups:
    raise SystemExit("RESTORE_DRILL_FAIL: no database backup found")
source = backups[0]

with tempfile.TemporaryDirectory(prefix="mcp-studio-restore-") as td:
    restored = Path(td) / "restored.sqlite3"
    shutil.copy2(source, restored)
    con = sqlite3.connect(restored)
    con.row_factory = sqlite3.Row
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    required = {
        "events", "sessions", "gateway_sessions", "workers", "workspace_leases",
        "work_items", "connectivity_tunnels", "oauth_clients", "schema_migrations",
        "audit_log", "operational_alerts", "mcp_request_metrics", "observability_samples",
    }
    missing = sorted(required - tables)
    migrations = []
    if "schema_migrations" in tables:
        migrations = [dict(r) for r in con.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()]
    con.close()

ok = integrity == "ok" and quick == "ok" and not missing and bool(migrations) and migrations[-1]["version"] >= 3
report = {
    "ok": ok,
    "created_at": datetime.now(timezone.utc).isoformat(),
    "backup": str(source),
    "integrity_check": integrity,
    "quick_check": quick,
    "missing_tables": missing,
    "schema_version": migrations[-1]["version"] if migrations else 0,
    "migrations": migrations,
}
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
out = REPORT_DIR / f"restore-drill-{stamp}.json"
out.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
print(f"report={out}")
if not ok:
    raise SystemExit("RESTORE_DRILL_FAIL")
print("RESTORE_DRILL_PASS")
