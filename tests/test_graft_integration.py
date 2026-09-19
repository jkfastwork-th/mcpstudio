import json
import sys
from pathlib import Path

import pytest

from mcp_studio.db import Database
from mcp_studio.graft import GraftManager
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.settings import Settings, StudioConfig


def make_settings(tmp_path: Path, cli: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            managed_session_enabled=True,
            managed_session_workspace_roots=[str(tmp_path)],
            graft_enabled=True,
            graft_cli_path=str(cli),
            graft_node_executable=sys.executable,
            graft_request_timeout_seconds=2.0,
            graft_default_rollout_percent=100.0,
        ),
        servers=[],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


def fake_graft_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_graft.py"
    script.write_text(
        """
import json
import sys

for raw in sys.stdin:
    msg = json.loads(raw)
    if msg.get("method") == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-graft", "version": "1"},
            },
        }), flush=True)
    elif msg.get("method") == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") or {}
        rejected = bool(args.get("reject"))
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "tool": params.get("name"),
                        "arguments": args,
                    }),
                }],
                "isError": rejected,
            },
        }), flush=True)
""".lstrip()
    )
    return script


@pytest.mark.asyncio
async def test_graft_profile_is_durable_read_only_and_queryable(tmp_path: Path):
    cli = fake_graft_cli(tmp_path)
    settings = make_settings(tmp_path, cli)
    db = Database(settings.studio.database)
    await db.init()

    project = tmp_path / "project"
    project.mkdir()
    await db.upsert_managed_workspace(
        key="alpha", name="Alpha", project_path=str(project), metadata={"source": "test"}
    )

    manager = GraftManager(settings, db)
    configured = await manager.configure(
        "alpha", enabled=True, rollout_percent=100, actor="test"
    )
    assert configured["graft"]["enabled"] is True
    assert configured["graft"]["mode"] == "read_only"
    assert configured["graft"]["write_authority"] is False
    assert configured["graft"]["destructive_authority"] is False
    assert configured["graft"]["cognitive_memory_write"] is False
    assert configured["graft_permission_profile"] == {
        "read": True,
        "write": False,
        "execute": True,
        "destructive": False,
        "scope": "workspace",
        "fail_closed_unknown": True,
    }

    persisted = await db.get_managed_workspace("alpha")
    assert persisted["metadata"]["graft"]["enabled"] is True

    result = await manager.query(
        "alpha", question="where is the router?", request_id="request-1", actor="test"
    )
    assert result["mode"] == "graft-shadow"
    assert result["fallback_required"] is False
    assert result["authority"]["edit_authority"] == "serena-only"
    assert result["authority"]["serena_verification_required"] is True


@pytest.mark.asyncio
async def test_graft_circuit_is_latched_until_explicit_rearm(tmp_path: Path):
    cli = fake_graft_cli(tmp_path)
    settings = make_settings(tmp_path, cli)
    db = Database(settings.studio.database)
    await db.init()

    project = tmp_path / "project"
    project.mkdir()
    await db.upsert_managed_workspace(
        key="alpha", name="Alpha", project_path=str(project), metadata={}
    )
    manager = GraftManager(settings, db)
    await manager.configure("alpha", enabled=True, actor="test")

    rejected = await manager.query(
        "alpha",
        tool="graft_find_all",
        arguments={"pattern": "x", "reject": True},
        request_id="reject-1",
        actor="test",
    )
    assert rejected["mode"] == "serena-only"
    assert rejected["fallback_required"] is True
    assert rejected["reason"] == "rollback-on-graft-rejection"

    status = await manager.status("alpha")
    assert status["profile"]["circuit_open"] is True
    assert status["available"] is False

    bypass = await manager.query(
        "alpha", question="must bypass graft", request_id="request-2", actor="test"
    )
    assert bypass["mode"] == "serena-only"
    assert bypass["reason"] == "circuit-open"

    rearmed = await manager.rearm("alpha", actor="test")
    assert rearmed["profile"]["circuit_open"] is False

    after = await manager.query(
        "alpha", question="graft returns", request_id="request-3", actor="test"
    )
    assert after["mode"] == "graft-shadow"


@pytest.mark.asyncio
async def test_graft_manual_rollback_and_rollout_zero_bypass_context_plane(tmp_path: Path):
    cli = fake_graft_cli(tmp_path)
    settings = make_settings(tmp_path, cli)
    db = Database(settings.studio.database)
    await db.init()

    project = tmp_path / "project"
    project.mkdir()
    await db.upsert_managed_workspace(
        key="alpha", name="Alpha", project_path=str(project), metadata={}
    )
    manager = GraftManager(settings, db)
    await manager.configure("alpha", enabled=True, rollout_percent=0, actor="test")

    outside = await manager.query(
        "alpha", question="rollout zero", request_id="request-4", actor="test"
    )
    assert outside["mode"] == "serena-only"
    assert outside["reason"] == "outside-canary-bucket"

    await manager.configure("alpha", rollout_percent=100, actor="test")
    rolled = await manager.rollback("alpha", reason="operator-test", actor="test")
    assert rolled["profile"]["circuit_open"] is True
    assert rolled["profile"]["circuit_reason"] == "operator-test"


def test_gateway_advertises_first_class_graft_management_tools(tmp_path: Path):
    cli = fake_graft_cli(tmp_path)
    settings = make_settings(tmp_path, cli)
    db = Database(settings.studio.database)

    class Pool:
        enabled = True

    graft = GraftManager(settings, db)
    gateway = GatewaySessionManager(
        settings, db, managed_sessions=Pool(), graft=graft
    )
    names = {tool["name"] for tool in gateway._management_tools()}
    assert {
        "mcpstudio_graft_status",
        "mcpstudio_graft_configure",
        "mcpstudio_graft_query",
        "mcpstudio_graft_rollback",
        "mcpstudio_graft_rearm",
    } <= names


@pytest.mark.asyncio
async def test_gateway_enable_graft_preserves_serena_write_authority(tmp_path: Path):
    cli = fake_graft_cli(tmp_path)
    settings = make_settings(tmp_path, cli)
    settings.studio.managed_session_tool_permissions_enabled = True
    db = Database(settings.studio.database)
    await db.init()

    project = tmp_path / "project"
    project.mkdir()
    await db.upsert_managed_workspace(
        key="alpha", name="Alpha", project_path=str(project), metadata={}
    )
    managed = await db.create_managed_session(
        name="Alpha",
        workspace_key="alpha",
        project_path=str(project),
        server_id="serena-8001",
        port=43110,
    )

    from mcp_studio.managed_sessions import ManagedSessionManager

    pool = ManagedSessionManager(settings, db)
    graft = GraftManager(settings, db)
    gateway = GatewaySessionManager(
        settings, db, managed_sessions=pool, graft=graft
    )

    before = await pool.permissions(managed["id"])
    assert before["effective"]["write"] is True

    result = await gateway._handle_management_tool(
        "unused-gateway",
        "mcpstudio_graft_configure",
        {"workspace": "alpha", "enabled": True, "rollout_percent": 100},
    )
    assert result["graft"]["enabled"] is True
    assert result["graft_permission_profile"]["write"] is False

    after = await pool.permissions(managed["id"])
    assert after["override"] == {}
    assert after["effective"]["write"] is True
    assert after["effective"]["destructive"] is False
