import pytest

from mcp_studio.skill_gateway import SkillGateway
from mcp_studio.skill_runtime import SkillRequestError


class _Runtime:
    class _Registry:
        def snapshot(self):
            return []

    registry = _Registry()

    def enrich_work_payload(self, payload):
        metadata = dict(payload.get("metadata") or {})
        metadata["skill_resolution"] = {
            "schema": "hirda-skill-resolution-v1",
            "status": "ready",
            "selected": [{"id": "social.publish", "version": "0.2.0"}],
            "workflow_steps": ["validate identity", "create task", "verify"],
            "verification_checks": ["task provenance recorded"],
            "permission_requirements": ["social.publish"],
            "runtime_constraints": ["tool_use"],
        }
        return {**payload, "metadata": metadata}


class _Resolver:
    def resolve_skill_metadata(self, metadata):
        return {
            "schema": "hirda-capability-resolution-v1",
            "resolved": True,
            "status": "ready",
            "selected_provider_ids": ["social-hub-task"],
        }


class _DB:
    def __init__(self):
        self.recorded = []
        self.events = []

    async def list_skill_experiences(self, **kwargs):
        return []

    async def record_skill_experience(self, payload):
        item = {"id": "skill-exp-1", **payload, "created_at": "now"}
        self.recorded.append(item)
        return item

    async def add_event(self, kind, message, **kwargs):
        self.events.append((kind, message, kwargs))


def _gateway():
    return SkillGateway(_DB(), _Runtime(), _Resolver())


def test_skill_gateway_resolve_keeps_hirda_authority():
    result = _gateway().resolve(
        intent="publish this on facebook",
        input_data={"channel": "facebook", "caption": "hello"},
        workspace="/ws/social",
        domain="social",
    )
    assert result["resolved"] is True
    assert result["selected"][0]["id"] == "social.publish"
    assert result["authority"]["runtime"] == "hirda"
    assert result["authority"]["model"] == "hirda"
    assert result["authority"]["machine"] == "hirda"
    assert result["authority"]["provider"] == "hirda"
    assert result["authority"]["permissions"] == "hirda"


def test_skill_gateway_prepare_run_sanitizes_secretish_input():
    prepared = _gateway().prepare_run(
        intent="publish this on facebook",
        input_data={
            "channel": "facebook",
            "caption": "hello",
            "api_token": "do-not-learn-this",
        },
        workspace="/ws/social",
        domain="social",
    )
    invocation = prepared["invocation"]
    assert invocation["skill_ids"] == ["social.publish"]
    assert invocation["input"]["channel"] == "facebook"
    assert invocation["input"]["api_token"] == "<redacted>"
    assert "do-not-learn-this" not in prepared["goal"]
    assert prepared["task_overrides"]["runtime_constraints"] == ["tool_use"]


@pytest.mark.asyncio
async def test_skill_gateway_records_only_execution_certified_experience():
    gateway = _gateway()
    invocation = gateway.prepare_run(
        intent="publish this on facebook",
        input_data={
            "channel": "facebook",
            "account_id": "owner-profile",
            "caption": "hello",
        },
        workspace="/ws/social",
        domain="social",
    )["invocation"]
    work = {
        "id": "work-1",
        "state": "completed",
        "execution_state": "completed",
        "metadata": {
            "skill_invocation": invocation,
            "pstack_task_id": "TASK-1",
        },
        "result": {
            "semantic_completion": {"status": "success"},
            "runtime_readiness": {"ready": True},
        },
    }
    skipped = await gateway.record_experience(
        work=work,
        pstack_task={"task_id": "TASK-1"},
        execution_certification={"passed": False},
        quality_certification={"passed": True, "score": 1.0},
    )
    assert skipped is None
    assert gateway.db.recorded == []

    experience = await gateway.record_experience(
        work=work,
        pstack_task={"task_id": "TASK-1"},
        execution_certification={"passed": True, "outcome": "PASS"},
        quality_certification={"passed": True, "score": 0.95, "outcome": "PASS"},
    )
    assert experience["skill_ids"] == ["social.publish"]
    assert experience["channel"] == "facebook"
    assert experience["identity"]["account_id"] == "owner-profile"
    assert experience["outcome"] == "quality_pass"
    assert gateway.db.events[-1][0] == "skill.experience.recorded"


def test_skill_gateway_prepare_run_requires_resolved_skill():
    class _EmptyRuntime(_Runtime):
        def enrich_work_payload(self, payload):
            metadata = dict(payload.get("metadata") or {})
            metadata["skill_resolution"] = {"status": "ready", "selected": []}
            return {**payload, "metadata": metadata}

    gateway = SkillGateway(_DB(), _EmptyRuntime(), _Resolver())
    with pytest.raises(SkillRequestError, match="skill_not_resolved"):
        gateway.prepare_run(
            intent="unknown intent",
            input_data={},
            workspace="/ws/social",
        )
