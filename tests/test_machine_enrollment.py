from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_studio.machine_enrollment import MachineEnrollmentManager
from mcp_studio.machine_registry import MachineRegistry
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
                "address": "100.98.34.45",
                "capabilities": ["filesystem", "process", "computer_use"],
                "providers": {
                    "desktop_commander": {"mode": "local"},
                    "computer_use": {"mode": "local"},
                },
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


def candidate(address: str = "100.100.10.20") -> dict:
    return {
        "schema": "hirda-machine-agent-v1",
        "machine_id": "NEWNODE",
        "hostname": "newnode",
        "platform": "linux",
        "architecture": "x86_64",
        "agent_version": "1",
        "capabilities": ["filesystem", "process"],
        "providers": {"desktop_commander": {"available": True, "tool_count": 13}},
        "address": address,
        "endpoint": f"http://{address}:8765",
        "fingerprint": "f" * 64,
    }


@pytest.mark.asyncio
async def test_discovery_creates_pending_without_granting_authority(tmp_path: Path, monkeypatch) -> None:
    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)

    monkeypatch.setattr(
        manager,
        "_tailscale_status_sync",
        lambda: {
            "Peer": {
                "peer1": {
                    "HostName": "newnode",
                    "Online": True,
                    "TailscaleIPs": ["100.100.10.20"],
                }
            }
        },
    )

    async def fake_probe(address: str):
        assert address == "100.100.10.20"
        return candidate(address)

    monkeypatch.setattr(manager, "_probe", fake_probe)
    result = await manager.discover()

    assert result["pending_count"] == 1
    assert result["observed"][0]["status"] == "pending"
    assert registry.contains("NEWNODE") is False
    persisted = json.loads((tmp_path / "enrollment.json").read_text())
    assert "NEWNODE" in persisted["pending"]


def test_approval_is_explicit_restrictive_and_persistent(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    manager._state["pending"]["NEWNODE"] = {
        **candidate(),
        "status": "pending",
        "first_seen_at": "2026-09-24T00:00:00+00:00",
        "last_seen_at": "2026-09-24T00:00:00+00:00",
    }
    manager._save_state()

    result = manager.approve(
        "NEWNODE",
        workspace_map={"demo": "/srv/demo"},
        actor="test",
    )

    assert result["status"] == "approved"
    node = registry.get("NEWNODE")
    assert node.policy == {
        "read": True,
        "write": False,
        "execute": False,
        "destructive": False,
        "fail_closed_unknown": True,
    }
    assert node.workspace_map == {"demo": "/srv/demo"}
    assert node.providers["desktop_commander"]["mode"] == "agent"
    assert node.providers["desktop_commander"]["auth_mode"] == "tailnet_ip"

    # Rebuild from config + sidecar to prove approval survives HIRDA restart.
    registry2 = MachineRegistry(cfg, base_dir=tmp_path)
    MachineEnrollmentManager(cfg, registry2, base_dir=tmp_path)
    restored = registry2.get("NEWNODE")
    assert restored.workspace_map["demo"] == "/srv/demo"
    assert restored.policy["execute"] is False


def test_approval_can_only_narrow_or_explicitly_expand_from_safe_default(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    manager._state["pending"]["NEWNODE"] = candidate()

    manager.approve(
        "NEWNODE",
        policy={"write": True, "execute": True, "destructive": False},
        actor="test",
    )
    node = registry.get("NEWNODE")
    assert node.policy["read"] is True
    assert node.policy["write"] is True
    assert node.policy["execute"] is True
    assert node.policy["destructive"] is False


def test_reject_keeps_machine_out_of_registry(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    manager._state["pending"]["NEWNODE"] = candidate()

    rejected = manager.reject("NEWNODE", reason="not approved", actor="test")

    assert rejected["status"] == "rejected"
    assert registry.contains("NEWNODE") is False
    snapshot = manager.snapshot()
    assert snapshot["pending_count"] == 0
    assert snapshot["rejected"][0]["machine_id"] == "NEWNODE"


def test_configured_machine_wins_over_sidecar_on_restart(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    state = {
        "schema": "hirda-machine-enrollment-v1",
        "pending": {},
        "rejected": {},
        "approved": {
            "openclaw": {
                "machine_id": "openclaw",
                "machine": {
                    "id": "openclaw",
                    "name": "evil-shadow",
                    "local": False,
                    "address": "100.100.10.50",
                    "capabilities": ["filesystem"],
                    "providers": {"desktop_commander": {"mode": "agent", "endpoint": "http://100.100.10.50:8765"}},
                },
            }
        },
        "updated_at": "2026-09-24T00:00:00+00:00",
    }
    (tmp_path / "enrollment.json").write_text(json.dumps(state))

    registry = MachineRegistry(cfg, base_dir=tmp_path)
    MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)

    assert registry.get("openclaw").name == "openclaw"
    assert registry.get("openclaw").local is True


def test_discovery_ignores_non_tailnet_addresses(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    assert manager._address_allowed("100.100.1.2") is True
    assert manager._address_allowed("192.168.1.20") is False
    assert manager._address_allowed("8.8.8.8") is False


def test_gateway_exposes_machine_enrollment_controls(tmp_path: Path) -> None:
    from mcp_studio.db import Database
    from mcp_studio.gateway import GatewaySessionManager
    from mcp_studio.settings import ServerConfig, Settings

    cfg = studio(tmp_path)
    settings = Settings(
        studio=cfg,
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://127.0.0.1:8001/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    enrollment = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)

    class Pool:
        enabled = True

    gateway = GatewaySessionManager(
        settings,
        Database(str(tmp_path / "db.sqlite3")),
        managed_sessions=Pool(),
        machine_enrollment=enrollment,
    )
    names = {tool["name"] for tool in gateway._management_tools()}
    assert {
        "mcpstudio_list_machine_enrollments",
        "mcpstudio_discover_machines",
        "mcpstudio_approve_machine",
        "mcpstudio_reject_machine",
    } <= names


def test_enrollment_snapshot_does_not_expose_sidecar_path(tmp_path: Path) -> None:
    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    snapshot = manager.snapshot()
    assert "state_path" not in snapshot
    assert snapshot["storage"] == "local-sidecar"


@pytest.mark.asyncio
async def test_auto_discovery_loop_runs_immediately(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    cfg = studio(tmp_path)
    registry = MachineRegistry(cfg, base_dir=tmp_path)
    manager = MachineEnrollmentManager(cfg, registry, base_dir=tmp_path)
    called = asyncio.Event()

    async def fake_discover():
        called.set()
        return {"enabled": True}

    monkeypatch.setattr(manager, "discover", fake_discover)
    await manager.start()
    await asyncio.wait_for(called.wait(), timeout=1.0)
    await manager.stop()
    assert manager._task is None


def test_machine_agent_advertises_enrollment_identity() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "hirda_machine_agent.py").read_text()
    assert 'self.path not in {"/health", "/identity"}' in source
    assert '"schema": "hirda-machine-agent-v1"' in source
    assert '"hostname": socket.gethostname()' in source
    assert '"architecture": platform.machine()' in source
    assert '"capabilities": ["filesystem", "process"]' in source
    assert 'if not self._authorized()' in source
