from pathlib import Path

import pytest

from mcp_studio.connectivity import ConnectivityManager
from mcp_studio.db import Database
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


@pytest.mark.asyncio
async def test_external_cloudflare_start_is_rejected(tmp_path: Path):
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            request_timeout_seconds=1,
            connectivity_interval_seconds=60,
            connectivity_auto_reconnect=False,
            worker_server_id="serena-8001",
        ),
        servers=[ServerConfig(id="serena-8001", name="Serena", url="http://127.0.0.1:65534/mcp")],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    await db.init()
    manager = ConnectivityManager(settings, db)
    await db.upsert_tunnel({
        "id": "cf-existing",
        "provider": "cloudflare",
        "name": "Existing",
        "endpoint": "https://example.invalid/mcp/serena-8001",
        "origin": "http://127.0.0.1:8100",
        "health_url": None,
        "enabled": True,
        "managed": False,
        "autostart": False,
        "auto_reconnect": False,
        "desired_state": "running",
        "tunnel_name": None,
        "config_file": None,
        "executable": "cloudflared",
        "metadata": {"path_scoped": True, "server_id": "serena-8001", "management_mode": "external_remote"},
    })
    with pytest.raises(ValueError, match="inventory-only"):
        await manager.start_tunnel("cf-existing")
