#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

path = Path(sys.argv[1] if len(sys.argv) > 1 else "config.yaml").expanduser().resolve()
if not path.exists():
    raise SystemExit(f"config not found: {path}")
raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
studio = raw.setdefault("studio", {})
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup = path.with_name(f"{path.name}.pre-m6.2.3a-{stamp}.bak")
shutil.copy2(path, backup)
studio["openai_local_ingress_enabled"] = True
studio.setdefault("openai_local_ingress_id", "openai-serena")
studio.setdefault("openai_local_ingress_allowed_hosts", ["127.0.0.1", "::1"])
path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(f"OPENAI_LOCAL_INGRESS_CONFIG_ENABLED {path}")
print(f"backup={backup}")
