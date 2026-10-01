from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.cognitive_router import CognitiveRouter
from mcp_studio.machine_router import MachineCapabilityRouter
from mcp_studio.node_observations import ingest_node_observations
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


class PlanHerdr:
    def __init__(self) -> None:
        self.snapshot = {
            "status": "healthy",
            "panes": {
                "panes": [
                    {
                        "pane_id": "wH:p1",
                        "agent": "hermes",
                        "agent_status": "idle",
                    },
                    {
                        "pane_id": "wC:p1",
                        "agent": "claude",
                        "agent_status": "idle",
                    },
                ]
            },
        }

    async def refresh(self):
        return self.snapshot

    def find_pane(self, *, pane_id=None, workspace=None, agent=None):
        panes = self.snapshot["panes"]["panes"]
        if pane_id:
            return next(
                (item for item in panes if item["pane_id"] == pane_id),
                None,
            )
        if agent:
            return next(
                (item for item in panes if item["agent"] == agent),
                None,
            )
        return panes[0]


class PlanRuntimes:
    def snapshot(self, *, force=False):
        return {
            "runtimes": [
                {
                    "id": "hermes",
                    "provider": "runtime-provider-h",
                    "model": "runtime-model-h",
                    "installed": True,
                    "observed": True,
                    "status": "ready",
                    "free": True,
                    "auth_health": {"status": "healthy"},
                    "limit_health": {"status": "healthy"},
                },
                {
                    "id": "claude",
                    "provider": "runtime-provider-c",
                    "model": "runtime-model-c",
                    "installed": True,
                    "observed": True,
                    "status": "ready",
                    "free": False,
                    "auth_health": {"status": "healthy"},
                    "limit_health": {"status": "healthy"},
                },
            ]
        }


def _machine(machine_id: str, *, local: bool) -> dict:
    return {
        "id": machine_id,
        "name": machine_id,
        "enabled": True,
        "local": local,
        "os": "linux",
        "address": "127.0.0.1" if local else "100.64.0.20",
        "capabilities": ["filesystem"],
        "providers": {},
        "policy": {
            "read": True,
            "write": True,
            "execute": True,
            "destructive": False,
            "fail_closed_unknown": True,
        },
    }


def _seed(
    registry: NodeRegistry,
    node_id: str,
    *,
    available: bool,
) -> None:
    observed_at = datetime.now(timezone.utc).isoformat()
    ingest_node_observations(
        registry,
        node_id,
        {
            "runtime.backends": {
                "schema": "hirda-node-backends-v1",
                "observed_at": observed_at,
                "backends": [
                    {
                        "id": "runtime-x",
                        "kind": "inference",
                        "available": True,
                        "binary": "/usr/bin/runtime-x",
                        "version": "runtime-x 1",
                        "capabilities": ["chat"],
                    }
                ],
            },
            "runtime.models": {
                "schema": "hirda-node-models-v1",
                "observed_at": observed_at,
                "inventories": [
                    {
                        "backend_id": "runtime-x",
                        "status": "ready",
                        "models": [
                            {
                                "id": "model-a",
                                "name": "Model A",
                            }
                        ],
                    }
                ],
            },
            "capabilities.advertised": {
                "schema": "hirda-node-capabilities-v1",
                "observed_at": observed_at,
                "node": {"capabilities": []},
                "backends": [],
                "models": [
                    {
                        "backend_id": "runtime-x",
                        "model_id": "model-a",
                        "capabilities": ["chat"],
                    }
                ],
            },
            "health.availability": {
                "schema": "hirda-node-health-v1",
                "observed_at": observed_at,
                "node": {
                    "available": available,
                    "state": "ready" if available else "offline",
                    "load": {
                        "logical_cpu_count": 8,
                        "load_1m": 1.0,
                        "load_5m": 1.0,
                        "load_15m": 1.0,
                        "load_ratio_1m": 0.125,
                        "memory_total_bytes": 16_000,
                        "memory_available_bytes": 8_000,
                        "memory_available_ratio": 0.5,
                    },
                    "storage": {
                        "total_bytes": 100_000,
                        "free_bytes": 50_000,
                        "free_ratio": 0.5,
                    },
                },
                "backends": [
                    {
                        "backend_id": "runtime-x",
                        "available": available,
                        "state": "ready" if available else "offline",
                        "latency_ms": 10,
                    }
                ],
            },
        },
        source="n8-integration-test",
    )


@pytest.mark.asyncio
async def test_real_n7_resolver_filters_cognitive_runtime_by_bound_node(
    tmp_path: Path,
) -> None:
    node_settings = StudioConfig(
        machine_local_id="node-h",
        machine_registry=[
            _machine("node-h", local=True),
            _machine("node-c", local=False),
        ],
    )
    registry = NodeRegistry(node_settings, base_dir=tmp_path)
    _seed(registry, "node-h", available=False)
    _seed(registry, "node-c", available=True)
    machine_router = MachineCapabilityRouter(
        registry,
        object(),
        object(),
    )

    cognitive_settings = SimpleNamespace(
        jev_enabled=False,
        jev_mode="shadow",
        jev_model="jev-latest",
        jev_min_confidence=0.90,
        cognitive_router_default_timeout_seconds=30.0,
        cognitive_node_routing_mode="enforce",
        cognitive_capability_routes={"fast_utility": "chat"},
        cognitive_runtime_bindings={
            "hermes": {
                "machine_id": "node-h",
                "backend_id": "runtime-x",
                "model_id": "model-a",
            },
            "claude": {
                "machine_id": "node-c",
                "backend_id": "runtime-x",
                "model_id": "model-a",
            },
        },
    )
    router = CognitiveRouter(
        cognitive_settings,
        PlanHerdr(),
        PlanRuntimes(),
        capability_router=machine_router,
    )

    plan = await router.plan(
        {
            "request_id": "COG-N8-REAL-N7",
            "prompt": "Return a short answer.",
            "capability": "fast_utility",
        }
    )

    assert [item["runtime"] for item in plan["candidates"]] == ["claude"]
    report = plan["infrastructure_routing"]
    assert report["mode"] == "enforce"
    hermes = next(
        item
        for item in report["decisions"]
        if item["runtime"] == "hermes"
    )
    claude = next(
        item
        for item in report["decisions"]
        if item["runtime"] == "claude"
    )
    assert hermes["eligible"] is False
    assert hermes["reason"] == "no_eligible_route"
    assert hermes["route_rejections"][0]["reason"] == "node_unavailable"
    assert claude["eligible"] is True
    assert claude["selected"]["node_id"] == "node-c"
    assert claude["selected"]["backend_id"] == "runtime-x"
    assert claude["selected"]["model_id"] == "model-a"
    assert plan["semantic_authority_changed"] is False


def test_cognitive_pane_routing_settings_validate_mode_and_selectors():
    cfg = StudioConfig(
        cognitive_pane_routing_mode="shadow",
        cognitive_runtime_bindings={
            "hermes": {
                "machine_id": "openclaw",
                "backend_id": "hermes-runtime",
                "pane_ids": ["wH:p1"],
                "pane_name_prefixes": ["hirda-cognitive-"],
                "pane_label_prefixes": ["cognition:"],
                "principal_pane_name_prefixes": {
                    "fern": "hirda-fern-hermes",
                    "nova": ["hirda-nova-hermes"],
                },
            }
        },
    )
    assert cfg.cognitive_pane_routing_mode == "shadow"
    assert (
        cfg.cognitive_runtime_bindings["hermes"][
            "principal_pane_name_prefixes"
        ]["fern"]
        == "hirda-fern-hermes"
    )

    with pytest.raises(
        ValueError,
        match="cognitive_pane_routing_mode must be off, shadow, or enforce",
    ):
        StudioConfig(cognitive_pane_routing_mode="magic")

    with pytest.raises(ValueError, match="pane_ids must be a list"):
        StudioConfig(
            cognitive_runtime_bindings={
                "hermes": {
                    "machine_id": "openclaw",
                    "pane_ids": "wH:p1",
                }
            }
        )

    with pytest.raises(
        ValueError,
        match="principal_pane_name_prefixes must be an object",
    ):
        StudioConfig(
            cognitive_runtime_bindings={
                "hermes": {
                    "machine_id": "openclaw",
                    "principal_pane_name_prefixes": ["fern"],
                }
            }
        )

    with pytest.raises(
        ValueError,
        match="principal_pane_name_prefixes values must be",
    ):
        StudioConfig(
            cognitive_runtime_bindings={
                "hermes": {
                    "machine_id": "openclaw",
                    "principal_pane_name_prefixes": {
                        "fern": [],
                    },
                }
            }
        )


def test_n8_settings_fail_closed_for_invalid_mode() -> None:
    with pytest.raises(ValueError, match="cognitive_node_routing_mode"):
        StudioConfig(cognitive_node_routing_mode="magic")


def test_n8_settings_require_explicit_backend_for_model_binding() -> None:
    with pytest.raises(ValueError, match="model_id requires backend_id"):
        StudioConfig(
            cognitive_runtime_bindings={
                "runtime-a": {
                    "machine_id": "node-a",
                    "model_id": "model-a",
                }
            }
        )


def test_main_wires_machine_router_into_cognitive_router() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "mcp_studio"
        / "main.py"
    ).read_text(encoding="utf-8")
    assert "cognitive_router = CognitiveRouter(" in source
    assert "machine_router," in source
    assert '"node_routing": {' in source
