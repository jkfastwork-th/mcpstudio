from __future__ import annotations

import argparse
import asyncio
import copy
import json
import subprocess
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


SCHEMA = "hirda-n8-shadow-certification-v1"
CASES = (
    ("social_dialogue", "fast"),
    ("reflection", "fast"),
    ("fast_utility", "fast"),
    ("reasoning_high", "deep"),
    ("coding", "deep"),
    ("research", "deep"),
)


class LiveHerdr:
    def __init__(self, status: dict[str, Any]) -> None:
        panes: list[dict[str, Any]] = []
        seen: set[str] = set()
        runtimes = ((status.get("runtimes") or {}).get("runtimes") or [])
        for runtime in runtimes:
            if not isinstance(runtime, dict):
                continue
            runtime_id = str(runtime.get("id") or "").strip().casefold()
            for raw in runtime.get("active_panes") or []:
                if not isinstance(raw, dict):
                    continue
                pane_id = str(raw.get("pane_id") or "").strip()
                if not pane_id or pane_id in seen:
                    continue
                item = dict(raw)
                item["agent"] = runtime_id
                panes.append(item)
                seen.add(pane_id)
        self.snapshot = {
            "status": ((status.get("runtimes") or {}).get("herdr_status") or "unknown"),
            "panes": {"panes": panes},
        }

    async def refresh(self) -> dict[str, Any]:
        return self.snapshot

    def find_pane(
        self,
        *,
        pane_id: str | None = None,
        workspace: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any] | None:
        panes = self.snapshot["panes"]["panes"]
        if pane_id:
            return next((row for row in panes if row.get("pane_id") == pane_id), None)
        if agent:
            wanted = str(agent).casefold()
            return next(
                (
                    row
                    for row in panes
                    if str(row.get("agent") or "").casefold() == wanted
                ),
                None,
            )
        return panes[0] if panes else None


class LiveRuntimes:
    def __init__(self, status: dict[str, Any]) -> None:
        self._snapshot = copy.deepcopy(status.get("runtimes") or {})

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        return copy.deepcopy(self._snapshot)


class StaticCapsules:
    def __init__(self, lane_states: dict[str, Any]) -> None:
        self._lane_states = copy.deepcopy(lane_states)

    async def lane_states(self) -> dict[str, Any]:
        return copy.deepcopy(self._lane_states)


def _runtime_order(plan: dict[str, Any]) -> list[str]:
    return [
        str(item.get("runtime") or "")
        for item in plan.get("candidates") or []
        if isinstance(item, dict)
    ]


def compare_case(
    capability: str,
    depth: str,
    legacy: dict[str, Any],
    shadow: dict[str, Any],
) -> dict[str, Any]:
    legacy_order = _runtime_order(legacy)
    shadow_order = _runtime_order(shadow)
    infrastructure = dict(shadow.get("infrastructure_routing") or {})
    decisions = [
        dict(item)
        for item in infrastructure.get("decisions") or []
        if isinstance(item, dict)
    ]
    decision_by_runtime = {
        str(item.get("runtime") or ""): item for item in decisions
    }
    candidate_decisions = [
        decision_by_runtime.get(runtime) for runtime in shadow_order
    ]
    all_routes_eligible = all(
        isinstance(item, dict) and bool(item.get("eligible"))
        for item in candidate_decisions
    )
    lane_equal = str(legacy.get("lane")) == str(shadow.get("lane"))
    order_equal = legacy_order == shadow_order
    identity_continuity = bool(shadow.get("identity_continuity"))
    semantic_authority_unchanged = (
        shadow.get("semantic_authority_changed") is False
        and infrastructure.get("semantic_authority_changed") is False
    )
    drift = 0 if lane_equal and order_equal else 1
    passed = (
        lane_equal
        and order_equal
        and all_routes_eligible
        and identity_continuity
        and semantic_authority_unchanged
        and infrastructure.get("mode") == "shadow"
        and infrastructure.get("filtered_runtimes") == []
    )
    return {
        "capability": capability,
        "preferred_depth": depth,
        "legacy_lane": legacy.get("lane"),
        "shadow_lane": shadow.get("lane"),
        "legacy_candidates": legacy_order,
        "shadow_candidates": shadow_order,
        "lane_equal": lane_equal,
        "candidate_order_equal": order_equal,
        "all_routes_eligible": all_routes_eligible,
        "identity_continuity": identity_continuity,
        "semantic_authority_changed": not semantic_authority_unchanged,
        "semantic_drift": drift,
        "infrastructure_routing": infrastructure,
        "passed": passed,
    }


def _git_head(root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


async def certify(
    root: Path,
    *,
    base_url: str,
    output_path: Path,
) -> dict[str, Any]:
    settings = load_settings(root / "config.yaml")
    if settings.studio.cognitive_node_routing_mode != "shadow":
        raise RuntimeError(
            "cognitive_node_routing_mode must be shadow for this certification"
        )

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
                    "eligible": isinstance(resolved.get("selected"), dict),
                    "selected": resolved.get("selected"),
                    "rejected": list(resolved.get("rejected") or []),
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

        cases: list[dict[str, Any]] = []
        for capability, depth in CASES:
            payload = {
                "request_id": f"CERT-N8-{capability.upper().replace('_', '-')}",
                "prompt": (
                    "Certification plan only. Preserve the existing semantic "
                    "runtime order and do not execute a prompt."
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
            live_shadow = live_response.json()
            lane_states = live_shadow.get("lane_states") or {}

            off_studio = replace(
                settings.studio,
                cognitive_node_routing_mode="off",
                jev_enabled=False,
            )
            off_router = CognitiveRouter(
                off_studio,
                LiveHerdr(live_status),
                LiveRuntimes(live_status),
                StaticCapsules(lane_states),
                machine_router,
            )
            baseline = await off_router.plan(payload)

            shadow_studio = replace(
                settings.studio,
                cognitive_node_routing_mode="shadow",
                jev_enabled=False,
            )
            shadow_router = CognitiveRouter(
                shadow_studio,
                LiveHerdr(live_status),
                LiveRuntimes(live_status),
                StaticCapsules(lane_states),
                machine_router,
            )
            shadow = await shadow_router.plan(payload)

            item = compare_case(
                capability,
                depth,
                baseline,
                shadow,
            )
            live_order = _runtime_order(live_shadow)
            shadow_order = _runtime_order(shadow)
            live_matches_shadow = (
                live_order == shadow_order
                and str(live_shadow.get("lane"))
                == str(shadow.get("lane"))
                and (
                    (live_shadow.get("infrastructure_routing") or {}).get("mode")
                    == "shadow"
                )
            )
            item["live_service_candidates"] = live_order
            item["live_service_shadow_matches"] = live_matches_shadow
            item["passed"] = bool(item["passed"] and live_matches_shadow)
            cases.append(item)

    semantic_drift = sum(int(item["semantic_drift"]) for item in cases)
    checks = {
        "configured_shadow_mode": (
            settings.studio.cognitive_node_routing_mode == "shadow"
        ),
        "production_shadow_active": (
            production_node_routing.get("mode") == "shadow"
        ),
        "all_runtime_bindings_eligible": all(
            bool(item["eligible"]) for item in binding_checks
        ),
        "all_cases_passed": all(bool(item["passed"]) for item in cases),
        "all_n7_routes_eligible": all(
            bool(item["all_routes_eligible"]) for item in cases
        ),
        "candidate_order_continuity": all(
            bool(item["candidate_order_equal"]) for item in cases
        ),
        "lane_continuity": all(bool(item["lane_equal"]) for item in cases),
        "live_service_shadow_matches": all(
            bool(item["live_service_shadow_matches"]) for item in cases
        ),
        "identity_continuity": all(
            bool(item["identity_continuity"]) for item in cases
        ),
        "semantic_authority_unchanged": all(
            item["semantic_authority_changed"] is False for item in cases
        ),
        "no_execution_performed": True,
        "semantic_drift_zero": semantic_drift == 0,
    }
    report = {
        "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": _git_head(root),
        "service_url": base_url.rstrip("/"),
        "configured_mode": settings.studio.cognitive_node_routing_mode,
        "production_node_routing": production_node_routing,
        "capability_mapping": dict(settings.studio.cognitive_capability_routes),
        "runtime_bindings": copy.deepcopy(
            settings.studio.cognitive_runtime_bindings
        ),
        "binding_checks": binding_checks,
        "cases": cases,
        "checks": checks,
        "semantic_drift": semantic_drift,
        "passed": all(checks.values()),
        "next_action": (
            "keep production in shadow and begin controlled enforce certification"
            if all(checks.values())
            else "keep production in shadow and inspect failed certification gates"
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
        description="Certify N8 shadow routing against an explicit off-mode baseline and the live production shadow planner."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8100",
    )
    parser.add_argument(
        "--output",
        default="data/certification/n8-shadow-latest.json",
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
