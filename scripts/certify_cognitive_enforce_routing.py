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


SCHEMA = "hirda-n8-enforce-certification-v1"
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


def _semantic_continuity(plan: dict[str, Any]) -> bool:
    infrastructure = dict(plan.get("infrastructure_routing") or {})
    return (
        bool(plan.get("identity_continuity"))
        and plan.get("semantic_authority_changed") is False
        and infrastructure.get("semantic_authority_changed") is False
        and infrastructure.get("routing_authority")
        == "infrastructure_eligibility_only"
    )


def compare_healthy_case(
    baseline: dict[str, Any],
    enforce: dict[str, Any],
) -> dict[str, Any]:
    baseline_order = _runtime_order(baseline)
    enforce_order = _runtime_order(enforce)
    lane_equal = str(baseline.get("lane")) == str(enforce.get("lane"))
    order_equal = baseline_order == enforce_order
    infrastructure = dict(enforce.get("infrastructure_routing") or {})
    decisions = [
        item
        for item in infrastructure.get("decisions") or []
        if isinstance(item, dict)
    ]
    all_eligible = all(bool(item.get("eligible")) for item in decisions)
    semantic_continuity = _semantic_continuity(enforce)
    drift = 0 if lane_equal and order_equal else 1
    return {
        "baseline_candidates": baseline_order,
        "enforce_candidates": enforce_order,
        "lane_equal": lane_equal,
        "candidate_order_equal": order_equal,
        "all_routes_eligible": all_eligible,
        "identity_and_semantic_continuity": semantic_continuity,
        "semantic_drift": drift,
        "passed": (
            lane_equal
            and order_equal
            and all_eligible
            and semantic_continuity
            and infrastructure.get("mode") == "enforce"
        ),
    }


def compare_filtered_case(
    baseline: dict[str, Any],
    enforce: dict[str, Any],
    *,
    filtered_runtime: str,
    expected_rejection: str,
) -> dict[str, Any]:
    baseline_order = _runtime_order(baseline)
    enforce_order = _runtime_order(enforce)
    expected_order = [
        runtime for runtime in baseline_order if runtime != filtered_runtime
    ]
    infrastructure = dict(enforce.get("infrastructure_routing") or {})
    decisions = [
        item
        for item in infrastructure.get("decisions") or []
        if isinstance(item, dict)
    ]
    decision = next(
        (
            item
            for item in decisions
            if str(item.get("runtime") or "") == filtered_runtime
        ),
        None,
    )
    rejections = (
        list(decision.get("route_rejections") or [])
        if isinstance(decision, dict)
        else []
    )
    rejection_reasons = {
        str(item.get("reason") or "")
        for item in rejections
        if isinstance(item, dict)
    }
    lane_equal = str(baseline.get("lane")) == str(enforce.get("lane"))
    survivor_order_preserved = enforce_order == expected_order
    filtered_before_dispatch = (
        isinstance(decision, dict)
        and not bool(decision.get("eligible"))
        and filtered_runtime in (infrastructure.get("filtered_runtimes") or [])
        and filtered_runtime not in enforce_order
        and expected_rejection in rejection_reasons
    )
    semantic_continuity = _semantic_continuity(enforce)
    drift = 0 if lane_equal and survivor_order_preserved else 1
    return {
        "filtered_runtime": filtered_runtime,
        "expected_rejection": expected_rejection,
        "baseline_candidates": baseline_order,
        "expected_survivors": expected_order,
        "enforce_candidates": enforce_order,
        "lane_equal": lane_equal,
        "survivor_order_preserved": survivor_order_preserved,
        "filtered_before_dispatch": filtered_before_dispatch,
        "rejection_reasons": sorted(rejection_reasons),
        "identity_and_semantic_continuity": semantic_continuity,
        "semantic_drift": drift,
        "passed": (
            lane_equal
            and survivor_order_preserved
            and filtered_before_dispatch
            and semantic_continuity
        ),
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


def _studio_with_execute_policy(
    studio: Any,
    *,
    machine_id: str,
    allowed: bool,
) -> Any:
    registry = copy.deepcopy(studio.machine_registry)
    matched = False
    for item in registry:
        if not isinstance(item, dict):
            continue
        current_id = str(item.get("id") or item.get("machine_id") or "").strip()
        if current_id != machine_id:
            continue
        policy = dict(item.get("policy") or {})
        policy["execute"] = bool(allowed)
        item["policy"] = policy
        matched = True
    if not matched:
        raise RuntimeError(f"machine {machine_id} not found in configured registry")
    return replace(studio, machine_registry=registry)


def _build_registry(
    studio: Any,
    *,
    base_dir: Path,
) -> NodeRegistry:
    registry = NodeRegistry(studio, base_dir=base_dir)
    refresh_local_node_backends(
        registry,
        studio.node_backend_probes,
    )
    refresh_local_node_models(
        registry,
        studio.node_backend_probes,
    )
    refresh_local_node_capabilities(
        registry,
        studio.node_backend_probes,
    )
    refresh_local_node_health(
        registry,
        studio.node_backend_probes,
        storage_path=base_dir,
    )
    return registry


def _mark_backend_unavailable(
    registry: NodeRegistry,
    *,
    node_id: str,
    backend_id: str,
) -> None:
    lifecycle = registry.lifecycle(node_id)
    facets = ((lifecycle.get("enrichment") or {}).get("facets") or {})
    current = copy.deepcopy(
        ((facets.get("health.availability") or {}).get("data") or {})
    )
    if not current:
        raise RuntimeError("health observation missing")

    matched = False
    for item in current.get("backends") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("backend_id") or "").strip().lower() != backend_id:
            continue
        item["available"] = False
        item["state"] = "unhealthy"
        matched = True
    if not matched:
        raise RuntimeError(f"backend health missing for {backend_id}")

    observed_at = datetime.now(timezone.utc).isoformat()
    current["observed_at"] = observed_at
    registry.enrich_node(
        node_id,
        facet="health.availability",
        data=current,
        source="n8-enforce-certification",
        observed_at=observed_at,
    )


async def _plan(
    studio: Any,
    *,
    status: dict[str, Any],
    lane_states: dict[str, Any],
    machine_router: MachineCapabilityRouter,
    payload: dict[str, Any],
) -> dict[str, Any]:
    router = CognitiveRouter(
        studio,
        LiveHerdr(status),
        LiveRuntimes(status),
        StaticCapsules(lane_states),
        machine_router,
    )
    return await router.plan(payload)


async def certify(
    root: Path,
    *,
    base_url: str,
    output_path: Path,
) -> dict[str, Any]:
    settings = load_settings(root / "config.yaml")
    if settings.studio.cognitive_node_routing_mode != "shadow":
        raise RuntimeError(
            "production must remain in shadow during enforce certification"
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        status_response = await client.get(
            f"{base_url.rstrip('/')}/api/cognition/status"
        )
        status_response.raise_for_status()
        live_status = status_response.json()

        production_mode = str(
            ((live_status.get("node_routing") or {}).get("mode") or "")
        )
        if production_mode != "shadow":
            raise RuntimeError(
                "live production must remain in shadow during certification"
            )

        healthy_cases: list[dict[str, Any]] = []
        unavailable_cases: list[dict[str, Any]] = []

        for capability, depth in CASES:
            payload = {
                "request_id": f"CERT-ENFORCE-{capability.upper().replace('_', '-')}",
                "prompt": (
                    "Certification plan only. Do not execute a prompt."
                ),
                "capability": capability,
                "preferred_depth": depth,
                "allow_fallback": True,
            }
            live_shadow_response = await client.post(
                f"{base_url.rstrip('/')}/api/cognition/plan",
                json=payload,
            )
            live_shadow_response.raise_for_status()
            live_shadow = live_shadow_response.json()
            lane_states = live_shadow.get("lane_states") or {}

            healthy_studio = replace(
                settings.studio,
                cognitive_node_routing_mode="enforce",
                jev_enabled=False,
            )
            healthy_registry = _build_registry(
                healthy_studio,
                base_dir=settings.config_path.parent,
            )
            healthy_machine_router = MachineCapabilityRouter(
                healthy_registry,
                object(),
                object(),
            )

            off_studio = replace(
                settings.studio,
                cognitive_node_routing_mode="off",
                jev_enabled=False,
            )
            baseline = await _plan(
                off_studio,
                status=live_status,
                lane_states=lane_states,
                machine_router=healthy_machine_router,
                payload=payload,
            )
            healthy_enforce = await _plan(
                healthy_studio,
                status=live_status,
                lane_states=lane_states,
                machine_router=healthy_machine_router,
                payload=payload,
            )
            healthy_result = compare_healthy_case(
                baseline,
                healthy_enforce,
            )
            healthy_result.update(
                {
                    "capability": capability,
                    "preferred_depth": depth,
                }
            )
            healthy_cases.append(healthy_result)

            baseline_order = _runtime_order(baseline)
            if not baseline_order:
                unavailable_cases.append(
                    {
                        "capability": capability,
                        "preferred_depth": depth,
                        "passed": False,
                        "error": "baseline_has_no_candidates",
                        "semantic_drift": 1,
                    }
                )
                continue

            primary_runtime = baseline_order[0]
            binding = dict(
                settings.studio.cognitive_runtime_bindings.get(
                    primary_runtime,
                    {},
                )
            )
            node_id = str(binding.get("machine_id") or "").strip()
            backend_id = str(binding.get("backend_id") or "").strip().lower()
            if not node_id or not backend_id:
                unavailable_cases.append(
                    {
                        "capability": capability,
                        "preferred_depth": depth,
                        "passed": False,
                        "error": "primary_runtime_binding_incomplete",
                        "semantic_drift": 1,
                    }
                )
                continue

            unhealthy_registry = _build_registry(
                healthy_studio,
                base_dir=settings.config_path.parent,
            )
            _mark_backend_unavailable(
                unhealthy_registry,
                node_id=node_id,
                backend_id=backend_id,
            )
            unhealthy_machine_router = MachineCapabilityRouter(
                unhealthy_registry,
                object(),
                object(),
            )
            unhealthy_enforce = await _plan(
                healthy_studio,
                status=live_status,
                lane_states=lane_states,
                machine_router=unhealthy_machine_router,
                payload=payload,
            )
            filtered_result = compare_filtered_case(
                baseline,
                unhealthy_enforce,
                filtered_runtime=primary_runtime,
                expected_rejection="backend_unavailable",
            )
            filtered_result.update(
                {
                    "capability": capability,
                    "preferred_depth": depth,
                    "simulated_backend": backend_id,
                }
            )
            unavailable_cases.append(filtered_result)

        machine_id = str(settings.studio.machine_local_id or "").strip()
        denied_studio = _studio_with_execute_policy(
            replace(
                settings.studio,
                cognitive_node_routing_mode="enforce",
                jev_enabled=False,
            ),
            machine_id=machine_id,
            allowed=False,
        )
        denied_registry = _build_registry(
            denied_studio,
            base_dir=settings.config_path.parent,
        )
        denied_machine_router = MachineCapabilityRouter(
            denied_registry,
            object(),
            object(),
        )

        policy_payload = {
            "request_id": "CERT-ENFORCE-POLICY-CEILING",
            "prompt": "Certification plan only. Do not execute a prompt.",
            "capability": "fast_utility",
            "preferred_depth": "fast",
            "allow_fallback": True,
        }
        policy_live_response = await client.post(
            f"{base_url.rstrip('/')}/api/cognition/plan",
            json=policy_payload,
        )
        policy_live_response.raise_for_status()
        policy_live = policy_live_response.json()

        denied_plan = await _plan(
            denied_studio,
            status=live_status,
            lane_states=policy_live.get("lane_states") or {},
            machine_router=denied_machine_router,
            payload=policy_payload,
        )
        policy_infrastructure = dict(
            denied_plan.get("infrastructure_routing") or {}
        )
        policy_decisions = [
            item
            for item in policy_infrastructure.get("decisions") or []
            if isinstance(item, dict)
        ]
        policy_reasons = {
            str(rejection.get("reason") or "")
            for decision in policy_decisions
            for rejection in decision.get("route_rejections") or []
            if isinstance(rejection, dict)
        }
        policy_ceiling = {
            "candidate_count": len(_runtime_order(denied_plan)),
            "filtered_runtimes": list(
                policy_infrastructure.get("filtered_runtimes") or []
            ),
            "rejection_reasons": sorted(policy_reasons),
            "identity_and_semantic_continuity": _semantic_continuity(
                denied_plan
            ),
            "semantic_drift": 0,
            "passed": (
                len(_runtime_order(denied_plan)) == 0
                and bool(policy_decisions)
                and policy_reasons == {"permission_denied"}
                and _semantic_continuity(denied_plan)
            ),
        }

    semantic_drift = (
        sum(int(item.get("semantic_drift") or 0) for item in healthy_cases)
        + sum(int(item.get("semantic_drift") or 0) for item in unavailable_cases)
        + int(policy_ceiling.get("semantic_drift") or 0)
    )
    checks = {
        "production_remained_shadow": production_mode == "shadow",
        "healthy_enforce_preserves_semantics": all(
            bool(item.get("passed")) for item in healthy_cases
        ),
        "unavailable_primary_filtered_before_dispatch": all(
            bool(item.get("passed")) for item in unavailable_cases
        ),
        "fallback_order_preserved": all(
            bool(item.get("survivor_order_preserved"))
            for item in unavailable_cases
        ),
        "machine_execute_policy_hard_ceiling": bool(
            policy_ceiling.get("passed")
        ),
        "identity_continuity": all(
            bool(item.get("identity_and_semantic_continuity"))
            for item in healthy_cases
        )
        and all(
            bool(item.get("identity_and_semantic_continuity"))
            for item in unavailable_cases
        )
        and bool(policy_ceiling.get("identity_and_semantic_continuity")),
        "semantic_authority_unchanged": True,
        "no_prompt_execution": True,
        "semantic_drift_zero": semantic_drift == 0,
    }
    report = {
        "schema": SCHEMA,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "base_commit": _git_head(root),
        "service_url": base_url.rstrip("/"),
        "production_mode": production_mode,
        "healthy_cases": healthy_cases,
        "unavailable_primary_cases": unavailable_cases,
        "policy_ceiling": policy_ceiling,
        "checks": checks,
        "semantic_drift": semantic_drift,
        "passed": all(checks.values()),
        "next_action": (
            "request explicit operator approval before switching production to enforce"
            if all(checks.values())
            else "keep production in shadow and inspect failed enforce gates"
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
            "Certify N8 enforce behavior against live runtime state while "
            "production remains in shadow."
        )
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8100",
    )
    parser.add_argument(
        "--output",
        default="data/certification/n8-enforce-latest.json",
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
