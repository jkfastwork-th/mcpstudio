from pathlib import Path

import pytest

from mcp_studio.db import Database
from mcp_studio.graft import GraftManager
from mcp_studio.settings import Settings, StudioConfig


REAL_CLI = Path("/data/graft-serena-lab/repos/target/dist/cli.js")
REAL_WORKSPACE = Path("/data/graft-serena-lab/benchmark/g7/workspace")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not REAL_CLI.is_file() or not REAL_WORKSPACE.is_dir(),
    reason="certified Graft lab fixture is unavailable",
)
async def test_hirda_graft_manager_calls_real_native_multi_repo_graft(tmp_path: Path):
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            graft_enabled=True,
            graft_cli_path=str(REAL_CLI),
            graft_node_executable="node",
            graft_request_timeout_seconds=10.0,
            graft_default_rollout_percent=100.0,
        ),
        servers=[],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    await db.init()
    await db.upsert_managed_workspace(
        key="g7-real",
        name="G7 Real",
        project_path=str(REAL_WORKSPACE),
        metadata={},
    )

    manager = GraftManager(settings, db)
    await manager.configure("g7-real", enabled=True, actor="test")
    result = await manager.query(
        "g7-real",
        tool="graft_trace_calls",
        arguments={
            "symbol": "sharedHandler",
            "direction": "out",
            "in": "billing-api",
        },
        request_id="hirda-real-g7",
        actor="test",
    )

    assert result["mode"] == "graft-shadow"
    assert result["fallback_required"] is False
    assert result["authority"]["edit_authority"] == "serena-only"
    text = "\n".join(
        str(item.get("text") or "")
        for item in result["graft_result"].get("content", [])
        if isinstance(item, dict)
    )
    assert "## billing-api/" in text
    assert "chargeInvoice" in text
    assert "## billing-worker/" not in text
    assert "processInvoice" not in text

    status = await manager.status("g7-real")
    assert status["profile"]["circuit_open"] is False
    assert status["available"] is True
