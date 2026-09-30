from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mcp_studio.device_runtime import (
    DeviceIdentityStore,
    DevicePairingClient,
    DeviceRuntime,
    DeviceRuntimeConfig,
    DeviceRuntimeError,
    WorkspaceResolver,
)


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def call(self, *, name, arguments, workspace_key, session_id):
        self.calls.append(
            {
                "name": name,
                "arguments": dict(arguments),
                "workspace_key": workspace_key,
                "session_id": session_id,
            }
        )
        if workspace_key == "missing":
            raise DeviceRuntimeError("workspace_not_configured:missing")
        return {"content": [{"type": "text", "text": "RUNTIME_OK"}]}

    async def close(self):
        self.closed = True


def _config(tmp_path: Path) -> DeviceRuntimeConfig:
    return DeviceRuntimeConfig.from_dict(
        {
            "control_plane_url": "https://hirda.example",
            "name": "JKFASTDEV",
            "identity_path": ".hirda/key.pem",
            "desktop_commander_command": ["desktop-commander"],
            "workspaces": {
                "mcp-studio": "./workspaces/mcp-studio",
            },
            "capabilities": ["filesystem", "process"],
        },
        base_dir=tmp_path,
    )


def test_identity_is_stable_and_private_key_is_not_world_readable(tmp_path):
    path = tmp_path / "identity.pem"
    store = DeviceIdentityStore(path)
    first = store.load_or_create()
    second = store.load_or_create()

    assert first.device_id == second.device_id
    assert first.public_key == second.public_key
    assert first.public_key.startswith("ed25519:")
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_runtime_config_resolves_device_local_paths(tmp_path):
    config = _config(tmp_path)
    assert config.identity_path == str((tmp_path / ".hirda/key.pem").resolve())
    assert config.workspaces["mcp-studio"] == str(
        (tmp_path / "workspaces/mcp-studio").resolve()
    )
    assert config.control_plane_url == "https://hirda.example"


def test_runtime_config_does_not_require_desktop_commander(tmp_path):
    config = DeviceRuntimeConfig.from_dict(
        {"control_plane_url": "https://hirda.example", "identity_path": ".hirda/key", "workspaces": {"demo": "./demo"}},
        base_dir=tmp_path,
    )
    assert config.desktop_commander_command is None
    assert config.workspaces["demo"] == str((tmp_path / "demo").resolve())


def test_workspace_resolver_fails_closed_for_unknown_key(tmp_path):
    resolver = WorkspaceResolver({"known": str(tmp_path / "known")})
    assert resolver.resolve("known") == str((tmp_path / "known").resolve())
    with pytest.raises(DeviceRuntimeError, match="workspace_not_configured:missing"):
        resolver.resolve("missing")


@pytest.mark.asyncio
async def test_pairing_client_uses_same_cross_platform_contract(tmp_path):
    config = _config(tmp_path)
    identity = DeviceIdentityStore(config.identity_path).load_or_create()
    seen = []

    async def handler(request: httpx.Request):
        seen.append(request)
        if request.method == "POST":
            body = __import__("json").loads(request.content)
            assert body["name"] == "JKFASTDEV"
            assert body["public_key"] == identity.public_key
            assert body["capabilities"] == ["filesystem", "hirda_native", "process"]
            return httpx.Response(
                200,
                json={
                    "session_id": "pair_test",
                    "display_code": "12345678",
                    "pairing_url": "https://hirda.example/pair/pair_test?nonce=x",
                    "state": "pairing",
                },
            )
        return httpx.Response(
            200,
            json={"session_id": "pair_test", "state": "trusted"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=config.control_plane_url,
    ) as client:
        pairing = DevicePairingClient(config, identity, http_client=client)
        started = await pairing.start()
        assert started["display_code"] == "12345678"
        status = await pairing.status("pair_test")
        assert status["state"] == "trusted"

    assert [request.url.path for request in seen] == [
        "/api/device-pairing/sessions",
        "/api/device-pairing/sessions/pair_test",
    ]


def test_runtime_signature_authenticates_exact_broker_challenge(tmp_path):
    config = _config(tmp_path)
    identity = DeviceIdentityStore(config.identity_path).load_or_create()
    runtime = DeviceRuntime(config, identity=identity, backend=FakeBackend())
    signing_input = (
        "HIRDA_DEVICE_BROKER_V1\n"
        f"{identity.device_id}\n"
        "chal_test\n"
        "nonce_test\n"
        "2026-09-25T23:59:59+00:00"
    )
    auth = runtime.authenticate_message(
        {
            "type": "challenge",
            "device_id": identity.device_id,
            "challenge_id": "chal_test",
            "signing_input": signing_input,
        }
    )

    raw_public = base64.urlsafe_b64decode(
        identity.public_key.split(":", 1)[1]
        + "=" * (-len(identity.public_key.split(":", 1)[1]) % 4)
    )
    signature = base64.urlsafe_b64decode(
        auth["signature"] + "=" * (-len(auth["signature"]) % 4)
    )
    Ed25519PublicKey.from_public_bytes(raw_public).verify(
        signature,
        signing_input.encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_runtime_dispatches_broker_rpc_to_local_backend(tmp_path):
    config = _config(tmp_path)
    identity = DeviceIdentityStore(config.identity_path).load_or_create()
    backend = FakeBackend()
    runtime = DeviceRuntime(config, identity=identity, backend=backend)

    result = await runtime.dispatch_rpc(
        {
            "type": "rpc_request",
            "request_id": "rpc-1",
            "method": "desktop_commander.call",
            "payload": {
                "name": "read_file",
                "arguments": {"path": "README.md"},
                "workspace_key": "mcp-studio",
                "session_id": "ms-1",
            },
        }
    )

    assert result["ok"] is True
    assert result["result"]["content"][0]["text"] == "RUNTIME_OK"
    assert backend.calls == [
        {
            "name": "read_file",
            "arguments": {"path": "README.md"},
            "workspace_key": "mcp-studio",
            "session_id": "ms-1",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_accepts_native_rpc_method(tmp_path):
    config = _config(tmp_path)
    backend = FakeBackend()
    runtime = DeviceRuntime(config, identity=DeviceIdentityStore(config.identity_path).load_or_create(), backend=backend)
    result = await runtime.dispatch_rpc({
        "request_id": "rpc-native",
        "method": "hirda_native.call",
        "payload": {"name": "read_file", "arguments": {"path": "README.md"}, "workspace_key": "mcp-studio", "session_id": "ms-1"},
    })
    assert result["ok"] is True
    assert backend.calls[0]["name"] == "read_file"


@pytest.mark.asyncio
async def test_runtime_does_not_replay_native_operation_errors(tmp_path):
    config = _config(tmp_path)
    runtime = DeviceRuntime(config, identity=DeviceIdentityStore(config.identity_path).load_or_create())
    calls = []

    class FailedNative:
        async def call(self, **kwargs):
            calls.append("native")
            raise DeviceRuntimeError("native write outcome unknown")

    class Fallback:
        async def call(self, **kwargs):
            calls.append("fallback")
            return {"content": []}

    runtime.backend = FailedNative()
    runtime._fallback_backend = Fallback()
    result = await runtime.dispatch_rpc({
        "request_id": "rpc-no-replay",
        "method": "hirda_native.call",
        "payload": {"name": "write_file", "arguments": {"path": "a", "content": "x"}, "workspace_key": "mcp-studio", "session_id": "ms-1"},
    })
    assert result["ok"] is False
    assert calls == ["native"]


@pytest.mark.asyncio
async def test_runtime_rpc_errors_are_bounded_and_do_not_crash_session(tmp_path):
    config = _config(tmp_path)
    identity = DeviceIdentityStore(config.identity_path).load_or_create()
    runtime = DeviceRuntime(config, identity=identity, backend=FakeBackend())

    unknown = await runtime.dispatch_rpc(
        {
            "request_id": "rpc-2",
            "method": "unknown.method",
            "payload": {},
        }
    )
    assert unknown["ok"] is False
    assert unknown["error"].startswith("unsupported_rpc_method:")

    failed = await runtime.dispatch_rpc(
        {
            "request_id": "rpc-3",
            "method": "desktop_commander.call",
            "payload": {
                "name": "read_file",
                "arguments": {},
                "workspace_key": "missing",
                "session_id": "ms-1",
            },
        }
    )
    assert failed["ok"] is False
    assert "workspace_not_configured:missing" in failed["error"]


def test_https_control_plane_becomes_wss_broker_url(tmp_path):
    config = _config(tmp_path)
    identity = DeviceIdentityStore(config.identity_path).load_or_create()
    runtime = DeviceRuntime(config, identity=identity, backend=FakeBackend())
    assert runtime.broker_url == (
        f"wss://hirda.example/api/device-broker/ws?device_id={identity.device_id}"
    )
