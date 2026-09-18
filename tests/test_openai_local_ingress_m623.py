from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager, make_gateway_router
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


class FakeClient:
    async def aclose(self):
        return None


def settings_for(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
            worker_server_id="serena-8001",
            openai_local_ingress_enabled=enabled,
            openai_local_ingress_id="openai-serena",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://upstream.invalid/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


@pytest.mark.asyncio
async def test_loopback_openai_ingress_virtualizes_and_attributes(tmp_path: Path, monkeypatch):
    st = settings_for(tmp_path)
    db = Database(st.studio.database)
    await db.init()
    mgr = GatewaySessionManager(st, db)

    async def fake_send(**kwargs):
        response = httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": "upstream-openai-A"},
            content=b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{"name":"Serena","version":"test"}}}',
            request=httpx.Request("POST", "http://upstream.invalid/mcp"),
        )
        return FakeClient(), response

    monkeypatch.setattr(mgr, "_send", fake_send)
    app = FastAPI()
    app.include_router(make_gateway_router(st, db, mgr))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 34567))
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tunnel-client", "version": "test"}},
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8100") as client:
        response = await client.post(
            "/ingress/openai/serena-8001",
            json=payload,
            headers={"accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 200
    gateway_id = response.headers["mcp-session-id"]
    row = await db.get_gateway_session(gateway_id)
    assert row["last_tunnel_id"] == "openai-serena"
    assert row["ingress_provider"] == "openai"
    assert row["attribution_method"] == "loopback_openai_secure_tunnel"
    assert row["attribution_confidence"] == "high"
    assert row["client_type"] == "openai-secure-tunnel"
    assert row["metadata"]["identity_scope"] == "transport"


@pytest.mark.asyncio
async def test_openai_ingress_rejects_non_loopback(tmp_path: Path):
    st = settings_for(tmp_path)
    db = Database(st.studio.database)
    await db.init()
    mgr = GatewaySessionManager(st, db)
    app = FastAPI()
    app.include_router(make_gateway_router(st, db, mgr))
    transport = httpx.ASGITransport(app=app, client=("192.168.1.55", 45678))
    async with httpx.AsyncClient(transport=transport, base_url="http://studio.local") as client:
        response = await client.post(
            "/ingress/openai/serena-8001",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_openai_ingress_disabled_is_hidden(tmp_path: Path):
    st = settings_for(tmp_path, enabled=False)
    db = Database(st.studio.database)
    await db.init()
    mgr = GatewaySessionManager(st, db)
    app = FastAPI()
    app.include_router(make_gateway_router(st, db, mgr))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45678))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8100") as client:
        response = await client.post(
            "/ingress/openai/serena-8001",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert response.status_code == 404
