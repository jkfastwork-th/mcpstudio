from __future__ import annotations

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


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_TEST_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
            gateway_session_auto_reconnect=True,
            gateway_session_replay_on_404=True,
            worker_server_id="serena-8001",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://upstream.invalid/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


def make_request(*, body: bytes = b"", session_id: str | None = None, client_id: str = "chatgpt-cert") -> Request:
    headers = [
        (b"authorization", b"Bearer test-secret"),
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
        (b"x-mcp-studio-client-id", client_id.encode()),
        (b"x-mcp-studio-client-type", b"chatgpt"),
    ]
    if session_id:
        headers.append((b"mcp-session-id", session_id.encode()))

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp/serena-8001",
            "raw_path": b"/mcp/serena-8001",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive,
    )


@pytest.mark.asyncio
async def test_initialize_virtualizes_upstream_and_reclaims_logical_session(tmp_path: Path, monkeypatch):
    os.environ["MCP_STUDIO_TEST_TOKEN"] = "test-secret"
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    await db.init()
    manager = GatewaySessionManager(settings, db)
    upstream_ids = iter(["upstream-A", "upstream-B"])

    async def fake_send(**kwargs):
        up = next(upstream_ids)
        response = httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": up},
            content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
            request=httpx.Request("POST", "http://upstream.invalid/mcp"),
        )
        return FakeClient(), response

    monkeypatch.setattr(manager, "_send", fake_send)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    body = __import__("json").dumps(payload).encode()
    request = make_request(body=body)
    _token, client_id, client_type, identity_scope, identity_source = manager.authenticate(request)
    first = await manager.initialize(
        request=request,
        server=settings.servers[0],
        body=body,
        jsonrpc=payload,
        client_id=client_id,
        client_type=client_type,
        identity_scope=identity_scope,
        identity_source=identity_source,
    )
    first_gw = first.headers["mcp-session-id"]
    first_studio = first.headers["x-mcp-studio-session-id"]
    assert first_gw.startswith("gws-")
    assert first.headers["x-mcp-studio-reclaimed"] == "false"
    stored = await db.get_gateway_session(first_gw)
    assert stored["upstream_session_id"] == "upstream-A"

    await db.close_gateway_session(first_gw)
    await db.disconnect_session(first_studio)
    request2 = make_request(body=body)
    _token, client_id, client_type, identity_scope, identity_source = manager.authenticate(request2)
    second = await manager.initialize(
        request=request2,
        server=settings.servers[0],
        body=body,
        jsonrpc=payload,
        client_id=client_id,
        client_type=client_type,
        identity_scope=identity_scope,
        identity_source=identity_source,
    )
    assert second.headers["mcp-session-id"] != first_gw
    assert second.headers["x-mcp-studio-session-id"] == first_studio
    assert second.headers["x-mcp-studio-reclaimed"] == "true"


@pytest.mark.asyncio
async def test_unknown_upstream_session_reconnect_keeps_gateway_id(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    await db.init()
    studio, _ = await db.reclaim_session(
        {"client_id": "chatgpt-cert", "client_type": "chatgpt", "server_id": "serena-8001", "metadata": {}}
    )
    gw = await db.create_gateway_session(
        studio_session_id=studio["id"],
        client_id="chatgpt-cert",
        client_type="chatgpt",
        server_id="serena-8001",
        upstream_session_id="dead-upstream",
        protocol_version="2025-06-18",
        init_payload={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    manager = GatewaySessionManager(settings, db)
    calls = 0

    async def fake_send(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            response = httpx.Response(404, content=b"unknown session", request=httpx.Request("POST", "http://upstream.invalid/mcp"))
        else:
            response = httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}',
                request=httpx.Request("POST", "http://upstream.invalid/mcp"),
            )
        return FakeClient(), response

    async def fake_reinitialize(session, server_url):
        return await db.reconnect_gateway_session(
            session["id"], upstream_session_id="fresh-upstream", error=None
        )

    monkeypatch.setattr(manager, "_send", fake_send)
    monkeypatch.setattr(manager, "_reinitialize_upstream", fake_reinitialize)
    body = b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    response = await manager.proxy_existing(
        request=make_request(body=body, session_id=gw["id"]),
        server=settings.servers[0],
        body=body,
        gateway_session_id=gw["id"],
    )
    assert response.status_code == 200
    assert response.headers["mcp-session-id"] == gw["id"]
    assert response.headers["x-mcp-studio-generation"] == "2"
    updated = await db.get_gateway_session(gw["id"])
    assert updated["upstream_session_id"] == "fresh-upstream"
    assert updated["reconnect_count"] == 1
    assert updated["generation"] == 2
    assert calls == 2


def test_bearer_fingerprint_is_stable_without_storing_secret(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_TOKEN"] = "test-secret"
    manager = GatewaySessionManager(make_settings(tmp_path), Database(str(tmp_path / "db.sqlite3")))
    req1 = make_request(client_id="")
    # Remove the explicit client header so bearer fingerprint is used.
    req1.scope["headers"] = [(k, v) for k, v in req1.scope["headers"] if k != b"x-mcp-studio-client-id"]
    req2 = make_request(client_id="")
    req2.scope["headers"] = [(k, v) for k, v in req2.scope["headers"] if k != b"x-mcp-studio-client-id"]
    _, cid1, _, scope1, source1 = manager.authenticate(req1)
    _, cid2, _, scope2, source2 = manager.authenticate(req2)
    assert cid1 == cid2
    assert "test-secret" not in cid1
    assert cid1.startswith("bearer-")
    assert scope1 == scope2 == "connector"
    assert source1 == source2 == "bearer-fingerprint"


def test_context_usage_header_is_accepted_without_estimation(tmp_path: Path):
    manager = GatewaySessionManager(make_settings(tmp_path), Database(str(tmp_path / "db.sqlite3")))

    req = make_request()
    req.scope["headers"].append((b"x-openai-context-usage-percent", b"84.5"))
    assert manager.context_usage_from_request(req) == (84.5, "header:x-openai-context-usage-percent")

    missing = make_request()
    assert manager.context_usage_from_request(missing) is None

    invalid = make_request()
    invalid.scope["headers"].append((b"x-openai-context-usage-percent", b"not-a-number"))
    assert manager.context_usage_from_request(invalid) is None

    out_of_range = make_request()
    out_of_range.scope["headers"].append((b"x-openai-context-usage-percent", b"101"))
    assert manager.context_usage_from_request(out_of_range) is None


def test_tools_list_includes_hirda_backend_before_upstream(tmp_path: Path):
    class FakeIntegrations:
        @staticmethod
        def backend_tools():
            return [
                {
                    "name": "hirda__desktop_commander__read_file",
                    "description": "HIRDA backend read",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

    manager = GatewaySessionManager(
        make_settings(tmp_path),
        Database(str(tmp_path / "db.sqlite3")),
        integrations=FakeIntegrations(),
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "serena_tool",
                    "description": "upstream",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        },
    }
    result = manager._augment_tools_payload(payload)
    names = [tool["name"] for tool in result["result"]["tools"]]
    assert names == ["hirda__desktop_commander__read_file", "serena_tool"]


def test_initialize_advertises_dynamic_tool_list_and_cache_hash(tmp_path: Path):
    manager = GatewaySessionManager(
        make_settings(tmp_path),
        Database(str(tmp_path / "db.sqlite3")),
    )
    content = (
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-11-25",'
        b'"capabilities":{"tools":{"listChanged":false}}}}'
    )

    augmented = manager._augment_initialize_content(content, "application/json")
    payload = __import__("json").loads(augmented)
    assert payload["result"]["capabilities"]["tools"]["listChanged"] is True

    cache = manager._initialize_response_cache(
        augmented,
        status_code=200,
        content_type="application/json",
    )
    assert cache["tool_catalog_hash"] == manager._tool_catalog_hash()


def test_stale_initialize_cache_is_invalidated_when_local_tool_catalog_changes(tmp_path: Path):
    class MutableIntegrations:
        def __init__(self):
            self.tools = [
                {
                    "name": "hirda__desktop_commander__read_file",
                    "description": "read",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        def backend_tools(self):
            return list(self.tools)

    integrations = MutableIntegrations()
    manager = GatewaySessionManager(
        make_settings(tmp_path),
        Database(str(tmp_path / "db.sqlite3")),
        integrations=integrations,
    )
    content = b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}'
    cache = manager._initialize_response_cache(
        content,
        status_code=200,
        content_type="application/json",
    )
    session = {"metadata": {"initialize_response": cache}}

    assert manager._cached_initialize_response(session, request_id=2) is not None

    integrations.tools.append(
        {
            "name": "hirda__desktop_commander__execute",
            "description": "execute",
            "inputSchema": {"type": "object", "properties": {}},
        }
    )
    assert manager._cached_initialize_response(session, request_id=3) is None


def test_tools_list_prepends_machine_management_and_backend_tools(tmp_path: Path):
    class FakeManagedPool:
        enabled = True

    class FakeIntegrations:
        @staticmethod
        def backend_tools():
            return [
                {
                    "name": "hirda__desktop_commander__read_file",
                    "description": "HIRDA backend read",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

    manager = GatewaySessionManager(
        make_settings(tmp_path),
        Database(str(tmp_path / "db.sqlite3")),
        managed_sessions=FakeManagedPool(),
        integrations=FakeIntegrations(),
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "serena_tool",
                    "description": "upstream",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        },
    }

    result = manager._augment_tools_payload(payload)
    names = [tool["name"] for tool in result["result"]["tools"]]
    assert "mcpstudio_list_machines" in names
    assert "mcpstudio_discover_machines" in names
    assert "mcpstudio_approve_machine" in names
    assert "mcpstudio_bind_machine" in names
    assert "hirda__desktop_commander__read_file" in names
    assert names.index("mcpstudio_list_machines") < names.index("serena_tool")
    assert names.index("hirda__desktop_commander__read_file") < names.index("serena_tool")
