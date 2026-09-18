from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


class FakeClient:
    async def aclose(self):
        return None


def settings(tmp_path: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "db.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_TEST_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
            openai_compatibility_enabled=True,
            openai_connector_reclaim_enabled=True,
            worker_server_id="serena-8001",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://upstream.invalid/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


def request(*, explicit: str | None = None, ua: str = "ChatGPT-MCP/1.0", extra_headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    headers = [
        (b"authorization", b"Bearer test-secret"),
        (b"user-agent", ua.encode()),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
    ]
    if explicit:
        headers.append((b"x-mcp-studio-client-id", explicit.encode()))
    headers.extend(extra_headers or [])
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "https",
        "path": "/mcp/serena-8001", "raw_path": b"/mcp/serena-8001", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 1234), "server": ("test", 443),
    })


def payload(name: str = "ChatGPT") -> dict:
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": name, "version": "2026.09"},
        },
    }


def test_bearer_identity_is_connector_scoped(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_TOKEN"] = "test-secret"
    manager = GatewaySessionManager(settings(tmp_path), Database(str(tmp_path / "db.sqlite3")))
    token, client_id, client_type, scope, source = manager.authenticate(request())
    assert token == "test-secret"
    assert client_id.startswith("bearer-")
    assert client_type == "mcp-http"
    assert scope == "connector"
    assert source == "bearer-fingerprint"
    assert "test-secret" not in client_id


def test_explicit_identity_is_marked_explicit(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_TOKEN"] = "test-secret"
    manager = GatewaySessionManager(settings(tmp_path), Database(str(tmp_path / "db.sqlite3")))
    _, client_id, _, scope, source = manager.authenticate(request(explicit="chat-123"))
    assert client_id == "chat-123"
    assert scope == "explicit"
    assert source == "x-mcp-studio-client-id"


def test_openai_observation_stores_names_not_secret_values(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_TOKEN"] = "test-secret"
    manager = GatewaySessionManager(settings(tmp_path), Database(str(tmp_path / "db.sqlite3")))
    req = request(extra_headers=[(b"x-openai-request-id", b"sensitive-request-value"), (b"cf-ray", b"abc123")])
    obs = manager.client_observation(req, payload(), identity_scope="connector", identity_source="bearer-fingerprint")
    assert obs["openai_like"] is True
    assert obs["client_info_name"] == "ChatGPT"
    assert obs["identity_scope"] == "connector"
    assert obs["conversation_identity_available"] is False
    assert "x-openai-request-id" in obs["observed_header_names"]
    assert "cf-ray" in obs["observed_header_names"]
    dumped = json.dumps(obs)
    assert "sensitive-request-value" not in dumped
    assert "test-secret" not in dumped


@pytest.mark.asyncio
async def test_connector_transport_reclaims_logical_session_and_marks_scope(tmp_path: Path, monkeypatch):
    os.environ["MCP_STUDIO_TEST_TOKEN"] = "test-secret"
    st = settings(tmp_path)
    db = Database(st.studio.database)
    await db.init()
    manager = GatewaySessionManager(st, db)
    seq = iter(["up-A", "up-B"])

    async def fake_send(**kwargs):
        up = next(seq)
        return FakeClient(), httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": up},
            content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
            request=httpx.Request("POST", "http://upstream.invalid/mcp"),
        )

    monkeypatch.setattr(manager, "_send", fake_send)
    body = json.dumps(payload()).encode()
    req1 = request()
    _, cid1, ctype1, scope1, source1 = manager.authenticate(req1)
    first = await manager.initialize(
        request=req1, server=st.servers[0], body=body, jsonrpc=payload(),
        client_id=cid1, client_type=ctype1, identity_scope=scope1, identity_source=source1,
    )
    sid1 = first.headers["x-mcp-studio-session-id"]
    assert first.headers["x-mcp-studio-identity-scope"] == "connector"
    assert first.headers["x-mcp-studio-client-class"] == "openai-like"
    gw1 = first.headers["mcp-session-id"]
    await db.close_gateway_session(gw1)
    await db.disconnect_session(sid1)

    req2 = request()
    _, cid2, ctype2, scope2, source2 = manager.authenticate(req2)
    second = await manager.initialize(
        request=req2, server=st.servers[0], body=body, jsonrpc=payload(),
        client_id=cid2, client_type=ctype2, identity_scope=scope2, identity_source=source2,
    )
    assert second.headers["x-mcp-studio-session-id"] == sid1
    assert second.headers["x-mcp-studio-reclaimed"] == "true"
    stored = await db.get_gateway_session(second.headers["mcp-session-id"])
    assert stored["metadata"]["identity_scope"] == "connector"
    assert stored["metadata"]["client_observation"]["openai_like"] is True
