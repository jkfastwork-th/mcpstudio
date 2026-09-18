from __future__ import annotations

from pathlib import Path

import pytest

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_allowed_servers=["serena-8001"],
            managed_session_enabled=True,
            managed_session_workspace_roots=[str(tmp_path)],
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://127.0.0.1:8001/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


@pytest.mark.asyncio
async def test_schema_v7_adds_managed_session_activity_columns(tmp_path: Path):
    db = Database(str(tmp_path / "db.sqlite3"))
    await db.init()
    status = await db.schema_status()
    assert status["current_version"] == 7
    assert status["expected_version"] == 7

    def inspect():
        with db._connect() as conn:
            return {r[1] for r in conn.execute("PRAGMA table_info(managed_sessions)")}

    columns = await db._run(inspect)
    assert {"last_used_at", "use_count"} <= columns


@pytest.mark.asyncio
async def test_managed_session_rename_and_touch(tmp_path: Path):
    db = Database(str(tmp_path / "db.sqlite3")); await db.init()
    project = tmp_path / "alpha"; project.mkdir()
    await db.upsert_managed_workspace(key="alpha", name="Alpha", project_path=str(project), metadata={})
    session = await db.create_managed_session(
        name="Old", workspace_key="alpha", project_path=str(project), server_id="serena-8001", port=8210
    )
    renamed = await db.rename_managed_session(session["id"], "Alpha Dev")
    assert renamed["name"] == "Alpha Dev"
    touched = await db.touch_managed_session(session["id"])
    assert touched["use_count"] == 1
    assert touched["last_used_at"]


@pytest.mark.asyncio
async def test_managed_session_overview_rolls_up_transport_activity(tmp_path: Path):
    db = Database(str(tmp_path / "db.sqlite3")); await db.init()
    project = tmp_path / "alpha"; project.mkdir()
    await db.upsert_managed_workspace(key="alpha", name="Alpha", project_path=str(project), metadata={})
    managed = await db.create_managed_session(
        name="Alpha", workspace_key="alpha", project_path=str(project), server_id="serena-8001", port=8210
    )
    await db.update_managed_session_runtime(managed["id"], status="ready", pid=123, endpoint="http://127.0.0.1:8210/mcp")
    studio = await db.create_session({"client_id":"c", "client_type":"chatgpt", "server_id":"serena-8001"})
    gateway = await db.create_gateway_session(
        studio_session_id=studio["id"], client_id="c", client_type="chatgpt", server_id="serena-8001",
        upstream_session_id="up", protocol_version="2025-06-18", init_payload={}
    )
    await db.update_gateway_session_ingress(gateway["id"], {
        "tunnel_id":"openai-serena", "host":"127.0.0.1",
        "path":"/ingress/openai/serena-8001", "provider":"openai",
        "method":"loopback_openai_secure_tunnel", "confidence":"high",
    })
    await db.bind_gateway_managed_session(
        gateway["id"], managed_session_id=managed["id"], upstream_url="http://127.0.0.1:8210/mcp", upstream_session_id="managed-up"
    )
    rows = await db.managed_session_overview()
    row = next(x for x in rows if x["id"] == managed["id"])
    assert row["lifecycle_state"] == "active"
    assert row["connected_transports"] == 1
    assert row["ingress_providers"] == ["openai"]


@pytest.mark.asyncio
async def test_managed_session_history_and_tools_are_exposed(tmp_path: Path):
    settings = settings_for(tmp_path)
    db = Database(settings.studio.database); await db.init()
    project = tmp_path / "alpha"; project.mkdir(exist_ok=True)
    await db.upsert_managed_workspace(key="alpha", name="Alpha", project_path=str(project), metadata={})
    managed = await db.create_managed_session(
        name="Alpha", workspace_key="alpha", project_path=str(project), server_id="serena-8001", port=8210
    )
    await db.add_audit("managed.session.rename", actor="test", target_type="managed_session", target_id=managed["id"], data={"name":"Alpha"})
    history = await db.managed_session_history(managed["id"])
    assert history["audit"][0]["action"] == "managed.session.rename"

    class Pool:
        enabled = True
    gateway = GatewaySessionManager(settings, db, managed_sessions=Pool())
    names = {x["name"] for x in gateway._management_tools()}
    assert "mcpstudio_rename_session" in names
    assert "mcpstudio_session_history" in names
