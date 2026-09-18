from __future__ import annotations

import pytest

from mcp_studio.capsules import CapsuleNotFound, CapsuleService
from mcp_studio.agent_runtimes import AgentRuntimeInventory


class FakeDB:
    def __init__(self):
        self.events = []

    async def add_event(self, kind, message, *, severity="info", server_id=None, data=None):
        i = len(self.events) + 1
        self.events.append(
            {
                "id": i,
                "kind": kind,
                "severity": severity,
                "server_id": server_id,
                "message": message,
                "data": data or {},
                "created_at": f"2026-09-18T08:00:{i:02d}+00:00",
            }
        )

    async def recent_events(self, limit=100):
        return list(reversed(self.events))[:limit]


@pytest.mark.asyncio
async def test_capsule_handoff_uses_capsule_id_as_visual_connector():
    service = CapsuleService(FakeDB())
    created = await service.create(
        title="Continue MCP Studio",
        workspace="mcp-studio",
        source_pane="w1:p1",
        agent="claude",
        capsule_id="C-204",
    )
    assert created["capsule_id"] == "C-204"
    assert created["current_agent"] == "claude"

    await service.set_stage("C-204", stage="agent_runtime", agent="claude")
    handed = await service.handoff(
        "C-204",
        from_agent="claude",
        to_agent="hermes",
        reason="quota_exhausted",
    )

    assert handed["current_agent"] == "hermes"
    assert handed["current_stage"] == "agent_runtime"
    assert handed["last_handoff"]["connector_id"] == "C-204"
    assert handed["last_handoff"]["handoff_id"].startswith("H-")
    assert handed["last_handoff"]["reason"] == "quota_exhausted"

    overview = await service.overview()
    assert overview["summary"] == {"active": 1, "total": 1, "handoffs": 1}

    done = await service.complete("C-204", agent="hermes")
    assert done["status"] == "completed"
    assert done["current_stage"] == "result"


@pytest.mark.asyncio
async def test_capsule_missing_fails_closed():
    service = CapsuleService(FakeDB())
    with pytest.raises(CapsuleNotFound):
        await service.handoff(
            "C-missing",
            from_agent="claude",
            to_agent="hermes",
        )


class FakeHerdr:
    snapshot = {
        "status": "healthy",
        "panes": {
            "panes": [
                {
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "agent": "claude",
                    "agent_status": "blocked",
                    "model": "claude-sonnet",
                    "provider": "anthropic",
                },
                {
                    "pane_id": "w1:p7",
                    "workspace_id": "w1",
                    "agent": "hermes",
                    "agent_status": "idle",
                    "model": "upstage/solar-pro4:free",
                    "provider": "nous",
                },
            ]
        },
    }

    @staticmethod
    def pane_list(snapshot):
        return snapshot["panes"]["panes"]


def test_runtime_inventory_detects_three_lanes_and_nous_free(monkeypatch):
    monkeypatch.setattr(
        "mcp_studio.agent_runtimes.shutil.which",
        lambda name: f"/usr/local/bin/{name}",
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_version",
        staticmethod(lambda binary: f"{binary.rsplit('/', 1)[-1]} v-test"),
    )
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_hermes_config",
        staticmethod(lambda: {}),
    )

    data = AgentRuntimeInventory(FakeHerdr(), ttl_seconds=60).snapshot(force=True)
    by_id = {item["id"]: item for item in data["runtimes"]}

    assert set(by_id) == {"claude", "codex", "hermes"}
    assert by_id["claude"]["status"] == "blocked"
    assert by_id["codex"]["status"] == "available"
    assert by_id["hermes"]["status"] == "ready"
    assert by_id["hermes"]["model"] == "upstage/solar-pro4:free"
    assert by_id["hermes"]["free"] is True
    assert by_id["hermes"]["nous_free"]["active"] is True
    assert data["summary"]["installed"] == 3
    assert data["summary"]["free_active"] == 1
