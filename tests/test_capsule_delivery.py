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
            "cwd": "mcp-studio",
            "foreground_cwd": "mcp-studio",
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
                "cwd": "mcp-studio",
                "foreground_cwd": "mcp-studio",
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


def correlated_receipt(state: dict) -> dict:
    pending = state["pending_handoff"]
    return {
        "transport": "herdr",
        "pane_id": "pane-hermes",
        "correlation": dict(pending["correlation"]),
    }


def completion_sentinel_from_call(herdr: FakeHerdr) -> str:
    assert herdr.client_instance.calls
    prompt = herdr.client_instance.calls[-1]["args"]["message"]
    match = re.search(r'"sentinel":"([^"]+)"', prompt)
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
    assert '"context_projection": {' in call["args"]["message"]
    assert '"context_projection_sha256":' in call["args"]["message"]
    assert '"projection_sha256":' in call["args"]["message"]

    dispatched = next(
        event
        for event in state["events"]
        if event["kind"] == "capsule.handoff_dispatched"
    )
    assert dispatched["data"]["context_projection_sha256"]
    assert dispatched["data"]["context_projection_stream_seq"] >= 1
    assert dispatched["data"]["context_projection_version"] == 1


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
        receipt=correlated_receipt(pending),
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
        receipt=correlated_receipt(pending),
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
        receipt=correlated_receipt(pending),
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
        receipt=correlated_receipt(pending),
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

@pytest.mark.asyncio
async def test_handoff_ack_requires_correlation_echo():
    service, _, herdr, capsule_id = await prepared_service()
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)

    with pytest.raises(ValueError, match="correlation missing"):
        await service.acknowledge_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token=token,
            receipt={"transport": "herdr", "pane_id": "pane-hermes"},
        )

    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"
    assert state["pending_handoff"]["handoff_id"] == handoff_id


@pytest.mark.asyncio
async def test_handoff_ack_rejects_wrong_turn_and_generation():
    service, _, herdr, capsule_id = await prepared_service()
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)

    wrong_turn = correlated_receipt(pending)
    wrong_turn["correlation"]["turn_id"] = "T-WRONG"
    with pytest.raises(ValueError, match="turn_id"):
        await service.acknowledge_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token=token,
            receipt=wrong_turn,
        )

    wrong_generation = correlated_receipt(pending)
    wrong_generation["correlation"]["generation"] = "G-STALE"
    with pytest.raises(ValueError, match="generation"):
        await service.acknowledge_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token=token,
            receipt=wrong_generation,
        )

    state = await service.get(capsule_id)
    assert state["current_agent"] == "claude"
    assert state["pending_handoff"]["state"] == "dispatched"


@pytest.mark.asyncio
async def test_handoff_correlation_is_bound_to_target_session():
    service, _, herdr, capsule_id = await prepared_service()
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)

    assert pending["pending_handoff"]["correlation"]["target_session"] == "pane-hermes"
    assert pending["pending_handoff"]["correlation"]["generation"].startswith("G-")

    committed = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt=correlated_receipt(pending),
    )
    correlation = committed["last_handoff"]["correlation"]
    assert correlation["handoff_id"] == handoff_id
    assert correlation["target_session"] == "pane-hermes"
    assert correlation["to_agent"] == "hermes"

@pytest.mark.asyncio
async def test_completion_requires_exact_per_turn_sentinel():
    service, _, herdr, capsule_id = await prepared_service()
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)
    sentinel = completion_sentinel_from_call(herdr)

    committed = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt=correlated_receipt(pending),
    )
    completion_receipt = {
        "transport": "herdr",
        "pane_id": "pane-hermes",
        "correlation": dict(committed["last_handoff"]["correlation"]),
    }

    with pytest.raises(ValueError, match="invalid handoff completion sentinel"):
        await service.complete_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token=token,
            sentinel=sentinel + "-WRONG",
            receipt=completion_receipt,
        )

    state = await service.get(capsule_id)
    assert "completion" not in state["last_handoff"]


@pytest.mark.asyncio
async def test_valid_completion_sentinel_marks_exact_handoff_complete_idempotently():
    service, _, herdr, capsule_id = await prepared_service()
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)
    sentinel = completion_sentinel_from_call(herdr)

    committed = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt=correlated_receipt(pending),
    )
    completion_receipt = {
        "transport": "herdr",
        "pane_id": "pane-hermes",
        "correlation": dict(committed["last_handoff"]["correlation"]),
    }

    completed = await service.complete_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        sentinel=sentinel,
        receipt=completion_receipt,
    )
    assert completed["last_handoff"]["completion"]["state"] == "completed"
    assert completed["last_handoff"]["completion"]["sentinel_verified"] is True
    assert completed["last_handoff"]["completed_at"]

    again = await service.complete_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        sentinel=sentinel,
        receipt=completion_receipt,
    )
    completion_events = [
        event
        for event in again["events"]
        if event["kind"] == "capsule.handoff_completed"
    ]
    assert len(completion_events) == 1


@pytest.mark.asyncio
async def test_completion_rejects_wrong_turn_correlation():
    service, _, herdr, capsule_id = await prepared_service()
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=handoff_metadata(),
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)
    sentinel = completion_sentinel_from_call(herdr)

    committed = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt=correlated_receipt(pending),
    )
    receipt = {
        "transport": "herdr",
        "pane_id": "pane-hermes",
        "correlation": dict(committed["last_handoff"]["correlation"]),
    }
    receipt["correlation"]["turn_id"] = "T-NEIGHBOUR"

    with pytest.raises(ValueError, match="turn_id"):
        await service.complete_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token=token,
            sentinel=sentinel,
            receipt=receipt,
        )


@pytest.mark.asyncio
async def test_completion_cannot_precede_validation_gated_commit():
    service, _, herdr, capsule_id = await prepared_service()
    metadata = handoff_metadata()
    metadata.update(
        {
            "requires_validation": True,
            "contract": capsule_contract("safe"),
        }
    )
    pending = await service.handoff(
        capsule_id,
        from_agent="claude",
        to_agent="hermes",
        metadata=metadata,
    )
    handoff_id = pending["pending_handoff"]["handoff_id"]
    token = delivery_token_from_call(herdr)
    sentinel = completion_sentinel_from_call(herdr)

    acknowledged = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt=correlated_receipt(pending),
    )
    assert acknowledged["current_agent"] == "claude"

    with pytest.raises(ValueError, match="must be committed before completion"):
        await service.complete_handoff(
            capsule_id,
            handoff_id,
            agent="hermes",
            delivery_token=token,
            sentinel=sentinel,
            receipt=correlated_receipt(acknowledged),
        )


@pytest.mark.asyncio
async def test_draining_lane_auto_rollover_preserves_stage_and_completes():
    service, _, herdr, capsule_id = await prepared_service()
    await service.update_contract(capsule_id, capsule_contract("safe"))
    await service.set_stage(
        capsule_id,
        stage="implementation",
        agent="claude",
    )

    result = await service.set_lane_state(
        "claude",
        "draining",
        reason="planned_runtime_drain",
        actor="test",
        auto_handoff=True,
    )

    assert result["lane"]["state"] == "draining"
    dispatched = result["auto_handoff"][0]
    assert dispatched["status"] == "dispatched"
    assert dispatched["source_lane_state"] == "draining"
    assert dispatched["stage"] == "implementation"
    assert dispatched["to_agent"] == "hermes"

    pending = await service.get(capsule_id)
    handoff = pending["pending_handoff"]
    assert handoff["from_stage"] == "implementation"
    assert handoff["to_stage"] == "implementation"
    assert handoff["metadata"]["handoff_mode"] == "safe_auto"
    assert handoff["metadata"]["auto_failover"]["source_stage"] == "implementation"

    delivery_call = herdr.client_instance.calls[-1]
    prompt = delivery_call["args"]["message"]
    assert "AUTOMATIC LANE ROLLOVER" in prompt
    assert "Do not restart work that the source lane already completed." in prompt
    assert '"context_projection": {' in prompt

    token = delivery_token_from_call(herdr)
    sentinel = completion_sentinel_from_call(herdr)
    receipt = correlated_receipt(pending)
    handoff_id = handoff["handoff_id"]

    acknowledged = await service.acknowledge_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        receipt=receipt,
    )
    assert acknowledged["current_agent"] == "claude"

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
                "evidence": "focused rollover certification green",
            }
        ],
    )
    assert committed["current_agent"] == "hermes"
    assert committed["current_stage"] == "implementation"

    completed = await service.complete_handoff(
        capsule_id,
        handoff_id,
        agent="hermes",
        delivery_token=token,
        sentinel=sentinel,
        receipt=receipt,
    )
    assert completed["last_handoff"]["completion"]["sentinel_verified"] is True

    kinds = [event["kind"] for event in completed["events"]]
    assert "capsule.auto_handoff_started" in kinds
    assert "capsule.auto_handoff_dispatched" in kinds
    assert "capsule.auto_handoff_committed" in kinds
    assert "capsule.auto_handoff_completed" in kinds


@pytest.mark.asyncio
async def test_auto_rollover_skips_context_incompatible_target_and_uses_next():
    class MultiPaneHerdr(FakeHerdr):
        def __init__(self):
            super().__init__(pane=True)
            self.panes = [
                {
                    "pane_id": "pane-hermes",
                    "agent": "hermes",
                    "agent_status": "idle",
                    "revision": 1,
                    "cwd": "mcp-studio",
                    "foreground_cwd": "mcp-studio",
                },
                {
                    "pane_id": "pane-codex",
                    "agent": "codex",
                    "agent_status": "idle",
                    "revision": 1,
                    "cwd": "mcp-studio",
                    "foreground_cwd": "mcp-studio",
                },
            ]

        def pane_list(self, snapshot=None):
            return [dict(item) for item in self.panes]

        def find_pane(self, *, pane_id=None, workspace=None, agent=None):
            candidates = self.panes
            if pane_id:
                candidates = [
                    item for item in candidates if item["pane_id"] == pane_id
                ]
            if agent:
                candidates = [
                    item for item in candidates if item["agent"] == agent
                ]
            return dict(candidates[0]) if candidates else None

    db = FakeDB()
    herdr = MultiPaneHerdr()
    service = CapsuleService(db, herdr)
    capsule = await service.create(
        title="Fallback fit test",
        workspace="mcp-studio",
        source_pane="pane-claude",
        agent="claude",
        metadata={
            "context_profile": {
                "source_tokens": 6000,
                "retained_tokens": 5000,
            }
        },
    )
    capsule_id = capsule["capsule_id"]
    contract = capsule_contract("safe")
    contract["target_capabilities"] = {
        "hermes": {
            "provider": "test",
            "model_id": "hermes-too-small",
            "context_window": 8000,
            "max_output_tokens": 2048,
            "system_prompt_tokens": 1000,
            "tool_schema_tokens": 1000,
            "safety_reserve_tokens": 4096,
        },
        "codex": {
            "provider": "test",
            "model_id": "codex-large",
            "context_window": 128000,
            "max_output_tokens": 8192,
            "system_prompt_tokens": 1000,
            "tool_schema_tokens": 1000,
            "safety_reserve_tokens": 4096,
        },
    }
    await service.update_contract(capsule_id, contract)

    result = await service.set_lane_state(
        "claude",
        "emergency",
        reason="primary_context_exhausted",
        actor="test",
        auto_handoff=True,
    )

    dispatched = result["auto_handoff"][0]
    assert dispatched["status"] == "dispatched"
    assert dispatched["to_agent"] == "codex"
    evaluations = dispatched["pending_handoff"]["metadata"]["auto_failover"][
        "candidate_evaluations"
    ]
    assert evaluations[0]["agent"] == "hermes"
    assert evaluations[0]["eligible"] is False
    assert evaluations[0]["reason"] == "capsule_context_too_large"
    assert evaluations[1]["agent"] == "codex"
    assert evaluations[1]["eligible"] is True
    assert herdr.client_instance.calls[-1]["args"]["agent_id"] == "pane-codex"


@pytest.mark.asyncio
async def test_guarded_auto_rollover_resumes_target_after_approval():
    service, _, herdr, capsule_id = await prepared_service()
    await service.update_contract(capsule_id, capsule_contract("guarded"))

    await service.set_lane_state(
        "claude",
        "disabled",
        reason="provider_unavailable",
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
        receipt=correlated_receipt(pending),
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
                "evidence": "focused guarded rollover validation green",
            }
        ],
        plan="Continue from the delivered projection without semantic changes.",
    )
    assert validated["current_agent"] == "claude"
    assert validated["pending_handoff"]["state"] == "awaiting_approval"
    assert len(herdr.client_instance.calls) == 1

    committed = await service.approve_handoff(
        capsule_id,
        handoff_id,
        approved_by="operator/test",
    )
    assert committed["current_agent"] == "hermes"
    assert committed["pending_handoff"] is None
    assert len(herdr.client_instance.calls) == 2
    resume_call = herdr.client_instance.calls[-1]
    assert resume_call["args"]["agent_id"] == "pane-hermes"
    assert "HIRDA AUTO ROLLOVER RESUME" in resume_call["args"]["message"]
    assert "do not restart the task" in resume_call["args"]["message"]

    kinds = [event["kind"] for event in committed["events"]]
    assert "capsule.auto_handoff_committed" in kinds
    assert "capsule.auto_handoff_resumed" in kinds


@pytest.mark.asyncio
async def test_auto_rollover_never_selects_target_from_another_workspace():
    class CrossWorkspaceHerdr(FakeHerdr):
        def __init__(self):
            super().__init__(pane=True)
            self.panes = [
                {
                    "pane_id": "pane-hermes-airapari",
                    "agent": "hermes",
                    "agent_status": "idle",
                    "revision": 1,
                    "cwd": "/home/alfred/airapari",
                    "foreground_cwd": "/home/alfred/airapari",
                },
                {
                    "pane_id": "pane-codex-mcp-studio",
                    "agent": "codex",
                    "agent_status": "idle",
                    "revision": 1,
                    "cwd": "/home/alfred/mcp-studio",
                    "foreground_cwd": "/home/alfred/mcp-studio",
                },
            ]

        def pane_list(self, snapshot=None):
            return [dict(item) for item in self.panes]

        def find_pane(self, *, pane_id=None, workspace=None, agent=None):
            candidates = self.panes
            if pane_id:
                candidates = [
                    item for item in candidates if item["pane_id"] == pane_id
                ]
            if workspace:
                candidates = [
                    item
                    for item in candidates
                    if item["cwd"] == workspace
                    or item["foreground_cwd"] == workspace
                ]
            if agent:
                candidates = [
                    item for item in candidates if item["agent"] == agent
                ]
            return dict(candidates[0]) if candidates else None

    db = FakeDB()
    herdr = CrossWorkspaceHerdr()
    service = CapsuleService(db, herdr)
    capsule = await service.create(
        title="Workspace isolation rollover",
        workspace="/home/alfred/mcp-studio",
        source_pane="pane-claude",
        agent="claude",
        metadata={
            "context_profile": {
                "source_tokens": 3000,
                "retained_tokens": 2500,
            }
        },
    )
    capsule_id = capsule["capsule_id"]
    capability = handoff_metadata()["model_capability"]
    contract = capsule_contract("safe")
    contract["target_capabilities"] = {
        "hermes": dict(capability),
        "codex": {**capability, "model_id": "codex-test"},
    }
    await service.update_contract(capsule_id, contract)

    result = await service.set_lane_state(
        "claude",
        "draining",
        reason="workspace_isolation_cert",
        actor="test",
        auto_handoff=True,
    )

    selected = result["auto_handoff"][0]
    assert selected["status"] == "dispatched"
    assert selected["to_agent"] == "codex"
    pending = selected["pending_handoff"]
    assert pending["target_pane"] == "pane-codex-mcp-studio"
    evaluations = pending["metadata"]["auto_failover"]["candidate_evaluations"]
    assert evaluations[0] == {
        "agent": "hermes",
        "eligible": False,
        "reason": "idle_target_pane_in_workspace_required",
    }
    assert evaluations[1]["agent"] == "codex"
    assert evaluations[1]["eligible"] is True


@pytest.mark.asyncio
async def test_auto_rollover_prefers_dedicated_rollover_pane():
    service, _, herdr, capsule_id = await prepared_service()
    panes = [
        {
            "pane_id": "pane-hermes-a",
            "agent": "hermes",
            "agent_status": "idle",
            "cwd": "mcp-studio",
            "foreground_cwd": "mcp-studio",
        },
        {
            "pane_id": "pane-hermes-z",
            "agent": "hermes",
            "agent_status": "done",
            "name": "hirda-auto-rollover-hermes",
            "cwd": "mcp-studio",
            "foreground_cwd": "mcp-studio",
        },
    ]
    herdr.pane_list = lambda snapshot=None: panes
    herdr.find_pane = lambda *, pane_id=None, workspace=None, agent=None: next(
        (
            pane
            for pane in panes
            if (not pane_id or pane["pane_id"] == pane_id)
            and (not agent or pane["agent"] == agent)
            and (
                not workspace
                or pane["foreground_cwd"] == workspace
                or pane["cwd"] == workspace
            )
        ),
        None,
    )

    await service.update_contract(capsule_id, capsule_contract("safe"))
    result = await service.set_lane_state(
        "claude",
        "draining",
        reason="dedicated_rollover_target",
        actor="test",
        auto_handoff=True,
    )

    selected = result["auto_handoff"][0]
    assert selected["status"] == "dispatched"
    assert selected["pending_handoff"]["target_pane"] == "pane-hermes-z"
