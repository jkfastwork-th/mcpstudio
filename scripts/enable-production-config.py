#!/usr/bin/env python3
from pathlib import Path
import sys, yaml
p = Path(sys.argv[1] if len(sys.argv)>1 else "config.yaml")
raw = yaml.safe_load(p.read_text()) or {}
st = raw.setdefault("studio", {})
st["production_mode"] = True
st["resilience_test_mode"] = False
st["connectivity_test_mode"] = False
st["gateway_session_test_mode"] = False
st["operations_enabled"] = True
p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
print(f"PRODUCTION_CONFIG_ENABLED: {p}")
