from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.machine_enrollment import MachineEnrollmentManager
from mcp_studio.node_hardware import (
    NODE_HARDWARE_SCHEMA,
    normalize_hardware_resources,
    probe_hardware_resources,
    refresh_local_node_hardware,
)
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


def studio(tmp_path: Path, *, include_remote: bool = False) -> StudioConfig:
    registry = [
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
    ]
    if include_remote:
        registry.append(
            {
                "id": "REMOTE",
                "name": "remote",
                "enabled": True,
                "local": False,
                "os": "linux",
                "address": "100.100.10.20",
                "capabilities": ["filesystem", "process"],
                "providers": {
                    "desktop_commander": {
                        "mode": "agent",
                        "endpoint": "http://100.100.10.20:8765",
                    }
                },
                "policy": {
                    "read": True,
                    "write": False,
                    "execute": False,
                    "destructive": False,
                    "fail_closed_unknown": True,
                },
            }
        )
    return StudioConfig(
        machine_local_id="openclaw",
        machine_registry=registry,
        machine_enrollment_enabled=True,
        machine_enrollment_state_path=str(tmp_path / "enrollment.json"),
        machine_enrollment_agent_port=8765,
        machine_enrollment_allowed_cidrs=["100.64.0.0/10"],
    )


def hardware_observation(*, logical: int = 8, total: int = 16_000) -> dict:
    return {
        "schema": NODE_HARDWARE_SCHEMA,
        "observed_at": "2026-09-26T01:00:00+00:00",
        "architecture": "test-arch",
        "processor": "test-processor",
        "cpu": {"logical_count": logical},
        "memory": {"total_bytes": total},
        "storage": {
            "total_bytes": 1_000_000,
            "used_bytes": 400_000,
            "free_bytes": 600_000,
        },
    }


def remote_identity(*, logical: int = 8) -> dict:
    return {
        "schema": "hirda-machine-agent-v1",
        "machine_id": "REMOTE",
        "hostname": "remote",
        "platform": "linux",
        "architecture": "test-arch",
        "agent_version": "1",
        "capabilities": ["filesystem", "process"],
        "providers": {
            "desktop_commander": {
                "available": True,
                "tool_count": 13,
            }
        },
        "observations": {
            "hardware.resources": hardware_observation(logical=logical),
        },
        "address": "100.100.10.20",
        "endpoint": "http://100.100.10.20:8765",
        "fingerprint": "f" * 64,
    }


def test_probe_hardware_resources_uses_cross_platform_stdlib(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.node_hardware as hardware

    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(hardware.platform, "machine", lambda: "test-arch")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "test-processor")
    monkeypatch.setattr(hardware, "_physical_memory_bytes", lambda: 32_000)
    monkeypatch.setattr(
        hardware.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100_000, used=40_000, free=60_000),
    )

    result = probe_hardware_resources(
        tmp_path,
        observed_at="2026-09-26T01:02:03+00:00",
    )

    assert result == {
        "schema": NODE_HARDWARE_SCHEMA,
        "observed_at": "2026-09-26T01:02:03+00:00",
        "architecture": "test-arch",
        "processor": "test-processor",
        "cpu": {"logical_count": 12},
        "memory": {"total_bytes": 32_000},
        "storage": {
            "total_bytes": 100_000,
            "used_bytes": 40_000,
            "free_bytes": 60_000,
        },
    }


def test_normalize_hardware_resources_rejects_unknown_schema_and_negative_values() -> None:
    assert normalize_hardware_resources({"schema": "other"}) is None

    raw = hardware_observation()
    raw["cpu"]["logical_count"] = -1
    raw["memory"]["total_bytes"] = "bad"
    normalized = normalize_hardware_resources(raw)

    assert normalized is not None
    assert normalized["cpu"]["logical_count"] is None
    assert normalized["memory"]["total_bytes"] is None
    assert normalized["storage"]["free_bytes"] == 600_000


def test_local_refresh_enriches_existing_node_without_changing_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import mcp_studio.node_hardware as hardware

    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    node = registry.get_node("openclaw")
    policy_before = dict(node.policy)
    providers_before = dict(node.providers)
    capabilities_before = tuple(node.capabilities)
    monkeypatch.setattr(
        hardware,
        "probe_hardware_resources",
        lambda storage_path=None: hardware_observation(logical=16),
    )

    observation = refresh_local_node_hardware(registry, storage_path=tmp_path)

    assert observation is not None
    assert registry.get_node("openclaw") is node
    assert node.policy == policy_before
    assert node.providers == providers_before
    assert node.capabilities == capabilities_before
    facet = registry.lifecycle("openclaw")["enrichment"]["facets"]["hardware.resources"]
    assert facet["source"] == "local_probe"
    assert facet["data"]["cpu"]["logical_count"] == 16


def test_approval_enriches_hardware_after_node_record_exists(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = NodeRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    manager._state["pending"]["REMOTE"] = remote_identity()

    result = manager.approve("REMOTE", actor="n2-test")

    assert result["status"] == "approved"
    lifecycle = registry.lifecycle("REMOTE")
    assert lifecycle["state"] == "enrolled"
    facet = lifecycle["enrichment"]["facets"]["hardware.resources"]
    assert facet["source"] == "machine_agent"
    assert facet["data"]["memory"]["total_bytes"] == 16_000

    approved_record = manager._state["approved"]["REMOTE"]["machine"]
    assert "observations" not in approved_record
    assert "node_registry" not in approved_record.get("metadata", {})


@pytest.mark.asyncio
async def test_registered_discovery_refreshes_same_node_record(tmp_path: Path, monkeypatch) -> None:
    cfg = studio(tmp_path, include_remote=True)
    registry = NodeRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    node = registry.get_node("REMOTE")
    policy_before = dict(node.policy)
    providers_before = dict(node.providers)
    revision_before = registry.lifecycle("REMOTE")["revision"]

    monkeypatch.setattr(
        manager,
        "_tailscale_status_sync",
        lambda: {
            "Peer": {
                "peer1": {
                    "HostName": "remote",
                    "Online": True,
                    "TailscaleIPs": ["100.100.10.20"],
                }
            }
        },
    )

    async def fake_probe(address: str):
        assert address == "100.100.10.20"
        return remote_identity(logical=24)

    monkeypatch.setattr(manager, "_probe", fake_probe)
    result = await manager.discover()

    assert result["observed"][0]["status"] == "registered"
    assert registry.get_node("REMOTE") is node
    assert node.policy == policy_before
    assert node.providers == providers_before
    lifecycle = registry.lifecycle("REMOTE")
    assert lifecycle["revision"] == revision_before + 1
    assert lifecycle["enrichment"]["facets"]["hardware.resources"]["data"]["cpu"]["logical_count"] == 24


def test_machine_agent_advertises_versioned_hardware_observation() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "hirda_machine_agent.py").read_text()
    assert '"observations": {' in source
    assert '"hardware.resources": probe_hardware_resources(Path.cwd())' in source


def test_n2_core_contains_no_product_or_runtime_vendor_hardcoding() -> None:
    source = Path("mcp_studio/node_hardware.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source
