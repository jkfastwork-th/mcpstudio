from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_studio.machine_registry import MachineRegistryError
from mcp_studio.machine_router import MachineCapabilityRouter
from mcp_studio.node_observations import ingest_node_observations
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


def _machine(
    machine_id: str,
    *,
    local: bool,
    execute: bool = True,
) -> dict:
    return {
        "id": machine_id,
        "name": machine_id,
        "enabled": True,
        "local": local,
        "os": "linux",
        "address": "127.0.0.1" if local else f"100.64.0.{10 if machine_id == 'node-a' else 11}",
        "capabilities": ["filesystem", "process"],
        "providers": {},
        "policy": {
            "read": True,
            "write": True,
            "execute": execute,
            "destructive": False,
            "fail_closed_unknown": True,
        },
    }


def studio(
    tmp_path: Path,
    *,
    node_a_execute: bool = True,
    node_b_execute: bool = True,
) -> StudioConfig:
    return StudioConfig(
        machine_local_id="node-a",
        node_health_interval_seconds=15,
        node_health_stale_after_seconds=45,
        machine_registry=[
            _machine("node-a", local=True, execute=node_a_execute),
            _machine("node-b", local=False, execute=node_b_execute),
        ],
    )


def _iso(*, age_seconds: float = 0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat()


def seed_node(
    registry: NodeRegistry,
    node_id: str,
    *,
    load_ratio: float,
    backend_available: bool = True,
    backend_discovered_available: bool = True,
    backend_latency_ms: float = 10.0,
    age_seconds: float = 0,
    node_available: bool = True,
    node_capabilities: list[str] | None = None,
    backend_capabilities: list[str] | None = None,
    model_capabilities: list[str] | None = None,
) -> None:
    observed_at = _iso(age_seconds=age_seconds)
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
                        "available": backend_discovered_available,
                        "binary": "/usr/bin/runtime-x",
                        "version": "runtime-x 1",
                        "capabilities": backend_capabilities
                        or ["text-generation"],
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
                "node": {
                    "capabilities": node_capabilities or ["filesystem"],
                },
                "backends": [
                    {
                        "backend_id": "runtime-x",
                        "capabilities": backend_capabilities
                        or ["text-generation"],
                    }
                ],
                "models": [
                    {
                        "backend_id": "runtime-x",
                        "model_id": "model-a",
                        "capabilities": model_capabilities or ["chat"],
                    }
                ],
            },
            "health.availability": {
                "schema": "hirda-node-health-v1",
                "observed_at": observed_at,
                "node": {
                    "available": node_available,
                    "state": "ready" if node_available else "offline",
                    "load": {
                        "logical_cpu_count": 8,
                        "load_1m": load_ratio * 8,
                        "load_5m": load_ratio * 8,
                        "load_15m": load_ratio * 8,
                        "load_ratio_1m": load_ratio,
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
                        "available": backend_available,
                        "state": "ready"
                        if backend_available
                        else "unhealthy",
                        "latency_ms": backend_latency_ms,
                    }
                ],
            },
        },
        source="n7-test",
    )


def router(registry: NodeRegistry) -> MachineCapabilityRouter:
    return MachineCapabilityRouter(registry, object(), object())


def test_router_selects_lowest_load_eligible_backend(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.8, backend_latency_ms=5)
    seed_node(registry, "node-b", load_ratio=0.2, backend_latency_ms=20)

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
    )

    assert result["schema"] == "hirda-capability-route-v1"
    assert result["eligible_count"] == 2
    assert result["selected"]["node_id"] == "node-b"
    assert result["selected"]["backend_id"] == "runtime-x"
    assert result["selected"]["scope"] == "backend"
    assert result["selected"]["health"]["load_ratio_1m"] == 0.2


def test_machine_policy_is_hard_ceiling_before_load_ranking(tmp_path: Path) -> None:
    registry = NodeRegistry(
        studio(tmp_path, node_b_execute=False),
        base_dir=tmp_path,
    )
    seed_node(registry, "node-a", load_ratio=0.9)
    seed_node(registry, "node-b", load_ratio=0.01)

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
    )

    assert result["selected"]["node_id"] == "node-a"
    denied = [
        item
        for item in result["rejected"]
        if item["node_id"] == "node-b"
    ]
    assert denied[0]["reason"] == "permission_denied"
    assert denied[0]["policy_code"] == "MACHINE_PERMISSION_DENIED"


def test_stale_health_is_never_an_eligible_route(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.01, age_seconds=120)
    seed_node(registry, "node-b", load_ratio=0.8)

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
    )

    assert result["selected"]["node_id"] == "node-b"
    stale = [
        item
        for item in result["rejected"]
        if item["node_id"] == "node-a"
    ]
    assert stale[0]["reason"] == "health_stale"


def test_unhealthy_backend_is_filtered_even_when_node_is_ready(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(
        registry,
        "node-a",
        load_ratio=0.01,
        backend_available=False,
    )
    seed_node(registry, "node-b", load_ratio=0.9)

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
    )

    assert result["selected"]["node_id"] == "node-b"
    rejected = [
        item
        for item in result["rejected"]
        if item["node_id"] == "node-a"
    ]
    assert rejected[0]["reason"] == "backend_unavailable"
    assert rejected[0]["backend_state"] == "unhealthy"


def test_node_scope_capability_requires_fresh_node_but_not_backend_health(
    tmp_path: Path,
) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(
        registry,
        "node-a",
        load_ratio=0.3,
        backend_available=False,
        node_capabilities=["filesystem"],
    )
    seed_node(
        registry,
        "node-b",
        load_ratio=0.4,
        node_capabilities=["filesystem"],
    )

    result = router(registry).resolve_capability_route(
        "filesystem",
        permission="read",
        machine_id="node-a",
    )

    assert result["eligible_count"] == 1
    assert result["selected"]["scope"] == "node"
    assert result["selected"]["backend_id"] is None
    assert result["selected"]["node_id"] == "node-a"


def test_model_route_requires_exact_backend_model_lineage(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.2)

    lifecycle = registry.lifecycle("node-a")
    advertised = lifecycle["enrichment"]["facets"][
        "capabilities.advertised"
    ]["data"]
    forged = dict(advertised)
    forged["models"] = list(advertised["models"]) + [
        {
            "backend_id": "runtime-x",
            "model_id": "forged-model",
            "capabilities": ["chat"],
        }
    ]
    registry.enrich_node(
        "node-a",
        facet="capabilities.advertised",
        data=forged,
        source="forged-test",
        observed_at=_iso(),
    )

    valid = router(registry).resolve_capability_route(
        "chat",
        permission="execute",
        machine_id="node-a",
        backend_id="runtime-x",
        model_id="model-a",
    )
    forged_result = router(registry).resolve_capability_route(
        "chat",
        permission="execute",
        machine_id="node-a",
        backend_id="runtime-x",
        model_id="forged-model",
    )

    assert valid["selected"]["scope"] == "model"
    assert valid["selected"]["model_id"] == "model-a"
    assert forged_result["selected"] is None
    assert forged_result["no_route_reason"] == "no_eligible_route"
    assert forged_result["rejected"][0]["reason"] == "lineage_mismatch"


def test_model_filter_requires_backend_id(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.2)

    with pytest.raises(
        MachineRegistryError,
        match="requires backend_id",
    ):
        router(registry).resolve_capability_route(
            "chat",
            permission="execute",
            model_id="model-a",
        )




def test_n3_unavailable_backend_cannot_be_revived_by_health_claim(
    tmp_path: Path,
) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(
        registry,
        "node-a",
        load_ratio=0.01,
        backend_available=True,
        backend_discovered_available=False,
    )

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
        machine_id="node-a",
    )

    assert result["selected"] is None
    assert result["rejected"][0]["reason"] == "lineage_mismatch"

def test_tie_break_is_deterministic_by_node_id(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.5, backend_latency_ms=10)
    seed_node(registry, "node-b", load_ratio=0.5, backend_latency_ms=10)

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
    )

    assert [item["node_id"] for item in result["eligible"]] == [
        "node-a",
        "node-b",
    ]
    assert result["selected"]["node_id"] == "node-a"


def test_resolver_does_not_bind_session_or_mutate_authority(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.2)
    machine = registry.get_node("node-a")
    policy_before = dict(machine.policy)
    capabilities_before = tuple(machine.capabilities)
    providers_before = dict(machine.providers)

    result = router(registry).resolve_capability_route(
        "text-generation",
        permission="execute",
    )

    assert result["selected"] is not None
    assert machine.policy == policy_before
    assert machine.capabilities == capabilities_before
    assert machine.providers == providers_before




@pytest.mark.asyncio
async def test_machine_snapshot_adds_routing_inputs_without_replacing_legacy_online(
    tmp_path: Path,
) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    seed_node(registry, "node-a", load_ratio=0.2)
    seed_node(registry, "node-b", load_ratio=0.4)

    snapshot = await router(registry).machine_snapshot()
    machines = {item["id"]: item for item in snapshot["machines"]}

    assert "online" in machines["node-a"]
    assert machines["node-a"]["routing_availability"]["fresh"] is True
    assert (
        machines["node-a"]["advertised_capabilities"]["schema"]
        == "hirda-node-capabilities-v1"
    )

def test_route_api_is_machine_surface_but_uses_node_router() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "mcp_studio" / "main.py").read_text(encoding="utf-8")

    assert '@app.post("/api/machines/routes/resolve")' in source
    assert "machine_router.resolve_capability_route(" in source


def test_n7_router_has_no_product_provider_or_model_family_hardcoding() -> None:
    source = Path("mcp_studio/machine_router.py").read_text(
        encoding="utf-8"
    ).casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source
