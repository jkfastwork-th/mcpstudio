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
    session_isolation_enabled = True

    async def descriptor(self, session_id):
        return {"session_id": session_id, "desktop_display": ":5"}

    async def status(self):
        return {"novnc_available": True, "auth_required": False}

    async def tcp_target(self, session_id):
        return ("127.0.0.1", 15900)

    def websocket_target(self):
        return "ws://127.0.0.1:6080"


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


def machine_config_with_remote_computer():
    config = machine_config()
    remote = config[1]
    remote["capabilities"] = ["filesystem", "process", "computer_use"]
    remote["providers"]["computer_use"] = {
        "mode": "agent",
        "endpoint": "http://100.85.206.7:8765",
        "auth_mode": "tailnet_ip",
    }
    remote["policy"]["execute"] = True
    return config


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


@pytest.mark.asyncio
async def test_remote_computer_descriptor_is_machine_bound_and_hides_upstream(tmp_path: Path, monkeypatch):
    registry = MachineRegistry(studio(machine_config_with_remote_computer()), base_dir=tmp_path)
    router = MachineCapabilityRouter(registry, FakeIntegrations(), FakeComputer())
    calls = []

    async def remote_descriptor(machine, *, session_id):
        calls.append((machine.machine_id, session_id))
        return {
            "descriptor": {
                "runtime_mode": "session-isolated-remote",
                "transport": "websocket",
                "desktop_display": "remote:7",
            },
            "websocket_url": f"ws://100.85.206.7:6080/session/{session_id}",
        }

    monkeypatch.setattr(registry, "remote_computer_descriptor", remote_descriptor)
    session = {"metadata": {"machine_id": "JKFASTDEV"}}

    descriptor = await router.computer_descriptor("ms-remote-gui", session)
    transport = await router.computer_transport("ms-remote-gui", session)

    assert descriptor["machine"]["id"] == "JKFASTDEV"
    assert descriptor["websocket_path"] == "/api/computer/vnc/ws/ms-remote-gui"
    assert descriptor["runtime_mode"] == "session-isolated-remote"
    assert "websocket_url" not in descriptor
    assert transport == {
        "mode": "websocket",
        "url": "ws://100.85.206.7:6080/session/ms-remote-gui",
        "machine_id": "JKFASTDEV",
    }
    assert calls == [
        ("JKFASTDEV", "ms-remote-gui"),
        ("JKFASTDEV", "ms-remote-gui"),
    ]


def test_remote_computer_websocket_must_match_machine_address(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config_with_remote_computer()), base_dir=tmp_path)
    machine = registry.get("JKFASTDEV")

    valid = registry._validate_remote_computer_websocket_url(
        machine,
        "ws://100.85.206.7:6080/session/ms-safe",
    )
    assert valid == "ws://100.85.206.7:6080/session/ms-safe"

    with pytest.raises(MachineRegistryError, match="host mismatch"):
        registry._validate_remote_computer_websocket_url(
            machine,
            "ws://100.73.1.126:6080/session/ms-safe",
        )

    with pytest.raises(MachineRegistryError, match="must not contain credentials"):
        registry._validate_remote_computer_websocket_url(
            machine,
            "ws://user:secret@100.85.206.7:6080/session/ms-safe",
        )


@pytest.mark.asyncio
async def test_remote_visual_fallback_uses_bound_machine_not_local_vnc(tmp_path: Path, monkeypatch):
    registry = MachineRegistry(studio(machine_config_with_remote_computer()), base_dir=tmp_path)
    computer = TrackingComputer()
    router = MachineCapabilityRouter(
        registry,
        FakeBrowserIntegrations(error=RuntimeError("canvas-only remote UI")),
        computer,
    )

    async def remote_descriptor(machine, *, session_id):
        return {
            "descriptor": {
                "runtime_mode": "session-isolated-remote",
                "transport": "websocket",
                "desktop_display": "remote:9",
            },
            "websocket_url": f"ws://100.85.206.7:6080/session/{session_id}",
        }

    monkeypatch.setattr(registry, "remote_computer_descriptor", remote_descriptor)
    result = await router.call_backend_tool(
        "hirda__openbrowser__click",
        {"index": 1},
        context={
            "machine_id": "JKFASTDEV",
            "managed_session_id": "ms-remote-browser",
            "permission_class": "execute",
        },
    )
    payload = __import__("json").loads(result["content"][0]["text"])
    assert payload["machine_id"] == "JKFASTDEV"
    assert payload["computer_use"]["websocket_path"].endswith("/ms-remote-browser")
    assert payload["computer_use"]["runtime_mode"] == "session-isolated-remote"
    assert computer.calls == []

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

class FakeBrowserIntegrations(FakeIntegrations):
    def __init__(self, *, result=None, error=None):
        super().__init__()
        self.result = result or {"content": [{"type": "text", "text": '{"ok":true}'}]}
        self.error = error

    def backend_tools(self):
        return [
            {
                "name": "hirda__openbrowser__click",
                "inputSchema": {"type": "object"},
            }
        ]

    def backend_permission(self, name):
        if name == "hirda__openbrowser__click":
            return "execute"
        return None

    def is_backend_tool(self, name):
        return name == "hirda__openbrowser__click"

    def backend_route(self, name):
        if name == "hirda__openbrowser__click":
            return ("openbrowser", "click")
        return None

    async def call_backend_tool(self, name, arguments, *, context):
        self.local_calls.append((name, arguments, context))
        if self.error is not None:
            raise self.error
        return self.result


class TrackingComputer(FakeComputer):
    def __init__(self):
        self.calls = []

    async def descriptor(self, session_id):
        self.calls.append(session_id)
        return {
            "session_id": session_id,
            "viewer_url": "/computer/novnc/vnc.html",
            "websocket_path": f"/api/computer/vnc/ws/{session_id}",
            "novnc_available": True,
            "auth_required": False,
            "desktop_display": ":5",
            "cdp_port": 9225,
            "runtime_mode": "session-isolated",
        }


def test_router_exposes_visual_browser_fallback_as_execute_tool(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    router = MachineCapabilityRouter(registry, FakeBrowserIntegrations(), TrackingComputer())
    names = {tool["name"] for tool in router.backend_tools()}
    assert "hirda__browser__visual_fallback" in names
    assert router.is_backend_tool("hirda__browser__visual_fallback") is True
    assert router.backend_permission("hirda__browser__visual_fallback") == "execute"


@pytest.mark.asyncio
async def test_openbrowser_failure_escalates_to_session_vnc(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    computer = TrackingComputer()
    router = MachineCapabilityRouter(
        registry,
        FakeBrowserIntegrations(error=RuntimeError("dom target missing")),
        computer,
    )
    result = await router.call_backend_tool(
        "hirda__openbrowser__click",
        {"index": 42},
        context={
            "machine_id": "openclaw",
            "managed_session_id": "ms-browser-1",
            "workspace_key": "mcp-studio",
            "project_path": "/home/alfred/mcp-studio",
            "permission_class": "execute",
        },
    )
    import json
    payload = json.loads(result["content"][0]["text"])
    assert result["isError"] is True
    assert payload["action_completed"] is False
    assert payload["fallback_required"] is True
    assert payload["fallback_mode"] == "computer_use_vnc"
    assert payload["routing_strategy"] == "dom_cdp_first_visual_fallback"
    assert payload["automatic"] is True
    assert payload["failed_tool"] == "hirda__openbrowser__click"
    assert payload["computer_use"]["websocket_path"].endswith("/ms-browser-1")
    assert "vnc_port" not in payload["computer_use"]
    assert computer.calls == ["ms-browser-1"]


@pytest.mark.asyncio
async def test_openbrowser_soft_error_escalates_to_session_vnc(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    router = MachineCapabilityRouter(
        registry,
        FakeBrowserIntegrations(
            result={"content": [{"type": "text", "text": "Error: element detached"}], "isError": False}
        ),
        TrackingComputer(),
    )
    result = await router.call_backend_tool(
        "hirda__openbrowser__click",
        {"index": 7},
        context={"machine_id": "openclaw", "managed_session_id": "ms-browser-soft", "permission_class": "execute"},
    )
    payload = __import__("json").loads(result["content"][0]["text"])
    assert result["isError"] is True
    assert payload["fallback_required"] is True
    assert "element detached" in payload["reason"]


@pytest.mark.asyncio
async def test_openbrowser_success_stays_on_dom_cdp_path(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    computer = TrackingComputer()
    expected = {"content": [{"type": "text", "text": '{"ok":true,"action":"click"}'}]}
    router = MachineCapabilityRouter(
        registry,
        FakeBrowserIntegrations(result=expected),
        computer,
    )
    result = await router.call_backend_tool(
        "hirda__openbrowser__click",
        {"index": 3},
        context={"machine_id": "openclaw", "managed_session_id": "ms-browser-dom"},
    )
    assert result == expected
    assert computer.calls == []


@pytest.mark.asyncio
async def test_explicit_visual_fallback_returns_descriptor_without_claiming_action(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    router = MachineCapabilityRouter(registry, FakeBrowserIntegrations(), TrackingComputer())
    result = await router.call_backend_tool(
        "hirda__browser__visual_fallback",
        {"reason": "canvas-only UI", "failed_tool": "hirda__openbrowser__state"},
        context={"machine_id": "openclaw", "managed_session_id": "ms-browser-explicit"},
    )
    payload = __import__("json").loads(result["content"][0]["text"])
    assert result["isError"] is False
    assert payload["fallback_required"] is True
    assert payload["action_completed"] is False
    assert payload["automatic"] is False
    assert payload["reason"] == "canvas-only UI"


@pytest.mark.asyncio
async def test_read_failure_requires_separate_execute_escalation(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    computer = TrackingComputer()
    router = MachineCapabilityRouter(
        registry,
        FakeBrowserIntegrations(error=RuntimeError("dom unavailable")),
        computer,
    )
    result = await router.call_backend_tool(
        "hirda__openbrowser__click",
        {"index": 4},
        context={
            "machine_id": "openclaw",
            "managed_session_id": "ms-browser-read",
            "permission_class": "read",
        },
    )
    payload = __import__("json").loads(result["content"][0]["text"])
    assert result["isError"] is True
    assert payload["fallback_required"] is True
    assert payload["fallback_tool"] == "hirda__browser__visual_fallback"
    assert payload["fallback_permission_required"] == "execute"
    assert payload["computer_use"] is None
    assert computer.calls == []


@pytest.mark.asyncio
async def test_remote_machine_cannot_use_local_visual_browser_fallback(tmp_path: Path):
    registry = MachineRegistry(studio(machine_config()), base_dir=tmp_path)
    router = MachineCapabilityRouter(registry, FakeBrowserIntegrations(), TrackingComputer())
    with pytest.raises(MachineRegistryError, match="Computer Use browser fallback is unavailable"):
        await router.call_backend_tool(
            "hirda__browser__visual_fallback",
            {},
            context={"machine_id": "JKFASTDEV", "managed_session_id": "ms-remote-browser"},
        )
