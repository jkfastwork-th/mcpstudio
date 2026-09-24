from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.tool_permissions import classify_tool
from mcp_studio.world_authoring import WorldAuthoringError, WorldAuthoringManager


class FakeWorkspacePool:
    enabled = True

    def __init__(self, project_path: Path):
        self.project_path = project_path

    async def list_workspaces(self):
        return [
            {
                "key": "earth-pixi",
                "name": "Earth Pixi",
                "project_path": str(self.project_path),
                "enabled": True,
            }
        ]


class FakeProcess:
    def __init__(self, response: dict):
        self.returncode = 0
        self.response = response
        self.input_payload: bytes | None = None
        self.killed = False

    async def communicate(self, payload: bytes):
        self.input_payload = payload
        return json.dumps(self.response).encode(), b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_world_authoring_preview_executes_registered_workspace_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pixel_world = tmp_path / "pixel-world"
    (pixel_world / "tools").mkdir(parents=True)
    (pixel_world / "package.json").write_text("{}\n")
    (pixel_world / "tools" / "world-authoring.mjs").write_text("// test\n")

    pool = FakeWorkspacePool(tmp_path)
    manager = WorldAuthoringManager(pool, timeout_seconds=3)
    fake_process = FakeProcess(
        {
            "validation": {
                "valid": True,
                "errors": [],
                "requiresApproval": False,
                "commands": [{"type": "map.decorate"}],
            },
            "visualRoutes": [],
        }
    )
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return fake_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await manager.preview(
        workspace="earth-pixi",
        proposal={
            "proposalId": "p-1",
            "kind": "decorate_area",
            "intent": "sakura reading corner",
            "targetArea": {"x": 1, "y": 2, "width": 3, "height": 4},
        },
    )

    assert result["preview_only"] is True
    assert result["world_authority_changed"] is False
    assert result["asset_promoted"] is False
    assert result["earth_validation"] == {
        "configured": False,
        "status": "not_configured",
        "mutationAuthorized": False,
    }
    assert result["result"]["validation"]["valid"] is True
    assert calls[0][0] == ("npm", "run", "authoring:preview", "--silent")
    assert calls[0][1]["cwd"] == str(pixel_world)
    sent = json.loads(fake_process.input_payload.decode())
    assert sent["proposal"]["proposalId"] == "p-1"


def test_world_authoring_validator_url_is_loopback_only(tmp_path: Path):
    pool = FakeWorkspacePool(tmp_path)

    with pytest.raises(WorldAuthoringError, match="loopback HTTP"):
        WorldAuthoringManager(
            pool,
            validator_url="https://example.com/api/world-authoring/validate",
        )

    with pytest.raises(WorldAuthoringError, match="loopback HTTP"):
        WorldAuthoringManager(
            pool,
            validator_url="http://127.0.0.1:8816/other",
        )


@pytest.mark.asyncio
async def test_world_authoring_preview_calls_earth_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pixel_world = tmp_path / "pixel-world"
    (pixel_world / "tools").mkdir(parents=True)
    (pixel_world / "package.json").write_text("{}\n")
    (pixel_world / "tools" / "world-authoring.mjs").write_text("// test\n")

    fake_process = FakeProcess(
        {
            "proposal": {
                "proposalId": "p-earth-1",
                "kind": "decorate_area",
                "intent": "quiet sakura corner",
                "targetArea": {"x": 10, "y": 20, "width": 30, "height": 40},
            },
            "validation": {
                "valid": True,
                "errors": [],
                "requiresApproval": False,
                "commands": [
                    {
                        "type": "map.decorate",
                        "id": "p-earth-1",
                        "bounds": {"x": 10, "y": 20, "width": 30, "height": 40},
                        "tags": ["sakura"],
                    }
                ],
            },
            "visualRoutes": [],
        }
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "schema": "earth616-world-authoring-validation-v1",
                "status": "accepted",
                "valid": True,
                "requiresApproval": False,
                "mutationAuthorized": False,
                "worldAuthority": "earth-616",
                "errors": [],
                "warnings": [],
            },
        )

    manager = WorldAuthoringManager(
        FakeWorkspacePool(tmp_path),
        timeout_seconds=3,
        validator_url="http://127.0.0.1:8816/api/world-authoring/validate",
        validator_transport=httpx.MockTransport(handler),
    )
    result = await manager.preview(
        workspace="earth-pixi",
        proposal={
            "proposalId": "p-earth-1",
            "kind": "decorate_area",
            "intent": "quiet sakura corner",
            "targetArea": {"x": 10, "y": 20, "width": 30, "height": 40},
        },
    )

    assert seen["url"] == "http://127.0.0.1:8816/api/world-authoring/validate"
    assert seen["payload"]["proposal"]["proposalId"] == "p-earth-1"
    assert seen["payload"]["commands"][0]["type"] == "map.decorate"
    assert result["earth_validation"]["configured"] is True
    assert result["earth_validation"]["status"] == "accepted"
    assert result["earth_validation"]["valid"] is True
    assert result["earth_validation"]["mutationAuthorized"] is False


@pytest.mark.asyncio
async def test_world_authoring_validator_fails_closed_on_authority_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pixel_world = tmp_path / "pixel-world"
    (pixel_world / "tools").mkdir(parents=True)
    (pixel_world / "package.json").write_text("{}\n")
    (pixel_world / "tools" / "world-authoring.mjs").write_text("// test\n")

    fake_process = FakeProcess(
        {
            "validation": {
                "valid": True,
                "errors": [],
                "requiresApproval": False,
                "commands": [],
            },
            "visualRoutes": [],
        }
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valid": True,
                "mutationAuthorized": True,
            },
        )

    manager = WorldAuthoringManager(
        FakeWorkspacePool(tmp_path),
        validator_url="http://localhost:8816/api/world-authoring/validate",
        validator_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WorldAuthoringError, match="preview-only contract"):
        await manager.preview(
            workspace="earth-pixi",
            proposal={
                "proposalId": "p-authority-1",
                "kind": "change_atmosphere",
                "intent": "warm dusk",
            },
        )


class GatewayPool:
    enabled = True


class FakeWorldAuthoring:
    async def preview(self, **kwargs):
        return {
            "preview_only": True,
            "world_authority_changed": False,
            "asset_promoted": False,
            "echo": kwargs,
        }

    async def resolve_workspace(self, selector):
        return {
            "key": selector,
            "name": selector,
            "project_path": f"/data/{selector}",
        }

    def validate_audit_events(self, events):
        return [
            {
                "event_id": event["eventId"],
                "kind": event["kind"],
                "recorded_at": event.get("recordedAt", ""),
                "provider_id": event.get("providerId"),
                "job_id": event.get("jobId"),
                "request_id": event.get("requestId"),
                "logical_id": event.get("logicalId"),
                "pack_id": event.get("packId"),
                "details": dict(event.get("details") or {}),
            }
            for event in events
        ]


def test_gateway_advertises_world_authoring_only_when_configured():
    base = GatewaySessionManager(
        SimpleNamespace(),
        object(),
        managed_sessions=GatewayPool(),
    )
    assert "mcpstudio_world_authoring_preview" not in {
        tool["name"] for tool in base._management_tools()
    }

    enabled = GatewaySessionManager(
        SimpleNamespace(),
        object(),
        managed_sessions=GatewayPool(),
        world_authoring=FakeWorldAuthoring(),
    )
    enabled_names = {tool["name"] for tool in enabled._management_tools()}
    assert "mcpstudio_world_authoring_preview" in enabled_names
    assert "mcpstudio_world_authoring_audit" in enabled_names


@pytest.mark.asyncio
async def test_gateway_world_authoring_handler_is_preview_only():
    class FakeDb:
        def __init__(self):
            self.audits = []

        async def add_audit(self, action, **kwargs):
            self.audits.append((action, kwargs))

    db = FakeDb()
    gateway = GatewaySessionManager(
        SimpleNamespace(),
        db,
        managed_sessions=GatewayPool(),
        world_authoring=FakeWorldAuthoring(),
    )
    result = await gateway._handle_management_tool(
        "gws-test",
        "mcpstudio_world_authoring_preview",
        {
            "workspace": "earth-pixi",
            "proposal": {
                "proposalId": "p-2",
                "kind": "change_atmosphere",
                "intent": "warmer evening",
            },
        },
    )
    assert result["preview_only"] is True
    assert result["world_authority_changed"] is False
    assert result["asset_promoted"] is False
    assert result["echo"]["workspace"] == "earth-pixi"
    assert len(result["proposal_fingerprint"]) == 64
    assert db.audits[0][0] == "world_authoring.preview"
    assert db.audits[0][1]["data"]["preview_only"] is True
    assert db.audits[0][1]["data"]["world_authority_changed"] is False


def test_world_authoring_tool_is_execute_class():
    assert classify_tool("mcpstudio_world_authoring_preview", {}) == "execute"
    assert classify_tool("mcpstudio_world_authoring_audit", {}) == "execute"


@pytest.mark.asyncio
async def test_gateway_world_authoring_audit_persists_bounded_lifecycle_evidence():
    class FakeDb:
        def __init__(self):
            self.audits = []

        async def add_audit(self, action, **kwargs):
            self.audits.append((action, kwargs))

    db = FakeDb()
    gateway = GatewaySessionManager(
        SimpleNamespace(),
        db,
        managed_sessions=GatewayPool(),
        world_authoring=FakeWorldAuthoring(),
    )
    result = await gateway._handle_management_tool(
        "gws-test",
        "mcpstudio_world_authoring_audit",
        {
            "workspace": "earth-pixi",
            "events": [
                {
                    "eventId": "visual-audit-1",
                    "kind": "job_created",
                    "recordedAt": "2026-09-24T00:00:00Z",
                    "providerId": "local_free",
                    "jobId": "job-1",
                    "requestId": "proposal-1",
                    "logicalId": "prop.lantern",
                    "details": {"progress": 0},
                },
                {
                    "eventId": "visual-audit-2",
                    "kind": "asset_promoted",
                    "recordedAt": "2026-09-24T00:01:00Z",
                    "logicalId": "prop.lantern",
                },
            ],
        },
    )

    assert result["accepted"] == 2
    assert result["audit_only"] is True
    assert result["world_authority_changed"] is False
    assert result["asset_promoted_by_hirda"] is False
    assert [action for action, _ in db.audits] == [
        "world_authoring.job_created",
        "world_authoring.asset_promoted",
    ]
    assert all(
        row["data"]["world_authority_changed"] is False
        for _, row in db.audits
    )


def test_world_authoring_audit_validator_rejects_unknown_event_kind():
    manager = WorldAuthoringManager(SimpleNamespace())
    with pytest.raises(WorldAuthoringError, match="unsupported"):
        manager.validate_audit_events([
            {"eventId": "x", "kind": "mutate_world"}
        ])
