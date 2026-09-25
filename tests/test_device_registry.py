from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_studio.device_broker import DeviceBroker
from mcp_studio.device_pairing import DevicePairingManager
from mcp_studio.machine_registry import MachineRegistry
from mcp_studio.machine_router import MachineCapabilityRouter


def _studio(tmp_path: Path):
    return SimpleNamespace(
        machine_registry=[],
        machine_local_id="openclaw",
        device_pairing_enabled=True,
        device_pairing_state_path=str(tmp_path / "pairing.json"),
        device_pairing_secret_path=str(tmp_path / "pairing.key"),
        device_pairing_base_url="https://hirda.example/pair",
        device_pairing_ttl_seconds=300,
        device_pairing_max_attempts=5,
        device_broker_enabled=True,
        device_broker_challenge_ttl_seconds=30,
        device_broker_session_ttl_seconds=900,
        device_broker_heartbeat_timeout_seconds=45,
        device_broker_max_message_bytes=262144,
    )


def _trusted_device(tmp_path: Path):
    studio = _studio(tmp_path)
    pairing = DevicePairingManager(studio, base_dir=tmp_path)
    private = Ed25519PrivateKey.generate()
    raw_public = private.public_key().public_bytes_raw()
    public_key = "ed25519:" + base64.urlsafe_b64encode(raw_public).decode("ascii").rstrip("=")
    started = pairing.start(
        name="JKFASTDEV",
        platform="windows",
        architecture="x86_64",
        public_key=public_key,
        capabilities=["filesystem", "process", "computer_use"],
        runtime_version="1.0.0",
    )
    nonce = started["pairing_url"].split("?nonce=", 1)[1]
    pairing.verify(started["session_id"], code=started["display_code"], nonce=nonce)
    device = pairing.approve(started["session_id"], actor="owner")
    broker = DeviceBroker(studio, pairing)
    registry = MachineRegistry(
        studio,
        base_dir=tmp_path,
        device_pairing=pairing,
        device_broker=broker,
    )
    return studio, pairing, broker, registry, private, device


class FakeDB:
    def __init__(self):
        self.session = {
            "id": "ms-1",
            "workspace_key": "mcp-studio",
            "project_path": "/not-used-on-remote-device",
            "metadata": {},
        }

    async def get_managed_session(self, session_id: str):
        if session_id != self.session["id"]:
            raise KeyError(session_id)
        return dict(self.session)

    async def update_managed_session_metadata(self, session_id: str, metadata: dict):
        if session_id != self.session["id"]:
            raise KeyError(session_id)
        self.session["metadata"] = dict(metadata)
        return dict(self.session)


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = []
        self.on_send = None

    async def send_json(self, message):
        self.sent.append(dict(message))
        if self.on_send is not None:
            await self.on_send(dict(message))

    async def close(self, *, code: int, reason: str):
        self.closed.append((code, reason))


class FakeIntegrations:
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


class FakeComputer:
    session_isolation_enabled = False


def test_trusted_device_materializes_as_dynamic_machine_with_safe_policy(tmp_path):
    _, _, _, registry, _, device = _trusted_device(tmp_path)

    machine = registry.get(device["device_id"])
    assert machine.name == "JKFASTDEV"
    assert machine.local is False
    assert machine.address is None
    assert machine.os_name == "windows"
    assert machine.providers["device_broker"]["mode"] == "broker"
    assert machine.providers["desktop_commander"]["mode"] == "broker"
    assert machine.metadata["registry_source"] == "device_pairing"
    assert machine.metadata["dynamic"] is True
    assert machine.policy == {
        "read": True,
        "write": False,
        "execute": False,
        "destructive": False,
        "fail_closed_unknown": True,
    }


@pytest.mark.asyncio
async def test_dynamic_machine_online_state_comes_from_broker_connection(tmp_path):
    _, pairing, broker, registry, _, device = _trusted_device(tmp_path)

    offline = await registry.snapshot()
    item = next(x for x in offline["machines"] if x["id"] == device["device_id"])
    assert item["online"] is False

    socket = FakeSocket()
    connection = await broker.register_connection(device["device_id"], socket, remote="127.0.0.1")
    online = await registry.snapshot()
    item = next(x for x in online["machines"] if x["id"] == device["device_id"])
    assert item["online"] is True
    assert item["provider_health"]["device_broker"]["ready"] is True

    await broker.unregister_connection(device["device_id"], connection["connection_id"])
    pairing.revoke(device["device_id"], actor="owner")
    registry.sync_trusted_devices()
    assert not registry.contains(device["device_id"])


@pytest.mark.asyncio
async def test_broker_machine_binding_does_not_require_static_workspace_map(tmp_path):
    _, _, _, registry, _, device = _trusted_device(tmp_path)
    db = FakeDB()

    bound = await registry.bind_session(
        db,
        "ms-1",
        device["device_id"],
        actor="test",
    )
    assert bound["session"]["metadata"]["machine_id"] == device["device_id"]

    read = registry.authorize_session(bound["session"], "read")
    write = registry.authorize_session(bound["session"], "write")
    assert read.allowed is True
    assert write.allowed is False


@pytest.mark.asyncio
async def test_desktop_commander_routes_over_authenticated_broker_rpc(tmp_path):
    _, _, broker, registry, _, device = _trusted_device(tmp_path)
    socket = FakeSocket()
    connection = await broker.register_connection(device["device_id"], socket, remote="127.0.0.1")

    async def respond(message):
        if message.get("type") != "rpc_request":
            return
        assert message["method"] == "desktop_commander.call"
        assert message["payload"]["workspace_key"] == "mcp-studio"
        assert "workspace_root" not in message["payload"]
        await broker.resolve_rpc_response(
            device["device_id"],
            connection["connection_id"],
            {
                "type": "rpc_response",
                "request_id": message["request_id"],
                "ok": True,
                "result": {"content": [{"type": "text", "text": "REMOTE_OK"}]},
            },
        )

    socket.on_send = respond
    router = MachineCapabilityRouter(registry, FakeIntegrations(), FakeComputer())
    result = await router.call_backend_tool(
        "hirda__desktop_commander__read_file",
        {"path": "README.md"},
        context={
            "machine_id": device["device_id"],
            "workspace_key": "mcp-studio",
            "project_path": "/host/path/that/must/not/be-sent",
            "managed_session_id": "ms-1",
            "policy": {"scope": "workspace"},
        },
    )

    assert result["content"][0]["text"] == "REMOTE_OK"
    assert any(message.get("type") == "rpc_request" for message in socket.sent)
