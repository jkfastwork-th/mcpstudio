from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_auth_mode="bearer",
            gateway_token_env="MCP_STUDIO_TEST_TOKEN",
            gateway_allowed_servers=["serena-8001"],
            gateway_session_enabled=True,
            worker_server_id="serena-8001",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://upstream.invalid/mcp")],
        tunnels=[], config_path=tmp_path / "config.yaml",
    )


def request(host="serena.example.com", cf=True) -> Request:
    headers=[(b"host", host.encode()), (b"authorization", b"Bearer test-secret")]
    if cf:
        headers.append((b"cf-ray", b"not-persisted"))
    return Request({"type":"http","http_version":"1.1","method":"POST","scheme":"https","path":"/mcp/serena-8001","raw_path":b"/mcp/serena-8001","query_string":b"","headers":headers,"client":("127.0.0.1",1),"server":("testserver",443)})


@pytest.mark.asyncio
async def test_exact_endpoint_attribution_and_summary(tmp_path: Path):
    os.environ["MCP_STUDIO_TEST_TOKEN"]="test-secret"
    st=settings_for(tmp_path); db=Database(st.studio.database); await db.init()
    await db.upsert_tunnel({"id":"cf-a","provider":"cloudflare","name":"CF A","endpoint":"https://serena.example.com/mcp/serena-8001","origin":"http://127.0.0.1:8100","enabled":True,"managed":False,"metadata":{"server_id":"serena-8001","path_scoped":True}})
    mgr=GatewaySessionManager(st,db)
    ingress=await mgr.resolve_ingress(request(),"serena-8001")
    assert ingress["tunnel_id"]=="cf-a"
    assert ingress["method"]=="endpoint_host_path"
    assert ingress["confidence"]=="high"
    assert "not-persisted" not in repr(ingress)
    logical=await db.create_session({"client_id":"x","client_type":"chatgpt","server_id":"serena-8001","metadata":{}})
    gw=await db.create_gateway_session(studio_session_id=logical["id"],client_id="x",client_type="chatgpt",server_id="serena-8001",upstream_session_id="u",protocol_version="1",init_payload={},ingress=ingress)
    assert gw["initial_tunnel_id"]=="cf-a" and gw["last_tunnel_id"]=="cf-a"
    filtered=await db.list_gateway_sessions(tunnel_id="cf-a")
    assert [x["id"] for x in filtered]==[gw["id"]]
    summary=await db.tunnel_session_summary()
    assert summary["by_tunnel"]["cf-a"]["total"]==1


@pytest.mark.asyncio
async def test_ambiguous_endpoint_is_not_guessed(tmp_path: Path):
    st=settings_for(tmp_path); db=Database(st.studio.database); await db.init()
    for tid in ("a","b"):
        await db.upsert_tunnel({"id":tid,"provider":"external","name":tid,"endpoint":"https://serena.example.com/mcp/serena-8001","enabled":True,"managed":False,"metadata":{"server_id":"serena-8001"}})
    ingress=await GatewaySessionManager(st,db).resolve_ingress(request(cf=False),"serena-8001")
    assert ingress["tunnel_id"] is None
    assert ingress["method"]=="ambiguous_endpoint"
    assert ingress["confidence"]=="low"


@pytest.mark.asyncio
async def test_legacy_session_backfills_and_tracks_switch(tmp_path: Path):
    st=settings_for(tmp_path); db=Database(st.studio.database); await db.init()
    logical=await db.create_session({"client_id":"x","client_type":"chatgpt","server_id":"serena-8001","metadata":{}})
    gw=await db.create_gateway_session(studio_session_id=logical["id"],client_id="x",client_type="chatgpt",server_id="serena-8001",upstream_session_id="u",protocol_version="1",init_payload={})
    one={"tunnel_id":"a","host":"a.example","path":"/mcp/serena-8001","provider":"cloudflare","method":"endpoint_host_path","confidence":"high"}
    two={**one,"tunnel_id":"b","host":"b.example"}
    x=await db.update_gateway_session_ingress(gw["id"],one)
    assert x["initial_tunnel_id"]=="a" and x["last_tunnel_id"]=="a" and x["tunnel_switch_count"]==0
    x=await db.update_gateway_session_ingress(gw["id"],two)
    assert x["initial_tunnel_id"]=="a" and x["last_tunnel_id"]=="b" and x["tunnel_switch_count"]==1


@pytest.mark.asyncio
async def test_v4_database_migrates_to_v5_without_preindex_failure(tmp_path: Path):
    import sqlite3
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE gateway_sessions (
            id TEXT PRIMARY KEY,
            studio_session_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            client_type TEXT NOT NULL,
            server_id TEXT NOT NULL,
            upstream_session_id TEXT,
            status TEXT NOT NULL DEFAULT 'connected',
            generation INTEGER NOT NULL DEFAULT 1,
            reconnect_count INTEGER NOT NULL DEFAULT 0,
            protocol_version TEXT,
            init_payload_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_reconnected_at TEXT,
            closed_at TEXT,
            error TEXT
        );
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES(4, 'm6.2.1-lease-lifecycle-integrity', '2026-01-01T00:00:00+00:00');
        """)
    db = Database(str(path))
    await db.init()
    status = await db.schema_status()
    assert status["current_version"] == 7
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(gateway_sessions)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(gateway_sessions)")}
    assert {"initial_tunnel_id", "last_tunnel_id", "attribution_confidence", "tunnel_switch_count"} <= columns
    assert "idx_gateway_sessions_tunnel" in indexes
