from __future__ import annotations

import pytest

from mcp_studio.capsules import CapsuleNotFound, CapsuleService
from mcp_studio.agent_runtimes import AgentRuntimeInventory
from mcp_studio.context_fit import ContextProfile, ModelCapability, evaluate_context_fit


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
        metadata={"context_profile": {"source_tokens": 120000, "retained_tokens": 120000}},
    )
    assert created["capsule_id"] == "C-204"
    assert created["current_agent"] == "claude"

    await service.set_stage("C-204", stage="agent_runtime", agent="claude")
    handed = await service.handoff(
        "C-204",
        from_agent="claude",
        to_agent="hermes",
        reason="quota_exhausted",
        metadata={
            "model_capability": {
                "provider": "nous",
                "model_id": "upstage/solar-pro4:free",
                "context_window": 524288,
                "max_output_tokens": 32768,
                "system_prompt_tokens": 6000,
                "tool_schema_tokens": 8000,
                "safety_reserve_tokens": 16000,
                "last_verified_at": "2026-09-18T09:30:00Z",
            }
        },
    )

    assert handed["current_agent"] == "hermes"
    assert handed["current_stage"] == "agent_runtime"
    assert handed["last_handoff"]["connector_id"] == "C-204"
    assert handed["last_handoff"]["handoff_id"].startswith("H-")
    assert handed["last_handoff"]["reason"] == "quota_exhausted"
    assert handed["capsule_type"] == "full"
    assert handed["context_profile"]["retained_percent"] == 100.0
    assert handed["last_handoff"]["context_fit"]["fit"] is True
    assert handed["last_handoff"]["a2a"]["protocol"] == "a2a"
    assert handed["last_handoff"]["a2a"]["task_id"].startswith("A2A-")
    assert handed["last_handoff"]["a2a"]["context_id"] == "C-204"
    assert handed["last_handoff"]["a2a"]["task"]["kind"] == "task"
    assert handed["last_handoff"]["a2a"]["task"]["status"]["state"] == "submitted"

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


def test_context_profile_types_and_fit_math():
    full = ContextProfile(source_tokens=100000, retained_tokens=100000)
    compact = ContextProfile(source_tokens=100000, retained_tokens=62000)
    minimal = ContextProfile(source_tokens=100000, retained_tokens=18000)
    assert full.capsule_type == "full" and full.retained_percent == 100.0
    assert compact.capsule_type == "compact" and compact.retained_percent == 62.0
    assert minimal.capsule_type == "minimal" and minimal.retained_percent == 18.0

    cap = ModelCapability(
        provider="test",
        model_id="model-a",
        context_window=100000,
        max_output_tokens=10000,
        system_prompt_tokens=5000,
        tool_schema_tokens=5000,
        safety_reserve_tokens=10000,
    )
    safe = evaluate_context_fit(compact, cap)
    blocked = evaluate_context_fit(ContextProfile(source_tokens=120000, retained_tokens=90000), cap)
    assert safe["fit"] is True
    assert safe["usable_context_tokens"] == 70000
    assert blocked["fit"] is False
    assert blocked["fit_state"] == "blocked"


@pytest.mark.asyncio
async def test_handoff_fails_closed_when_context_exceeds_target_model():
    db = FakeDB()
    service = CapsuleService(db)
    await service.create(
        title="Oversized capsule",
        agent="claude",
        capsule_id="C-BIG",
        metadata={"context_profile": {"source_tokens": 200000, "retained_tokens": 150000}},
    )
    with pytest.raises(ValueError, match="capsule_context_too_large"):
        await service.handoff(
            "C-BIG",
            from_agent="claude",
            to_agent="hermes",
            metadata={
                "model_capability": {
                    "provider": "nous",
                    "model_id": "small-target",
                    "context_window": 100000,
                    "max_output_tokens": 10000,
                    "system_prompt_tokens": 5000,
                    "tool_schema_tokens": 5000,
                    "safety_reserve_tokens": 10000,
                }
            },
        )
    current = await service.get("C-BIG")
    assert current["current_agent"] == "claude"
    assert current["handoffs"] == []
    assert len(current["blocked_handoffs"]) == 1
    assert current["blocked_handoffs"][0]["reason"] == "capsule_context_too_large"
    assert current["blocked_handoffs"][0]["a2a"]["task"]["status"]["state"] == "rejected"


@pytest.mark.asyncio
async def test_handoff_requires_verified_context_profile_and_capability():
    db = FakeDB()
    service = CapsuleService(db)
    await service.create(title="No context profile", agent="claude", capsule_id="C-NOPROFILE")
    with pytest.raises(ValueError, match="capsule_context_profile_required"):
        await service.handoff("C-NOPROFILE", from_agent="claude", to_agent="codex")
    current = await service.get("C-NOPROFILE")
    assert current["current_agent"] == "claude"
    assert current["blocked_handoffs"][0]["reason"] == "capsule_context_profile_required"


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
