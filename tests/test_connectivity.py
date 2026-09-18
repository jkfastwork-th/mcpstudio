from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcp_studio.connectivity import ConnectivityManager
from mcp_studio.db import Database
from mcp_studio.settings import ServerConfig, Settings, StudioConfig, TunnelConfig


def make_settings(tmp_path: Path, *, port: int | None = None) -> Settings:
    endpoint = f"http://127.0.0.1:{port}/mcp" if port else "http://127.0.0.1:65534/mcp"
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            request_timeout_seconds=1,
            connectivity_interval_seconds=60,
            connectivity_auto_reconnect=False,
            session_stale_seconds=5,
            worker_server_id="serena-8001",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url=endpoint)],
        tunnels=[
            TunnelConfig(
                id="local-serena", provider="local", name="Local Serena", endpoint=endpoint
            )
        ],
        config_path=tmp_path / "config.yaml",
    )


@pytest.mark.asyncio
async def test_local_tunnel_probe_and_execution_isolation(tmp_path: Path):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = make_settings(tmp_path, port=port)
    db = Database(settings.studio.database)
    await db.init()
    await db.ensure_workers(4, "serena-8001")
    before = await db.list_workers()

    manager = ConnectivityManager(settings, db)
    await db.sync_tunnels(settings.tunnels)
    snap = await manager.poll_once(allow_reconnect=False)
    tunnel = (await db.list_tunnels())[0]
    after = await db.list_workers()

    assert snap["status"] == "healthy"
    assert tunnel["status"] == "healthy"
    assert [(x["id"], x["state"], x["workspace"]) for x in before] == [
        (x["id"], x["state"], x["workspace"]) for x in after
    ]
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_session_reclaim_preserves_studio_session_id(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    await db.init()
    payload = {
        "client_id": "chatgpt-browser-1",
        "client_type": "chatgpt",
        "server_id": "serena-8001",
        "workspace": "/data/earth-616",
        "pane": "wF:p6",
        "metadata": {},
    }
    first, reclaimed = await db.reclaim_session(payload)
    assert reclaimed is False
    await db.disconnect_session(first["id"])
    second, reclaimed = await db.reclaim_session({**payload, "pane": "wF:p7"})
    assert reclaimed is True
    assert second["id"] == first["id"]
    assert second["status"] == "connected"
    assert second["pane"] == "wF:p7"
    assert second["metadata"]["reclaim_count"] == 1


@pytest.mark.asyncio
async def test_stale_session_detection(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    await db.init()
    item = await db.create_session(
        {
            "client_id": "old-client",
            "client_type": "chatgpt",
            "server_id": "serena-8001",
            "metadata": {},
        }
    )
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (old, item["id"]))
    count = await db.mark_stale_sessions(5)
    updated = await db.get_session(item["id"])
    assert count == 1
    assert updated["status"] == "stale"


@pytest.mark.asyncio
async def test_tunnel_registry_persists_runtime_state(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    await db.init()
    await db.sync_tunnels(settings.tunnels)
    item = await db.update_tunnel_runtime(
        "local-serena", status="healthy", pid=123, command=["example"], last_checked_at="now"
    )
    assert item["status"] == "healthy"
    assert item["pid"] == 123
    assert item["command"] == ["example"]
    # Config sync must not reset runtime status/desired state.
    await db.sync_tunnels(settings.tunnels)
    item = await db.get_tunnel("local-serena")
    assert item["status"] == "healthy"
    assert item["pid"] == 123


def test_cloudflare_named_command_is_shell_free(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    manager = ConnectivityManager(settings, db)
    tunnel = {
        "executable": "cloudflared",
        "tunnel_name": "serena-primary",
        "config_file": "/home/alfred/.cloudflared/config.yml",
        "origin": "http://127.0.0.1:8100",
    }
    assert manager.build_cloudflare_command(tunnel) == [
        "cloudflared",
        "tunnel",
        "--config",
        "/home/alfred/.cloudflared/config.yml",
        "run",
        "serena-primary",
    ]


def test_cloudflare_quick_tunnel_is_refused(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.studio.database)
    manager = ConnectivityManager(settings, db)
    tunnel = {
        "executable": "cloudflared",
        "tunnel_name": None,
        "config_file": None,
        "origin": "http://127.0.0.1:8100",
    }
    with pytest.raises(ValueError, match="refuses Cloudflare quick tunnels"):
        manager.build_cloudflare_command(tunnel)
