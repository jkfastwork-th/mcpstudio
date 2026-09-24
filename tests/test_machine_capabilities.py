from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.machine_registry import MachineRegistry, MachineRegistryError
from mcp_studio.machine_router import MachineCapabilityRouter


class FakeDB:
    def __init__(self, session: dict):
        self.session = dict(session)

    async def get_managed_session(self, session_id: str):
        if session_id != self.session["id"]:
            raise KeyError(session_id)
        return dict(self.session)

    async def update_managed_session_metadata(self, session_id: str, metadata: dict):
        if session_id != self.session["id"]:
            raise KeyError(session_id)
        self.session["metadata"] = dict(metadata)
        return dict(self.session)


class FakeIntegrations:
    def __init__(self):
        self.local_calls = []

    def backend_tools(self):
        return [{"name": "hirda__desktop_commander__read_file", "inputSchema": {"type": "object"}}]

    def backend_permission(self, name):
        return "read"

    def is_backend_tool(self, name):
        return name == "hirda__desktop_commander__read_file"

    def backend_route(self, name):
        if self.is_backend_tool(name):
            return ("desktop-commander", "read_file")
        return None

    async def call_backend_tool(self, name, arguments, *, context):
        self.local_calls.append((name, arguments, context))
        return {"content": [{"type": "text", "text": "LOCAL"}]}


class FakeComputer:
    async def descriptor(self, session_id):
        return {"session_id": session_id, "desktop_display": ":5"}


def studio(machine_registry, local_id="openclaw"):
    return SimpleNamespace(machine_registry=machine_registry, machine_local_id=local_id)


def machine_config():
    return [
        {
            "id": "openclaw",
            "name": "openclaw",
            "local": True,
            "capabilities": ["filesystem", "computer_use"],
            "policy": {
                "read": True,
                "write": True,
                "execute": True,
                "destructive": False,
                "fail_closed_unknown": True,
            },
            "providers": {
                "desktop_commander": {"mode": "local"},
                "computer_use": {"mode": "local"},
            },
        },
        {
            "id": "JKFASTDEV",
            "name": "JKFASTDEV",
            "os": "windows",
            "address": "100.85.206.7",
            "capabilities": ["filesystem", "process"],
            "policy": {
                "read": True,
                "write": True,
                "execute": False,
                "destructive": False,
                "fail_closed_unknown": True,
            },
            "providers": {
                "desktop_commander": {
                    "mode": "agent",
                    "endpoint": "http://100.85.206.7:8765",
                    "auth_mode": "tailnet_ip",
                }
            },
            "workspace_map": {"mcp-studio": r"C:\Users\itpla\HIRDA\workspaces\mcp-studio"},
        },
    ]


def test_registry_resolves_local_and_remote_workspace(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    local = registry.get("openclaw")
    remote = registry.get("JKFASTDEV")
    assert local.local is True
    assert registry.workspace_root(local, "demo", "/tmp/demo") == "/tmp/demo"
    assert registry.workspace_root(remote, "mcp-studio", "/ignored") == r"C:\Users\itpla\HIRDA\workspaces\mcp-studio"
    with pytest.raises(MachineRegistryError, match="not mapped"):
        registry.workspace_root(remote, "unknown", "/ignored")


@pytest.mark.asyncio
async def test_bind_session_persists_machine_metadata(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    db = FakeDB({
        "id": "ms-1",
        "workspace_key": "mcp-studio",
        "project_path": "/home/alfred/mcp-studio",
        "metadata": {},
    })
    result = await registry.bind_session(db, "ms-1", "JKFASTDEV", actor="test")
    assert result["session"]["metadata"]["machine_id"] == "JKFASTDEV"
    assert result["machine"]["id"] == "JKFASTDEV"
    assert result["session"]["metadata"]["machine_bound_by"] == "test"


@pytest.mark.asyncio
async def test_router_uses_local_backend_for_local_machine(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    integrations = FakeIntegrations()
    router = MachineCapabilityRouter(registry, integrations, FakeComputer())
    result = await router.call_backend_tool(
        "hirda__desktop_commander__read_file",
        {"path": "README.md"},
        context={
            "machine_id": "openclaw",
            "managed_session_id": "ms-1",
            "workspace_key": "mcp-studio",
            "project_path": "/home/alfred/mcp-studio",
            "policy": {"scope": "workspace"},
        },
    )
    assert result["content"][0]["text"] == "LOCAL"
    assert len(integrations.local_calls) == 1


@pytest.mark.asyncio
async def test_router_uses_remote_agent_for_remote_machine(tmp_path: Path, monkeypatch):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    integrations = FakeIntegrations()
    router = MachineCapabilityRouter(registry, integrations, FakeComputer())
    calls = []

    async def remote_call(machine, **kwargs):
        calls.append((machine.machine_id, kwargs))
        return {"content": [{"type": "text", "text": "REMOTE"}]}

    monkeypatch.setattr(registry, "remote_call", remote_call)
    result = await router.call_backend_tool(
        "hirda__desktop_commander__read_file",
        {"path": "notes.txt"},
        context={
            "machine_id": "JKFASTDEV",
            "managed_session_id": "ms-2",
            "workspace_key": "mcp-studio",
            "project_path": "/home/alfred/mcp-studio",
            "policy": {"scope": "workspace"},
        },
    )
    assert result["content"][0]["text"] == "REMOTE"
    assert calls[0][0] == "JKFASTDEV"
    assert calls[0][1]["tool_name"] == "read_file"
    assert integrations.local_calls == []


@pytest.mark.asyncio
async def test_remote_machine_cannot_fall_through_to_local_computer_use(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    router = MachineCapabilityRouter(registry, FakeIntegrations(), FakeComputer())
    session = {"metadata": {"machine_id": "JKFASTDEV"}}
    with pytest.raises(MachineRegistryError, match="Computer Use backend is unavailable"):
        await router.computer_descriptor("ms-2", session)

def test_machine_policy_is_hard_ceiling_for_remote_machine(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    session = {"metadata": {"machine_id": "JKFASTDEV"}}

    read = registry.authorize_session(session, "read")
    execute = registry.authorize_session(session, "execute")
    destructive = registry.authorize_session(session, "destructive")

    assert read.allowed is True
    assert execute.allowed is False
    assert execute.code == "MACHINE_PERMISSION_DENIED"
    assert execute.machine_id == "JKFASTDEV"
    assert destructive.allowed is False


def test_machine_policy_preserves_local_execute_but_denies_destructive(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    session = {"metadata": {"machine_id": "openclaw"}}

    assert registry.authorize_session(session, "execute").allowed is True
    denied = registry.authorize_session(session, "destructive")
    assert denied.allowed is False
    assert denied.code == "MACHINE_PERMISSION_DENIED"


def test_machine_policy_unknown_is_fail_closed(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    decision = registry.authorize_session(
        {"metadata": {"machine_id": "JKFASTDEV"}}, "unknown"
    )
    assert decision.allowed is False
    assert decision.code == "MACHINE_PERMISSION_UNCLASSIFIED"


def test_machine_public_snapshot_exposes_policy_without_secrets(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    remote = registry.get("JKFASTDEV").public()
    assert remote["policy"] == {
        "read": True,
        "write": True,
        "execute": False,
        "destructive": False,
        "fail_closed_unknown": True,
    }
    assert "token" not in remote["providers"]["desktop_commander"]


def test_invalid_machine_policy_is_rejected(tmp_path: Path):
    broken = machine_config()
    broken[1]["policy"] = {"execute": "yes"}
    with pytest.raises(MachineRegistryError, match="execute must be boolean"):
        MachineRegistry(studio(broken), base_dir=tmp_path)
