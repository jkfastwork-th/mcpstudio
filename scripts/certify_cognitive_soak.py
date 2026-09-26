from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def certify(base_url: str, window: int, require_min: bool) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        status = (
            await client.get(f"{base_url}/api/cognition/status")
        ).json()
        soak = (
            await client.get(
                f"{base_url}/api/cognition/observability",
                params={"window_minutes": window},
            )
        ).json()

    metrics = soak.get("metrics") or {}
    thresholds = soak.get("thresholds") or {}
    integrity = soak.get("integrity") or {}
    privacy = soak.get("privacy") or {}
    enough = int(metrics.get("execute_count") or 0) >= int(
        thresholds.get("min_executions") or 1
    )
    checks = {
        "production_enforce": (
            (status.get("node_routing") or {}).get("mode") == "enforce"
        ),
        "schema": soak.get("schema") == "hirda-cognitive-observability-v1",
        "mode": soak.get("routing_mode") == "enforce",
        "privacy": (
            privacy.get("prompt_recorded") is False
            and privacy.get("output_recorded") is False
        ),
        "identity": integrity.get("identity_continuity") is True,
        "semantic_authority": (
            integrity.get("semantic_authority_unchanged") is True
        ),
        "observer": soak.get("last_observer_error") in {None, ""},
        "not_down": soak.get("state") != "down",
        "sample_requirement": enough if require_min else True,
    }
    return {
        "schema": "hirda-cognitive-soak-certification-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": _head(Path.cwd()),
        "checks": checks,
        "soak": soak,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--window-minutes", type=int, default=1440)
    parser.add_argument("--require-min-executions", action="store_true")
    parser.add_argument(
        "--output",
        default="data/certification/cognitive-soak-latest.json",
    )
    args = parser.parse_args()
    result = asyncio.run(
        certify(
            args.base_url.rstrip("/"),
            max(1, args.window_minutes),
            args.require_min_executions,
        )
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
