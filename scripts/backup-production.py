#!/usr/bin/env python3
from __future__ import annotations
import os, shutil, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from mcp_studio.settings import load_settings

root = Path(__file__).resolve().parents[1]
cfg_path = Path(os.environ.get("MCP_STUDIO_CONFIG", root / "config.yaml")).resolve()
s = load_settings(cfg_path)
db_path = Path(s.studio.database)
if not db_path.is_absolute():
    db_path = cfg_path.parent / db_path
backup_dir = root / "backups"
backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
if db_path.exists():
    out = backup_dir / f"mcp-studio-{stamp}.sqlite3"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    print(f"database_backup={out}")
config_out = backup_dir / f"config-{stamp}.yaml"
shutil.copy2(cfg_path, config_out)
print(f"config_backup={config_out}")
# Keep the newest 14 backups of each type.
for pattern in ("mcp-studio-*.sqlite3", "config-*.yaml"):
    items = sorted(backup_dir.glob(pattern), reverse=True)
    for old in items[14:]:
        old.unlink(missing_ok=True)
