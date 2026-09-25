from pathlib import Path

from mcp_studio.machine_enrollment import MachineEnrollmentManager
from mcp_studio.machine_registry import MachineRegistry
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


def studio(tmp_path: Path) -> StudioConfig:
    return StudioConfig(
        machine_local_id="openclaw",
        machine_registry=[
            {
                "id": "openclaw",
                "name": "openclaw",
                "enabled": True,
                "local": True,
                "os": "linux",
                "address": "127.0.0.1",
                "capabilities": ["filesystem", "process"],
                "providers": {"desktop_commander": {"mode": "local"}},
                "policy": {
                    "read": True,
                    "write": True,
                    "execute": True,
                    "destructive": False,
                    "fail_closed_unknown": True,
                },
            }
        ],
        machine_enrollment_enabled=True,
        machine_enrollment_state_path=str(tmp_path / "enrollment.json"),
        machine_enrollment_agent_port=8765,
        machine_enrollment_allowed_cidrs=["100.64.0.0/10"],
    )


def candidate() -> dict:
    address = "100.100.10.20"
    return {
        "schema": "hirda-machine-agent-v1",
        "machine_id": "NEWNODE",
        "hostname": "newnode",
        "platform": "linux",
        "architecture": "x86_64",
        "agent_version": "1",
        "capabilities": ["filesystem", "process"],
        "providers": {
            "desktop_commander": {"available": True, "tool_count": 13},
        },
        "address": address,
        "endpoint": f"http://{address}:8765",
        "fingerprint": "f" * 64,
    }


def test_node_registry_is_machine_registry_compatible(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)

    assert isinstance(registry, MachineRegistry)
    node = registry.get_node("openclaw")
    assert registry.contains("openclaw") is True
    assert registry.contains_node("openclaw") is True
    assert registry.list_nodes() == registry.list()
    assert node.public()["id"] == "openclaw"

    lifecycle = registry.lifecycle("openclaw")
    assert lifecycle["schema"] == "hirda-node-registry-v1"
    assert lifecycle["node_id"] == "openclaw"
    assert lifecycle["state"] == "registered"
    assert lifecycle["enrichment"]["state"] == "pending"


def test_enrollment_creates_node_record_immediately_and_restores_it(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = NodeRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    manager._state["pending"]["NEWNODE"] = {
        **candidate(),
        "status": "pending",
        "first_seen_at": "2026-09-26T00:00:00+00:00",
        "last_seen_at": "2026-09-26T00:00:00+00:00",
    }

    result = manager.approve("NEWNODE", actor="n1-test")

    assert result["status"] == "approved"
    assert registry.contains_node("NEWNODE") is True
    lifecycle = registry.lifecycle("NEWNODE")
    assert lifecycle["state"] == "enrolled"
    assert lifecycle["enrichment"]["state"] == "pending"
    assert lifecycle["source"] == "enrollment"
    assert registry.get_node("NEWNODE").metadata["enrollment"]["approved_by"] == "n1-test"

    registry2 = NodeRegistry(cfg, base_dir=tmp_path)
    MachineEnrollmentManager(cfg, registry2, base_dir=tmp_path)
    restored = registry2.lifecycle("NEWNODE")
    assert restored["state"] == "enrolled"
    assert restored["enrolled_at"] == lifecycle["enrolled_at"]


def test_discovery_enriches_same_record_without_replacing_policy_or_provider(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    before = registry.get_node("openclaw")
    policy_before = dict(before.policy)
    providers_before = dict(before.providers)
    revision_before = registry.lifecycle("openclaw")["revision"]

    enriched = registry.enrich_node(
        "openclaw",
        facet="hardware.resources",
        data={"cpu_logical": 16, "memory_bytes": 34359738368},
        source="n2-probe",
    )

    assert enriched is before
    assert enriched.policy == policy_before
    assert enriched.providers == providers_before
    lifecycle = registry.lifecycle("openclaw")
    assert lifecycle["revision"] == revision_before + 1
    assert lifecycle["enrichment"]["state"] == "partial"
    facet = lifecycle["enrichment"]["facets"]["hardware.resources"]
    assert facet["source"] == "n2-probe"
    assert facet["data"]["cpu_logical"] == 16


def test_arbitrary_runtime_and_capability_names_remain_vendor_neutral(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    node = registry.register_node(
        {
            "id": "AI-NODE",
            "name": "AI Node",
            "enabled": True,
            "local": False,
            "os": "linux",
            "capabilities": ["inference", "filesystem"],
            "providers": {
                "runtime-provider-x": {"mode": "local"},
                "filesystem-provider-y": {"mode": "local"},
            },
            "policy": {
                "read": True,
                "write": False,
                "execute": False,
                "destructive": False,
                "fail_closed_unknown": True,
            },
        },
        source="runtime",
    )

    assert "runtime-provider-x" in node.providers
    assert "inference" in node.capabilities
    registry.enrich_node(
        "AI-NODE",
        facet="runtime.backends",
        data={"items": [{"provider": "runtime-provider-x", "models": []}]},
        source="n3-probe",
    )
    items = registry.lifecycle("AI-NODE")["enrichment"]["facets"]["runtime.backends"]["data"]["items"]
    assert items[0]["provider"] == "runtime-provider-x"


def test_node_registry_core_contains_no_product_or_vendor_hardcoding() -> None:
    source = Path("mcp_studio/node_registry.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source


def test_product_surface_keeps_machine_api_while_control_plane_uses_node_registry() -> None:
    source = Path("mcp_studio/main.py").read_text(encoding="utf-8")
    assert 'machine_registry = NodeRegistry(' in source
    assert '@app.get("/api/machines")' in source
