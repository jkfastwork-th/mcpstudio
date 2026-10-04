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
TASK_ID = "task_0123456789abcdef"


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
    duplicate_suppressed = False
    error_status = None
    error_payload = None
    request_count = 0

    def _check_auth(self):
        supplied = self.headers.get("X-Social-Hub-Ingress-Token", "")
        if supplied != self.token.decode("utf-8"):
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_POST(self):
        type(self).request_count += 1
        if not self._check_auth():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if type(self).error_status:
            body = json.dumps(type(self).error_payload or {}).encode()
            self.send_response(type(self).error_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/social/ingress/tasks":
            payload = json.loads(raw)
            type(self).received = payload
            body = json.dumps({
                "task": {"task_id": TASK_ID, "status": "pending"},
                "duplicate_suppressed": type(self).duplicate_suppressed,
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
        type(self).request_count += 1
        if not self._check_auth():
            return
        if type(self).error_status:
            body = json.dumps(type(self).error_payload or {}).encode()
            self.send_response(type(self).error_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/social/ingress/tasks/"):
            body = json.dumps({
                "task": {
                    "task_id": self.path.split("/")[-1],
                    "status": "completed",
                    "result": {"published": True},
                },
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
    SocialHubHandler.duplicate_suppressed = False
    SocialHubHandler.error_status = None
    SocialHubHandler.error_payload = None
    SocialHubHandler.request_count = 0
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
    assert result["status"] == "pending"
    assert result["task_id"] == TASK_ID
    assert result["duplicate_suppressed"] is False
    assert result["source_app"] == "chatgpt_web"
    assert result["requested_by"] == "jack"
    assert result["assigned_agent"] == "fern"
    assert SocialHubHandler.received["source_app"] == "chatgpt_web"
    assert SocialHubHandler.received["requested_by"] == "jack"
    assert SocialHubHandler.received["assigned_agent"] == "fern"


@pytest.mark.asyncio
async def test_create_task_from_social_publish_skill_preserves_governed_policy_and_provenance(
    hub_server, token_file, monkeypatch
):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()

    result = await integration.call_tool(
        "social_task_create",
        {
            "instructions": "Prepare a Facebook analysis post.",
            "channels": ["facebook"],
            "publish_policy": "review",
            "comment_policy": "track",
            "correlation_id": "skill-social-publish-1",
        },
        context={
            "gateway_session_id": "gws-social",
            "skill_id": "social.publish",
            "conversation_id": "conversation-1",
            "parent_request_id": "request-1",
        },
    )

    assert result["task_id"] == TASK_ID
    assert result["source_app"] == "hirda_skill"
    assert result["skill_id"] == "social.publish"
    assert result["publish_policy"] == "review"
    assert result["comment_policy"] == "track"

    received = SocialHubHandler.received
    assert received["task_type"] == "publish"
    assert received["publish_policy"] == "review"
    assert received["target_identity"] == "owner_social"
    assert received["comment_policy"] == "track"
    assert received["watch_after_publish"] is True
    assert received["auto_reply"] is False
    assert received["source_app"] == "hirda_skill"
    assert received["requested_by"] == "jack"
    assert received["assigned_agent"] == "fern"
    assert received["skill_id"] == "social.publish"
    assert received["conversation_id"] == "conversation-1"
    assert received["parent_request_id"] == "request-1"


@pytest.mark.asyncio
async def test_create_task_reports_duplicate_suppression(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    SocialHubHandler.duplicate_suppressed = True
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()

    result = await integration.call_tool(
        "social_task_create",
        {"instructions": "A duplicate request", "channels": ["facebook"]},
        context={},
    )

    assert result["task_id"] == TASK_ID
    assert result["status"] == "pending"
    assert result["duplicate_suppressed"] is True


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
            {"instructions": "Scheduled post", "channels": ["facebook"], "run_mode": "scheduled"},
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
            "channels": ["facebook"],
            "run_mode": "scheduled",
            "scheduled_at": "2026-10-15T10:00:00Z",
        },
        context={},
    )
    assert result["status"] == "pending"
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
            "task_id": TASK_ID,
            "asset_base64": asset_b64,
            "asset_name": "hero banner-01.png",
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
    assert headers.get("X-Asset-Name") == "hero banner-01.png"
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
                "task_id": TASK_ID,
                "asset_base64": "not-valid-base64!!!",
                "asset_name": "hero.png",
                "asset_channel": "facebook",
                "content_type": "image/png",
            },
            context={},
        )


@pytest.mark.asyncio
async def test_task_ids_are_validated_before_url_interpolation(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    baseline_requests = SocialHubHandler.request_count
    invalid_ids = ("task-123", "../task_0123456789abcdef", "task_0123456789abcdeg")

    for task_id in invalid_ids:
        with pytest.raises(module.SocialHubTaskAdapterError, match="task_id_invalid"):
            await integration.call_tool("social_task_status", {"task_id": task_id}, context={})
        with pytest.raises(module.SocialHubTaskAdapterError, match="task_id_invalid"):
            await integration.call_tool(
                "social_task_attach_asset",
                {
                    "task_id": task_id,
                    "asset_base64": base64.b64encode(b"x").decode(),
                    "asset_name": "x.png",
                    "asset_channel": "facebook",
                    "content_type": "image/png",
                },
                context={},
            )
    assert SocialHubHandler.request_count == baseline_requests


@pytest.mark.asyncio
async def test_backend_error_text_is_not_reflected(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    SocialHubHandler.error_status = 502
    SocialHubHandler.error_payload = {"error": "secret reflected backend traceback"}
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    calls = (
        integration.call_tool(
            "social_task_create",
            {"instructions": "Error case", "channels": ["facebook"]},
            context={},
        ),
        integration.call_tool(
            "social_task_attach_asset",
            {
                "task_id": TASK_ID,
                "asset_base64": base64.b64encode(b"x").decode(),
                "asset_name": "x.png",
                "asset_channel": "facebook",
                "content_type": "image/png",
            },
            context={},
        ),
        integration.call_tool("social_task_status", {"task_id": TASK_ID}, context={}),
    )
    for operation, call in zip(("create", "attach", "status"), calls):
        with pytest.raises(module.SocialHubTaskAdapterError) as raised:
            await call
        assert f"social_hub_{operation}_failed:502" in str(raised.value)
        assert "secret reflected backend traceback" not in str(raised.value)


@pytest.mark.asyncio
async def test_empty_token_file_fails_validation_and_certify_without_exposure(tmp_path, monkeypatch):
    empty_token = tmp_path / "empty-token"
    empty_token.write_text(" \n\t", encoding="utf-8")
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(empty_token))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()

    validation = await integration.validate()
    certification = await integration.certify()

    assert validation["ok"] is False
    assert validation["token_available"] is False
    assert certification["ok"] is False
    assert "test-social-hub-token" not in repr(validation)
    assert "test-social-hub-token" not in repr(certification)


@pytest.mark.asyncio
async def test_explicit_overlength_correlation_id_rejected(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    baseline_requests = SocialHubHandler.request_count

    with pytest.raises(module.SocialHubTaskAdapterError, match="correlation_id_invalid"):
        await integration.call_tool(
            "social_task_create",
            {"instructions": "Too long", "channels": ["facebook"], "correlation_id": "x" * 161},
            context={},
        )

    assert SocialHubHandler.request_count == baseline_requests


@pytest.mark.asyncio
async def test_asset_base64_encoded_bound_checked_before_decode(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    monkeypatch.setattr(module, "_MAX_ASSET_BYTES", 3)
    decode_called = False

    def forbidden_decode(*args, **kwargs):
        nonlocal decode_called
        decode_called = True
        raise AssertionError("oversized base64 must be rejected before decode")

    monkeypatch.setattr(module.base64, "b64decode", forbidden_decode)
    with pytest.raises(module.SocialHubTaskAdapterError, match="asset_too_large"):
        await integration.call_tool(
            "social_task_attach_asset",
            {
                "task_id": TASK_ID,
                "asset_base64": "A" * 5,
                "asset_name": "x.png",
                "asset_channel": "facebook",
                "content_type": "image/png",
            },
            context={},
        )
    assert decode_called is False


@pytest.mark.asyncio
async def test_asset_name_rejects_non_ascii_and_control_chars(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    for asset_name in ("รูป.png", "bad\rname.png", "bad\nname.png", "bad\x01name.png", "../bad.png"):
        with pytest.raises(module.SocialHubTaskAdapterError, match="asset_name_invalid"):
            await integration.call_tool(
                "social_task_attach_asset",
                {
                    "task_id": TASK_ID,
                    "asset_base64": base64.b64encode(b"x").decode(),
                    "asset_name": asset_name,
                    "asset_channel": "facebook",
                    "content_type": "image/png",
                },
                context={},
            )


@pytest.mark.asyncio
async def test_channels_require_at_least_one_value(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    for args in ({"instructions": "No channels"}, {"instructions": "Empty", "channels": []}):
        with pytest.raises(module.SocialHubTaskAdapterError, match="channels_invalid"):
            await integration.call_tool("social_task_create", args, context={})


@pytest.mark.asyncio
async def test_status_authenticated_get(hub_server, token_file, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    result = await integration.call_tool(
        "social_task_status",
        {"task_id": TASK_ID},
        context={},
    )
    assert result["status"] == "ok"
    assert result["task_id"] == TASK_ID
    assert result["task"]["status"] == "completed"
    assert "task" not in result["task"]


@pytest.mark.asyncio
async def test_status_rejects_missing_token(hub_server, tmp_path, monkeypatch):
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_URL", hub_server)
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(tmp_path / "nonexistent"))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    with pytest.raises(module.SocialHubTaskAdapterError, match="token_unavailable"):
        await integration.call_tool(
            "social_task_status",
            {"task_id": TASK_ID},
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
    tools = asyncio.run(integration.list_tools())
    names = [t["name"] for t in tools]
    assert names == ["social_task_create", "social_task_attach_asset", "social_task_status"]


def test_create_schema_forced_fields_not_caller_controlled():
    """source_app, requested_by, assigned_agent are NOT in the input schema."""
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    import asyncio
    tools = asyncio.run(integration.list_tools())
    create_tool = next(t for t in tools if t["name"] == "social_task_create")
    props = create_tool["inputSchema"]["properties"]
    assert "source_app" not in props
    assert "requested_by" not in props
    assert "assigned_agent" not in props


def test_schemas_bound_channels_correlation_task_ids_and_asset_payload():
    module = load_plugin()
    import asyncio
    tools = asyncio.run(module.SocialHubTaskIntegration().list_tools())
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    create = schemas["social_task_create"]
    assert create["properties"]["channels"]["minItems"] == 1
    assert "channels" in create["required"]
    assert create["properties"]["correlation_id"]["maxLength"] == 160
    for name in ("social_task_status", "social_task_attach_asset"):
        task_id_schema = schemas[name]["properties"]["task_id"]
        assert task_id_schema["pattern"] == r"^task_[0-9a-f]{16}$"
        assert task_id_schema["maxLength"] == 21
    assert schemas["social_task_attach_asset"]["properties"]["asset_base64"]["maxLength"] == module._MAX_ASSET_BASE64_CHARS


def test_certify_no_network_side_effect(token_file, monkeypatch):
    """certify() makes no network calls."""
    monkeypatch.setenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", str(token_file))
    module = load_plugin()
    integration = module.SocialHubTaskIntegration()
    import asyncio
    result = asyncio.run(integration.certify())
    assert result["network_call"] is False
    assert result["ok"] is True
    assert "test-social-hub-token-12345" not in repr(result)
