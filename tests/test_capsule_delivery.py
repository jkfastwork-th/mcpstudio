from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from mcp_studio.capsules import CapsuleDeliveryError, CapsuleService


class FakeDB:
    def __init__(self):
        self.events = []
        self.lanes = {}

    async def add_event(
        self,
        kind,
        message,
        *,
        severity="info",
        server_id=None,
        data=None,
    ):
        self.events.append(
            {
                "id": len(self.events) + 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "severity": severity,
                "server_id": server_id,
                "message": message,
                "data": dict(data or {}),
            }
        )

    async def recent_events(self, limit=100):
        return list(reversed(self.events[-limit:]))

    async def list_lane_states(self):
        return [dict(value) for value in self.lanes.values()]

    async def set_lane_state(self, agent, state, *, reason=None, actor="system"):
        row = {
            "agent": agent,
            "state": state,
            "reason": reason,
            "actor": actor,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.lanes[agent] = row
        return dict(row)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args, client_name=None):
        self.calls.append(
            {
                "name": name,
                "args": dict(args),
                "client_name": client_name,
            }
        )
        return {"content": [{"type": "text", "text": '{"ok":true}'}]}


class FakeHerdr:
    def __init__(self, *, pane=True):
        self.snapshot = {"status": "healthy"}
        self.client_instance = FakeClient()
        self.pane_enabled = pane
        self.tool_definition = {
            "name": "herdr_prompt_agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Unique agent name or pane ID.",
                    },
                    "message": {"type": "string"},
                    "wait": {"type": "boolean", "default": False},
                    "timeout_ms": {"type": "integer"},
                },
                "required": ["agent_id", "message"],
            },
        }

    async def refresh(self):
        return self.snapshot

    def find_pane(self, *, pane_id=None, workspace=None, agent=None):
        if not self.pane_enabled:
            return None
        pane = {
            "pane_id": "pane-hermes",
            "agent": "hermes",
            "agent_status": "idle",
            "revision": 1,
        }
        if pane_id and pane_id != pane["pane_id"]:
            return None
        if agent and agent != pane["agent"]:
            return None
        return pane

    def pane_list(self, snapshot=None):
        if not self.pane_enabled:
            return []
        return [
            {
                "pane_id": "pane-hermes",
                "agent": "hermes",
                "agent_status": "idle",
                "revision": 1,
            }
        ]

    def tool(self, name):
        return self.tool_definition if name == "herdr_prompt_agent" else None

    def client(self):
        return self.client_instance


def handoff_metadata():
    return {
        "model_capability": {
            "provider": "test",
            "model_id": "hermes-test",
            "context_window": 128000,
            "max_output_tokens": 8192,
            "system_prompt_tokens": 1000,
            "tool_schema_tokens": 1000,
            "safety_reserve_tokens": 4096,
        }
    }


def capsule_contract(portability="safe"):
    return {
        "objective": "Preserve the existing implementation while continuing the task.",
        "locked_decisions": ["Do not change the public API."],
        "artifacts": ["mcp_studio/capsules.py"],
        "current_state": {"tests": "passing"},
        "checkpoint": {"git": "clean"},
        "next_actions": ["Continue implementation."],
        "acceptance_criteria": ["Existing behavior remains compatible."],
        "handoff_checks": ["baseline tests pass"],
        "constraints": ["Preserve semantic intent."],
        "portability": portability,
        "target_capabilities": {
            "hermes": handoff_metadata()["model_capability"],
        },
    }


async def prepared_service(*, pane=True):
    db = FakeDB()
    herdr = FakeHerdr(pane=pane)
    service = CapsuleService(
        db,
        herdr,
        local_api_base="http://127.0.0.1:8100",
    )
    capsule = await service.create(
        title="Capsule delivery test",
        workspace="mcp-studio",
        source_pane="test",
        agent="claude",
        metadata={
            "context_profile": {
                "source_tokens": 4000,
                "retained_tokens": 3000,
            }
        },
    )
    await service.set_stage(
        capsule["capsule_id"],
        stage="agent_runtime",
        agent="claude",
    )
    return service, db, herdr, capsule["capsule_id"]


def delivery_token_from_call(herdr: FakeHerdr) -> str:
    assert herdr.client_instance.calls
    prompt = herdr.client_instance.calls[-1]["args"]["message"]
    match = re.search(r'"delivery_token":"([^"]+)"', prompt)
    assert match, prompt
    return match.group(1)


@pytest.mark.asyncio
async def test_handoff_dispatch_does_not_transfer_ownership_before_ack():
    service, _, herdr, capsule_id = await prepared_service()

    state = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        from_stage="agent_runtime",
        to_stage="agent_runtime",
        metadata=handoff_metadata(),
    )

    assert state["current_agent"] == "claude"
    assert state["current_stage"] == "agent_runtime"
    assert state["handoffs"] == []
    assert state["pending_handoff"]["to_agent"] == "hermes"
    assert state["pending_handoff"]["state"] == "dispatched"
    assert [
        event["kind"]
        for event in state["events"]
        if event["kind"].startswith("capsule.handoff")
    ] == [
        "capsule.handoff_requested",
        "capsule.handoff_dispatched",
    ]

    call = herdr.client_instance.calls[-1]
    assert call["name"] == "herdr_prompt_agent"
    assert call["args"]["agent_id"] == "pane-hermes"
    assert call["args"]["wait"] is False
    assert "/handoff/" in call["args"]["message"]
    assert "/ack" in call["args"]["message"]


@pytest.mark.asyncio
async def test_valid_target_ack_commits_transfer_and_is_idempotent():
    service, _, herdr, capsule_id = await prepared_service()

    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)

    committed = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt={"transport": "herdr", "pane_id": "pane-hermes"},
    )

    assert committed["current_agent"] == "hermes"
    assert committed["pending_handoff"] is None
    assert len(committed["handoffs"]) == 1
    assert committed["handoffs"][0]["state"] == "committed"
    assert committed["handoffs"][0]["from_agent"] == "claude"
    assert committed["handoffs"][0]["to_agent"] == "hermes"

    again = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt={"transport": "herdr", "pane_id": "pane-hermes"},
    )
    assert again["current_agent"] == "hermes"
    assert len(again["handoffs"]) == 1


@pytest.mark.asyncio
async def test_invalid_ack_token_never_changes_owner():
    service, _, _, capsule_id = await prepared_service()

    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]

    with pytest.raises(ValueError, match="invalid handoff delivery token"):
        await service.acknowledge_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token="x" * 32,
        )

    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"
    assert state["pending_handoff"]["handoff_id"] == handoff_id
    assert state["handoffs"] == []


@pytest.mark.asyncio
async def test_missing_target_fails_delivery_and_retains_owner():
    service, _, _, capsule_id = await prepared_service(pane=False)

    with pytest.raises(CapsuleDeliveryError, match="target_pane_not_found"):
        await service.handoff(
            capsule_id,
            from_agent="claude",
            to_agent="hermes",
            metadata=handoff_metadata(),
        )

    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"
    assert state["pending_handoff"] is None
    assert len(state["failed_handoffs"]) == 1
    assert state["failed_handoffs"][0]["to_agent"] == "hermes"


@pytest.mark.asyncio
async def test_owner_mismatch_is_rejected_before_dispatch():
    service, _, herdr, capsule_id = await prepared_service()

    with pytest.raises(ValueError, match="handoff owner mismatch"):
        await service.handoff(
            capsule_id,
            from_agent="hermes",
            to_agent="claude",
            metadata=handoff_metadata(),
        )

    assert herdr.client_instance.calls == []
    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"


@pytest.mark.asyncio
async def test_stage_and_complete_cannot_bypass_handoff_ownership():
    service, _, _, capsule_id = await prepared_service()

    with pytest.raises(ValueError, match="cannot transfer capsule ownership"):
        await service.set_stage(
            capsule_id,
            stage="agent_runtime",
            agent="hermes",
        )

    with pytest.raises(ValueError, match="does not own capsule"):
        await service.complete(capsule_id, agent="hermes")

    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"
    assert state["status"] == "active"


@pytest.mark.asyncio
async def test_pending_handoff_blocks_stage_and_completion_until_ack():
    service, _, _, capsule_id = await prepared_service()

    await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )

    with pytest.raises(ValueError, match="capsule_handoff_in_progress"):
        await service.set_stage(capsule_id, stage="review", agent="claude")

    with pytest.raises(ValueError, match="capsule_handoff_in_progress"):
        await service.complete(capsule_id, agent="claude")


@pytest.mark.asyncio
async def test_safe_emergency_lane_handoff_requires_validation_before_commit():
    service, _, herdr, capsule_id = await prepared_service()
    await service.update_contract(capsule_id, capsule_contract("safe"))

    result = await service.set_lane_state(
        "claude",
        "emergency",
        reason="quota_critical",
        actor="test",
        auto_handoff=True,
    )
    assert result["auto_handoff"][0]["status"] == "dispatched"

    pending = await service.get(capsule_id)
    assert pending["current_agent"] == "claude"
    assert pending["pending_handoff"]["to_agent"] == "hermes"
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)

    acknowledged = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt={"transport": "herdr", "pane_id": "pane-hermes"},
    )
    assert acknowledged["current_agent"] == "claude"
    assert acknowledged["pending_handoff"]["state"] == "acknowledged"

    committed = await service.validate_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        passed=True,
        checks=[
            {
                "criterion": "baseline tests pass",
                "status": "pass",
                "evidence": "pytest green",
            }
        ],
    )
    assert committed["current_agent"] == "hermes"
    assert committed["pending_handoff"] is None


@pytest.mark.asyncio
async def test_guarded_emergency_handoff_waits_for_explicit_approval():
    service, _, herdr, capsule_id = await prepared_service()
    await service.update_contract(capsule_id, capsule_contract("guarded"))

    await service.set_lane_state(
        "claude",
        "disabled",
        reason="preserve_quota",
        actor="test",
        auto_handoff=True,
    )
    pending = await service.get(capsule_id)
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)

    await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt={"transport": "herdr", "pane_id": "pane-hermes"},
    )
    validated = await service.validate_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        passed=True,
        checks=[
            {
                "criterion": "baseline tests pass",
                "status": "pass",
                "evidence": "pytest green",
            }
        ],
        plan="Continue without changing locked decisions.",
    )
    assert validated["current_agent"] == "claude"
    assert validated["pending_handoff"]["state"] == "awaiting_approval"

    committed = await service.approve_handoff(
        capsule_id,
        handoff_id,
        approved_by="chatgpt/sol",
    )
    assert committed["current_agent"] == "hermes"
    assert committed["pending_handoff"] is None


@pytest.mark.asyncio
async def test_pinned_capsule_is_not_auto_handed_off_from_emergency_lane():
    service, _, _, capsule_id = await prepared_service()
    await service.update_contract(capsule_id, capsule_contract("pinned"))

    result = await service.set_lane_state(
        "claude",
        "emergency",
        reason="quota_critical",
        actor="test",
        auto_handoff=True,
    )

    assert result["auto_handoff"] == [
        {
            "capsule_id": capsule_id,
            "status": "blocked",
            "reason": "capsule_pinned",
        }
    ]
    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"
    assert state["pending_handoff"] is None
    assert state["handoff_review"]["reason"] == "capsule_pinned"


@pytest.mark.asyncio
async def test_guarded_contract_without_handoff_checks_is_rejected():
    service, _, _, capsule_id = await prepared_service()
    contract = capsule_contract("guarded")
    contract["handoff_checks"] = []
    with pytest.raises(ValueError, match="requires at least one handoff_check"):
        await service.update_contract(capsule_id, contract)
