"""Tests for Social Hub Task HIRDA plugin."""
from __future__ import annotations

import base64
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "social-hub-task"


def load_plugin():
    spec = importlib.util.spec_from_file_location("hirda_social_hub_task_plugin", PLUGIN_ROOT / "adapter.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SocialHubHandler(BaseHTTPRequestHandler):
    token = b""
    received = None
    asset_received = None
    status_response = None

    def _check_auth(self):
        supplied = self.headers.get("X-Social-Hub-Ingress-Token", "")
        if supplied != self.token.decode("utf-8"):
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_POST(self):
        if not self._check_auth():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path == "/api/social/ingress/tasks":
            payload = json.loads(raw)
            type(self).received = payload
            body = json.dumps({
                "task_id": "task-123",
                "status": "pending",
            }).encode()
            self.send_response(201)
        elif self.path.endswith("/assets"):
            type(self).asset_received = {
                "body": raw,
                "headers": dict(self.headers),
            }
            body = json.dumps({
                "task_id": self.path.split("/")[-3],
                "asset_id": "asset-456",
            }).encode()
            self.send_response(200)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_auth():
            return
        if self.path.startswith("/api/social/ingress/tasks/"):
            body = json.dumps({
                "task_id": self.path.split("/")[-1],
                "status": "completed",
                "result": {"published": True},
            }).encode()
            self.send_response(200)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


@pytest.fixture
def hub_server():
    SocialHubHandler.token = b"test-social-hub-token-12345"
    SocialHubHandler.received = None
    SocialHubHandler.asset_received = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), SocialHubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "ingress-token"
    path.write_text("test-social-hub-token-12345")
    return path


@pytest.mark.asyncio
async def test_create_task_forced_identities(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    result = await integration.call_tool(
        "social_task_create",
        {
            "instructions": "Post about our new product launch",
            "title": "Product Launch",
            "channels": ["facebook", "instagram"],
            "run_mode": "now",
        },
        context={"turn_id": "turn-1"},
    )
    assert result["status"] == "created"
    assert result["source_app"] == "chatgpt_web"
    assert result["requested_by"] == "jack"
    assert result["assigned_agent"] == "fern"
    assert SocialHubHandler.received["source_app"] == "chatgpt_web"
    assert SocialHubHandler.received["requested_by"] == "jack"
    assert SocialHubHandler.received["assigned_agent"] == "fern"


@pytest.mark.asyncio
async def test_create_task_deterministic_correlation_id(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    context = {"turn_id": "turn-abc", "gateway_session_id": "gws-xyz"}
    result1 = await integration.call_tool(
        "social_task_create",
        {"instructions": "Same instructions", "channels": ["facebook"]},
        context=context,
    )
    result2 = await integration.call_tool(
        "social_task_create",
        {"instructions": "Same instructions", "channels": ["facebook"]},
        context=context,
    )
    assert result1["correlation_id"] == result2["correlation_id"]
    assert result1["correlation_id"].startswith("sht-")


@pytest.mark.asyncio
async def test_create_task_distinct_content_distinct_correlation(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    context = {"turn_id": "turn-abc"}
    result1 = await integration.call_tool(
        "social_task_create",
        {"instructions": "First task", "channels": ["facebook"]},
        context=context,
    )
    result2 = await integration.call_tool(
        "social_task_create",
        {"instructions": "Second task", "channels": ["facebook"]},
        context=context,
    )
    assert result1["correlation_id"] != result2["correlation_id"]


@pytest.mark.asyncio
async def test_create_task_scheduled_requires_scheduled_at(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    with pytest.raises(module.SocialHubTaskAdapterError, match="scheduled_at_required"):
        await integration.call_tool(
            "social_task_create",
            {"instructions": "Scheduled post", "run_mode": "scheduled"},
            context={},
        )


@pytest.mark.asyncio
async def test_create_task_scheduled_with_scheduled_at(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    result = await integration.call_tool(
        "social_task_create",
        {
            "instructions": "Scheduled post",
            "run_mode": "scheduled",
            "scheduled_at": "2026-10-15T10:00:00Z",
        },
        context={},
    )
    assert result["status"] == "created"
    assert SocialHubHandler.received["scheduled_at"] == "2026-10-15T10:00:00Z"


@pytest.mark.asyncio
async def test_attach_asset_sends_raw_bytes_and_headers(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    raw_bytes = b"\x89PNG\r\n\x1a\n" + b"fake-image-data" * 100
    asset_b64 = base64.b64encode(raw_bytes).decode("ascii")
    result = await integration.call_tool(
        "social_task_attach_asset",
        {
            "task_id": "task-123",
            "asset_base64": asset_b64,
            "asset_name": "hero.png",
            "asset_channel": "facebook",
            "content_type": "image/png",
        },
        context={},
    )
    assert result["status"] == "attached"
    assert result["size_bytes"] == len(raw_bytes)
    assert SocialHubHandler.asset_received is not None
    assert SocialHubHandler.asset_received["body"] == raw_bytes
    headers = SocialHubHandler.asset_received["headers"]
    assert headers.get("X-Asset-Name") == "hero.png"
    assert headers.get("X-Asset-Channel") == "facebook"
    assert headers.get("Content-Type") == "image/png"


@pytest.mark.asyncio
async def test_attach_asset_rejects_non_base64(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    with pytest.raises(module.SocialHubTaskAdapterError, match="not_base64"):
        await integration.call_tool(
            "social_task_attach_asset",
            {
                "task_id": "task-123",
                "asset_base64": "not-valid-base64!!!",
                "asset_name": "hero.png",
                "asset_channel": "facebook",
                "content_type": "image/png",
            },
            context={},
        )


@pytest.mark.asyncio
async def test_status_authenticated_get(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    result = await integration.call_tool(
        "social_task_status",
        {"task_id": "task-123"},
        context={},
    )
    assert result["status"] == "ok"
    assert result["task"]["status"] == "completed"


@pytest.mark.asyncio
async def test_status_rejects_missing_token(hub_server, tmp_path, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(tmp_path / "nonexistent"))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    with pytest.raises(module.SocialHubTaskAdapterError, match="token_unavailable"):
        await integration.call_tool(
            "social_task_status",
            {"task_id": "task-123"},
            context={},
        )


@pytest.mark.asyncio
async def test_reject_non_loopback_url(token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", "http://192.168.1.100:8790")
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    with pytest.raises(module.SocialHubTaskAdapterError, match="not_loopback"):
        await integration.call_tool(
            "social_task_create",
            {"instructions": "test"},
            context={},
        )


@pytest.mark.asyncio
async def test_reject_https_url(token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", "https://127.0.0.1:8790")
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    with pytest.raises(module.SocialHubTaskAdapterError, match="not_loopback"):
        await integration.call_tool(
            "social_task_create",
            {"instructions": "test"},
            context={},
        )


def test_plugin_folder_preflight():
    """inspect_plugin_folder discovers the plugin with correct permissions."""
    from mcp_studio.plugin_folder import inspect_plugin_folder
    result = inspect_plugin_folder(ROOT / "plugins" / "social-hub-task")
    assert result.plugin_id == "social-hub-task"
    assert result.preflight_ok
    assert "social_task_create" in result.permissions
    assert "social_task_attach_asset" in result.permissions
    assert "social_task_status" in result.permissions
    assert result.permissions["social_task_create"] == "write"
    assert result.permissions["social_task_attach_asset"] == "write"
    assert result.permissions["social_task_status"] == "read"


def test_plugin_metadata():
    """Plugin manifest has correct metadata."""
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    meta = integration.manifest.metadata
    assert meta["executor_identity"] == "fern"
    assert meta["owner_social_accounts"] is True
    assert meta["runtime_authority"] == "hirda"
    assert meta["governance_authority"] is False


def test_tool_catalog_exact():
    """Exactly three tools with correct names."""
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    import asyncio
    tools = asyncio.get_event_loop().run_until_complete(integration.list_tools())
    names = [t["name"] for t in tools]
    assert names == ["social_task_create", "social_task_attach_asset", "social_task_status"]


def test_create_schema_forced_fields_not_caller_controlled():
    """source_app, requested_by, assigned_agent are NOT in the input schema."""
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    import asyncio
    tools = asyncio.get_event_loop().run_until_complete(integration.list_tools())
    create_tool = next(t for t in tools if t["name"] == "social_task_create")
    props = create_tool["inputSchema"]["properties"]
    assert "source_app" not in props
    assert "requested_by" not in props
    assert "assigned_agent" not in props


def test_certify_no_network_side_effect():
    """certify() makes no network calls."""
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(integration.certify())
    assert result["network_call"] is False
    assert result["ok"] is True
