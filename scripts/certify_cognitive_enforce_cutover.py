from __future__ import annotations

import argparse
import asyncio
import copy
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from mcp_studio.cognitive_router import CognitiveRouter
from mcp_studio.machine_router import MachineCapabilityRouter
from mcp_studio.node_backends import refresh_local_node_backends
from mcp_studio.node_capabilities import refresh_local_node_capabilities
from mcp_studio.node_health import refresh_local_node_health
from mcp_studio.node_models import refresh_local_node_models
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import load_settings

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from certify_cognitive_node_routing import (
    CASES,
    LiveHerdr,
    LiveRuntimes,
    StaticCapsules,
    _runtime_order,
)


SCHEMA = "hirda-n8-enforce-cutover-certification-v1"


def _git_head(root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def compare_live_enforce(
    baseline: dict[str, Any],
    live_enforce: dict[str, Any],
) -> dict[str, Any]:
    baseline_order = _runtime_order(baseline)
    enforce_order = _runtime_order(live_enforce)
    infrastructure = dict(
        live_enforce.get("infrastructure_routing") or {}
    )
    decisions = [
        dict(item)
        for item in infrastructure.get("decisions") or []
        if isinstance(item, dict)
    ]
    decision_by_runtime = {
        str(item.get("runtime") or ""): item for item in decisions
    }
    survivor_decisions = [
        decision_by_runtime.get(runtime) for runtime in enforce_order
    ]

    lane_equal = str(baseline.get("lane")) == str(live_enforce.get("lane"))
    order_equal = baseline_order == enforce_order
    all_candidates_eligible = all(
        isinstance(item, dict) and bool(item.get("eligible"))
        for item in survivor_decisions
    )
    no_filtered_runtimes = (
        list(infrastructure.get("filtered_runtimes") or []) == []
    )
    identity_continuity = bool(
        live_enforce.get("identity_continuity")
    )
    semantic_authority_unchanged = (
        live_enforce.get("semantic_authority_changed") is False
        and infrastructure.get("semantic_authority_changed") is False
        and infrastructure.get("routing_authority")
        == "infrastructure_eligibility_only"
    )
    drift = 0 if lane_equal and order_equal else 1
    passed = (
        infrastructure.get("mode") == "enforce"
        and bool(infrastructure.get("enforced"))
        and lane_equal
        and order_equal
        and all_candidates_eligible
        and no_filtered_runtimes
        and identity_continuity
        and semantic_authority_unchanged
    )
    return {
        "baseline_lane": baseline.get("lane"),
        "enforce_lane": live_enforce.get("lane"),
        "baseline_candidates": baseline_order,
        "enforce_candidates": enforce_order,
        "lane_equal": lane_equal,
        "candidate_order_equal": order_equal,
        "all_candidates_eligible": all_candidates_eligible,
        "filtered_runtimes": list(
            infrastructure.get("filtered_runtimes") or []
        ),
        "identity_continuity": identity_continuity,
        "semantic_authority_changed": not semantic_authority_unchanged,
        "semantic_drift": drift,
        "passed": passed,
    }


def _build_registry(settings: Any) -> NodeRegistry:
    registry = NodeRegistry(
        settings.studio,
        base_dir=settings.config_path.parent,
    )
    refresh_local_node_backends(
        registry,
        settings.studio.node_backend_probes,
    )
    refresh_local_node_models(
        registry,
        settings.studio.node_backend_probes,
    )
    refresh_local_node_capabilities(
        registry,
        settings.studio.node_backend_probes,
    )
    refresh_local_node_health(
        registry,
        settings.studio.node_backend_probes,
        storage_path=settings.config_path.parent,
    )
    return registry


async def certify(
    root: Path,
    *,
    base_url: str,
    output_path: Path,
) -> dict[str, Any]:
    settings = load_settings(root / "config.yaml")
    if settings.studio.cognitive_node_routing_mode != "enforce":
        raise RuntimeError(
            "config must be enforce for post-cutover certification"
        )

    registry = _build_registry(settings)
    machine_router = MachineCapabilityRouter(
        registry,
        object(),
        object(),
    )

    mapped_capabilities = sorted(
        {
            str(value).strip().lower()
            for value in settings.studio.cognitive_capability_routes.values()
            if str(value).strip()
        }
    )
    binding_checks: list[dict[str, Any]] = []
    for runtime_id, raw_binding in sorted(
        settings.studio.cognitive_runtime_bindings.items()
    ):
        binding = dict(raw_binding)
        for advertised_capability in mapped_capabilities:
            resolved = machine_router.resolve_capability_route(
                advertised_capability,
                permission="execute",
                machine_id=str(binding.get("machine_id") or "").strip(),
                backend_id=(
                    str(binding.get("backend_id") or "").strip().lower()
                    or None
                ),
                model_id=(
                    str(binding.get("model_id") or "").strip()
                    or None
                ),
            )
            binding_checks.append(
                {
                    "runtime": str(runtime_id).casefold(),
                    "advertised_capability": advertised_capability,
                    "binding": binding,
                    "eligible": isinstance(
                        resolved.get("selected"),
                        dict,
                    ),
                    "selected": resolved.get("selected"),
                    "rejected": list(
                        resolved.get("rejected") or []
                    ),
                }
            )

    async with httpx.AsyncClient(timeout=30.0) as client:
        status_response = await client.get(
            f"{base_url.rstrip('/')}/api/cognition/status"
        )
        status_response.raise_for_status()
        live_status = status_response.json()
        production_node_routing = dict(
            live_status.get("node_routing") or {}
        )
        if production_node_routing.get("mode") != "enforce":
            raise RuntimeError(
                "live production is not enforce after cutover"
            )

        cases: list[dict[str, Any]] = []
        for capability, depth in CASES:
            payload = {
                "request_id": (
                    f"CERT-CUTOVER-{capability.upper().replace('_', '-')}"
                ),
                "prompt": (
                    "Post-cutover plan-only certification. "
                    "Do not execute a prompt."
                ),
                "capability": capability,
                "preferred_depth": depth,
                "allow_fallback": True,
            }
            live_response = await client.post(
                f"{base_url.rstrip('/')}/api/cognition/plan",
                json=payload,
            )
            live_response.raise_for_status()
            live_enforce = live_response.json()
            lane_states = live_enforce.get("lane_states") or {}

            off_studio = replace(
                settings.studio,
                cognitive_node_routing_mode="off",
                jev_enabled=False,
            )
            baseline_router = CognitiveRouter(
                off_studio,
                LiveHerdr(live_status),
                LiveRuntimes(live_status),
                StaticCapsules(lane_states),
                machine_router,
            )
            baseline = await baseline_router.plan(payload)

            result = compare_live_enforce(
                baseline,
                live_enforce,
            )
            result.update(
                {
                    "capability": capability,
                    "preferred_depth": depth,
                }
            )
            cases.append(result)

    semantic_drift = sum(
        int(item["semantic_drift"]) for item in cases
    )
    checks = {
        "config_enforce": (
            settings.studio.cognitive_node_routing_mode == "enforce"
        ),
        "production_enforce_active": (
            production_node_routing.get("mode") == "enforce"
        ),
        "all_runtime_bindings_eligible": all(
            bool(item["eligible"]) for item in binding_checks
        ),
        "all_cases_passed": all(
            bool(item["passed"]) for item in cases
        ),
        "candidate_order_continuity": all(
            bool(item["candidate_order_equal"]) for item in cases
        ),
        "lane_continuity": all(
            bool(item["lane_equal"]) for item in cases
        ),
        "no_unexpected_runtime_filtering": all(
            not item["filtered_runtimes"] for item in cases
        ),
        "identity_continuity": all(
            bool(item["identity_continuity"]) for item in cases
        ),
        "semantic_authority_unchanged": all(
            item["semantic_authority_changed"] is False
            for item in cases
        ),
        "no_prompt_execution": True,
        "semantic_drift_zero": semantic_drift == 0,
    }
    report = {
        "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": _git_head(root),
        "service_url": base_url.rstrip("/"),
        "production_node_routing": production_node_routing,
        "binding_checks": binding_checks,
        "cases": cases,
        "checks": checks,
        "semantic_drift": semantic_drift,
        "passed": all(checks.values()),
        "rollback_mode": "shadow",
        "next_action": (
            "keep production enforce and monitor live execution"
            if all(checks.values())
            else "rollback production to shadow"
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Certify the live N8 production enforce cutover "
            "without prompt execution."
        )
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8100",
    )
    parser.add_argument(
        "--output",
        default="data/certification/n8-enforce-cutover-latest.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    root = Path(__file__).resolve().parents[1]
    report = asyncio.run(
        certify(
            root,
            base_url=args.base_url,
            output_path=(root / args.output).resolve(),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
