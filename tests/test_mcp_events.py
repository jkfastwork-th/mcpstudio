from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager, make_gateway_router
from mcp_studio.mcp_events import (
    EventProtocolError,
    MCP_EVENTS_PROTOCOL_VERSION,
    McpEventService,
    PEER_MESSAGE_EVENT,
)
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


def _secret(fill: bytes = b"x") -> str:
    return "whsec_" + base64.b64encode(fill * 32).decode("ascii")


async def _service(tmp_path) -> tuple[Database, McpEventService]:
    db = Database(str(tmp_path / "events.sqlite3"))
    await db.init()
    return db, McpEventService(cast(Any, SimpleNamespace()), db)


@pytest.mark.asyncio
async def test_subscription_is_persistent_idempotent_and_oauth_scoped(tmp_path, monkeypatch):
    db, service = await _service(tmp_path)

    async def resolve(_url):
        return None

    async def verify(**_kwargs):
        return None

    monkeypatch.setattr(service, "_resolve_callback", resolve)
    monkeypatch.setattr(service, "_verify_callback", verify)

    params = {
        "name": PEER_MESSAGE_EVENT,
        "arguments": {"sender_principal_id": "oauth-somchai"},
        "delivery": {
            "mode": "webhook",
            "url": "https://receiver.example/callback",
            "secret": _secret(),
        },
        "ttlMs": 60_000,
    }
    first = await service.subscribe(
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        params=params,
    )
    refreshed = {
        **params,
        "delivery": {
            **params["delivery"],
            "secret": _secret(b"y"),
        },
    }
    second = await service.subscribe(
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        params=refreshed,
    )

    assert first["id"] == second["id"]
    assert first["id"].startswith("sub_")
    rows = await db.list_mcp_event_subscriptions(
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        event_name=PEER_MESSAGE_EVENT,
    )
    assert len(rows) == 1
    assert rows[0]["arguments"] == {"sender_principal_id": "oauth-somchai"}
    assert rows[0]["callback_url"] == "https://receiver.example/callback"
    assert rows[0]["delivery_secret"] == _secret(b"y")
    assert rows[0]["previous_delivery_secret"] == _secret()
    assert rows[0]["previous_secret_expires_at"] is not None


@pytest.mark.asyncio
async def test_private_callback_address_is_rejected(tmp_path):
    _db, service = await _service(tmp_path)
    with pytest.raises(EventProtocolError) as exc:
        await service._resolve_callback("https://127.0.0.1/callback")
    assert exc.value.code == -32015
    assert exc.value.data["reason"] == "non_public_address"


def test_standard_webhook_signature_covers_id_timestamp_and_exact_body():
    secret = _secret(b"k")
    body = b'{"eventId":"evt_123","name":"peer.message.created"}'
    headers = McpEventService._sign_headers(
        secret=secret,
        webhook_id="evt_123",
        body=body,
        timestamp=1_700_000_000,
        subscription_id="sub_123",
    )
    key = base64.b64decode(secret[len("whsec_") :])
    expected = base64.b64encode(
        hmac.new(
            key,
            b"evt_123.1700000000." + body,
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    assert headers["webhook-signature"] == "v1," + expected
    assert headers["X-MCP-Subscription-Id"] == "sub_123"


def test_standard_webhook_signature_supports_rotation_overlap():
    current = _secret(b"n")
    previous = _secret(b"o")
    headers = McpEventService._sign_headers(
        secret=current,
        previous_secret=previous,
        webhook_id="evt_rotate",
        body=b"{}",
        timestamp=1_700_000_001,
        subscription_id="sub_rotate",
    )
    signatures = headers["webhook-signature"].split(" ")
    assert len(signatures) == 2
    assert all(item.startswith("v1,") for item in signatures)


@pytest.mark.asyncio
async def test_peer_message_dispatch_delivers_signed_event_and_records_success(
    tmp_path, monkeypatch
):
    db, service = await _service(tmp_path)
    secret = _secret(b"d")
    await db.upsert_mcp_event_subscription(
        subscription_id="sub_live",
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        event_name=PEER_MESSAGE_EVENT,
        arguments={"sender_principal_id": "oauth-somchai"},
        callback_url="https://receiver.example/callback",
        delivery_secret=secret,
        expires_at=None,
    )
    captured: list[dict[str, Any]] = []

    async def post(url, *, body, headers, timeout=5.0):
        captured.append(
            {
                "url": url,
                "body": body,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return 202, b"{}"

    monkeypatch.setattr(service, "_post_callback", post)
    message = {
        "message_id": "pm-live",
        "peer_scope_id": "oauth-scope-main",
        "thread_id": "pt-live",
        "correlation_id": "pc-live",
        "sender_principal_id": "oauth-somchai",
        "recipient_principal_id": "oauth-fern",
        "reply_to": "pm-parent",
        "text": "LIVE_NOTIFY_CERT_REPLY",
        "created_at": "2026-10-02T07:18:48+00:00",
    }

    delivered = await service.dispatch_peer_message(message)
    assert delivered == 1
    assert len(captured) == 1

    payload = json.loads(captured[0]["body"])
    assert payload["name"] == PEER_MESSAGE_EVENT
    assert payload["data"]["message_id"] == "pm-live"
    assert payload["data"]["text"] == "LIVE_NOTIFY_CERT_REPLY"
    assert captured[0]["headers"]["X-MCP-Subscription-Id"] == "sub_live"

    rows = await db.list_mcp_event_subscriptions(
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        event_name=PEER_MESSAGE_EVENT,
    )
    assert rows[0]["last_delivery_at"] is not None
    assert rows[0]["failure_count"] == 0


@pytest.mark.asyncio
async def test_transient_webhook_failure_retries_with_same_event_id(tmp_path, monkeypatch):
    db, service = await _service(tmp_path)
    await db.upsert_mcp_event_subscription(
        subscription_id="sub_retry",
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        event_name=PEER_MESSAGE_EVENT,
        arguments={},
        callback_url="https://receiver.example/callback",
        delivery_secret=_secret(b"r"),
        expires_at=None,
    )
    calls: list[tuple[str, str]] = []
    statuses = [503, 202]

    async def post(_url, *, body, headers, timeout=5.0):
        calls.append((headers["webhook-id"], headers["webhook-signature"]))
        return statuses.pop(0), b"{}"

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(service, "_post_callback", post)
    monkeypatch.setattr("mcp_studio.mcp_events.asyncio.sleep", no_sleep)
    message = {
        "message_id": "pm-retry",
        "peer_scope_id": "oauth-scope-main",
        "thread_id": "pt-retry",
        "correlation_id": "pc-retry",
        "sender_principal_id": "oauth-somchai",
        "recipient_principal_id": "oauth-fern",
        "reply_to": None,
        "text": "retry me",
        "created_at": "2026-10-02T07:30:00+00:00",
    }

    assert await service.dispatch_peer_message(message) == 1
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    rows = await db.list_mcp_event_subscriptions(
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        event_name=PEER_MESSAGE_EVENT,
    )
    assert rows[0]["failure_count"] == 0
    assert rows[0]["last_delivery_at"] is not None


@pytest.mark.asyncio
async def test_peer_event_filters_do_not_cross_sender(tmp_path, monkeypatch):
    db, service = await _service(tmp_path)
    await db.upsert_mcp_event_subscription(
        subscription_id="sub_filter",
        principal_id="oauth-fern",
        peer_scope_id="oauth-scope-main",
        event_name=PEER_MESSAGE_EVENT,
        arguments={"sender_principal_id": "oauth-somchai"},
        callback_url="https://receiver.example/callback",
        delivery_secret=_secret(),
        expires_at=None,
    )

    async def should_not_post(*_args, **_kwargs):
        raise AssertionError("filtered message must not be delivered")

    monkeypatch.setattr(service, "_post_callback", should_not_post)
    delivered = await service.dispatch_peer_message(
        {
            "message_id": "pm-other",
            "peer_scope_id": "oauth-scope-main",
            "thread_id": "pt-other",
            "correlation_id": "pc-other",
            "sender_principal_id": "oauth-joe",
            "recipient_principal_id": "oauth-fern",
            "reply_to": None,
            "text": "not Somchai",
            "created_at": "2026-10-02T07:20:00+00:00",
        }
    )
    assert delivered == 0


def test_event_discovery_and_initialize_bridge_advertise_mcp_events():
    discover = McpEventService.server_discover_result()
    assert discover["supportedVersions"] == [MCP_EVENTS_PROTOCOL_VERSION]
    assert discover["capabilities"]["events"] == {}

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
        },
    }
    result = GatewaySessionManager._augment_initialize_payload(
        payload,
        protocol_version_override=MCP_EVENTS_PROTOCOL_VERSION,
    )
    assert result["result"]["protocolVersion"] == MCP_EVENTS_PROTOCOL_VERSION
    assert result["result"]["capabilities"]["events"] == {}
    assert result["result"]["capabilities"]["logging"] == {}


@pytest.mark.asyncio
async def test_server_discover_is_available_on_authenticated_mcp_endpoint_without_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_STUDIO_GATEWAY_TOKEN", "events-test-token")
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "route.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_GATEWAY_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
        ),
        servers=[
            ServerConfig(
                id="serena-8001",
                name="Serena",
                url="http://127.0.0.1:8999/mcp",
            )
        ],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    await db.init()
    manager = GatewaySessionManager(settings, db)
    app = FastAPI()
    app.include_router(make_gateway_router(settings, db, manager))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://studio.local",
    ) as client:
        response = await client.post(
            "/mcp/serena-8001",
            headers={
                "Authorization": "Bearer events-test-token",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 91,
                "method": "server/discover",
                "params": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 91
    assert payload["result"]["supportedVersions"] == [MCP_EVENTS_PROTOCOL_VERSION]
    assert payload["result"]["capabilities"]["events"] == {}


@pytest.mark.asyncio
async def test_tools_list_is_available_without_session_for_chatgpt_action_discovery(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_STUDIO_GATEWAY_TOKEN", "action-discovery-token")
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "action-discovery.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_GATEWAY_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
        ),
        servers=[
            ServerConfig(
                id="serena-8001",
                name="Serena",
                url="http://127.0.0.1:8999/mcp",
            )
        ],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    await db.init()
    manager = GatewaySessionManager(settings, db, skill_runtime=object())

    async def fail_if_forwarded(**kwargs):
        raise AssertionError("sessionless tools/list must not be forwarded to Serena")

    monkeypatch.setattr(manager, "_send", fail_if_forwarded)
    app = FastAPI()
    app.include_router(make_gateway_router(settings, db, manager))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://studio.local",
    ) as client:
        response = await client.post(
            "/mcp/serena-8001",
            headers={
                "Authorization": "Bearer action-discovery-token",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 92,
                "method": "tools/list",
                "params": {},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 92
    names = [tool["name"] for tool in payload["result"]["tools"]]
    assert "hirda_skill_run" in names


@pytest.mark.asyncio
async def test_initialize_bridges_mcp_events_protocol_to_legacy_upstream(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_STUDIO_GATEWAY_TOKEN", "events-bridge-token")
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "bridge.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_GATEWAY_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
        ),
        servers=[
            ServerConfig(
                id="serena-8001",
                name="Serena",
                url="http://127.0.0.1:8999/mcp",
            )
        ],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    await db.init()
    manager = GatewaySessionManager(settings, db)
    captured: dict[str, Any] = {}

    class FakeClient:
        async def aclose(self):
            return None

    class FakeResponse:
        status_code = 200
        headers = {
            "content-type": "application/json",
            "mcp-session-id": "upstream-session-1",
        }

        async def aread(self):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "Serena", "version": "test"},
                    },
                }
            ).encode("utf-8")

        async def aclose(self):
            return None

    async def fake_send(*, server_url, method, headers, body, stream):
        captured["request"] = json.loads(body.decode("utf-8"))
        return FakeClient(), FakeResponse()

    monkeypatch.setattr(manager, "_send", fake_send)
    app = FastAPI()
    app.include_router(make_gateway_router(settings, db, manager))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://studio.local",
    ) as client:
        response = await client.post(
            "/mcp/serena-8001",
            headers={
                "Authorization": "Bearer events-bridge-token",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_EVENTS_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "openai-mcp", "version": "test"},
                },
            },
        )

    assert response.status_code == 200
    assert captured["request"]["params"]["protocolVersion"] == "2025-11-25"
    payload = response.json()
    assert payload["result"]["protocolVersion"] == MCP_EVENTS_PROTOCOL_VERSION
    assert payload["result"]["capabilities"]["events"] == {}
    gateway_id = response.headers["mcp-session-id"]
    gateway = await db.get_gateway_session(gateway_id)
    assert gateway["protocol_version"] == MCP_EVENTS_PROTOCOL_VERSION
