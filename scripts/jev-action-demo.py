#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_studio.action_adapter import (
    evaluate_action_envelope,
    normalize_action_envelope,
    sample_action_envelope,
)
from mcp_studio.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate or live-test the HIRDA JEV Action Adapter with a no-executor sample."
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--json", action="store_true", help="Print normalized sample JSON.")
    parser.add_argument("--live", action="store_true", help="Call configured JEV endpoint. Never executes an action.")
    args = parser.parse_args()

    envelope = normalize_action_envelope(sample_action_envelope())
    if args.live:
        settings = load_settings(args.config)
        judgment = asyncio.run(evaluate_action_envelope(settings.studio, envelope))
        print(json.dumps(judgment.as_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if judgment.evaluated else 2

    if args.json:
        print(json.dumps(envelope.as_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(
            "HIRDA_JEV_ACTION_ADAPTER_VALIDATE_PASS "
            f"schema={envelope.schema} provider={envelope.provider} "
            f"domain={envelope.domain} actions={len(envelope.actions)} executor_attached=false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
