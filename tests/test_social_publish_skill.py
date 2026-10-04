from __future__ import annotations

from pathlib import Path

import pytest

from mcp_studio.gateway import GatewaySessionManager, ManagedSessionError
from mcp_studio.skill_runtime import SkillRuntime
from mcp_studio.settings import ServerConfig, Settings, StudioConfig


ROOT = Path(__file__).resolve().parents[1]


class FakeDb:
    def __init__(self):
        self.events = []

    async def add_event(self, kind, message, **kwargs):
        self.events.append((kind, message, kwargs))


class FakeIntegrations:
    def __init__(self, *, available=True):
        self.available = available
        self.calls = []

    def capability_providers(self, capability):
        assert capability == "social_hub_task_ingress"
        return [{
            "id": "social-hub-task",
            "available": self.available,
            "stage": "ready" if self.available else "blocked",
        }]

    def is_backend_tool(self, name):
        return self.available and name == "hirda__social_hub_task__social_task_create"

    async def call_backend_tool(self, name, arguments, *, context):
        self.calls.append((name, dict(arguments), dict(context)))
        return {
            "schema": "hirda-social-hub-task-create-v1",
            "task_id": "task_0123456789abcdef",
            "status": "needs_review",
            "correlation_id": "skill-correlation-1",
        }

    def backend_tools(self):
        return [{
            "name": "hirda__social_hub_task__social_task_create",
            "description": "backend social task",
            "inputSchema": {"type": "object", "properties": {}},
        }]


def settings(tmp_path: Path) -> Settings:
    return Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            gateway_enabled=True,
            gateway_allowed_servers=["serena-8001"],
            worker_server_id="serena-8001",
        ),
        servers=[
            ServerConfig(
                id="serena-8001",
                name="Serena",
                url="http://127.0.0.1:8001/mcp",
            )
        ],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )


def runtime() -> SkillRuntime:
    return SkillRuntime(
        enabled=True,
        path=ROOT / "skills",
        base_dir=ROOT,
    )


def test_social_publish_manifest_resolves_without_authority():
    rt = runtime()
    resolution = rt.registry.resolve(
        "ช่วยโพสต์เรื่องนี้ลง facebook",
        explicit_skill_ids=["social.publish"],
    )
    match = next(item for item in resolution.matches if item.skill.id == "social.publish")
    assert match.explicit is True
    assert match.skill.scope.kind == "global"
    assert resolution.required_capabilities == ("social_hub_task_ingress",)
    assert resolution.required_tools == ("social_task_create",)

    serialized = repr({
        "metadata": dict(match.skill.metadata),
        "policy_hints": match.skill.policy_hints,
        "workflow_steps": match.skill.workflow_steps,
    }).casefold()
    for forbidden in (
        "'model':", "'runtime':", "'machine':", "'agent':",
        "'permission':", "'provider':", "'router':", "'routing':",
        "'command':", "'adapter':", "'script':",
    ):
        assert forbidden not in serialized


def test_skill_run_is_priority_control_tool(tmp_path):
    manager = GatewaySessionManager(
        settings(tmp_path),
        FakeDb(),
        integrations=FakeIntegrations(),
        skill_runtime=runtime(),
    )
    names = [item["name"] for item in manager._all_local_tools()]
    assert "hirda_skill_run" in names
    assert "hirda__social_hub_task__social_task_create" not in names


@pytest.mark.asyncio
async def test_social_publish_dispatches_governed_backend_with_safe_defaults(tmp_path):
    integrations = FakeIntegrations()
    db = FakeDb()
    manager = GatewaySessionManager(
        settings(tmp_path),
        db,
        integrations=integrations,
        skill_runtime=runtime(),
    )

    result = await manager._run_action_skill(
        "gws-test",
        {
            "skill_id": "social.publish",
            "instruction": "เขียนโพสต์สรุป Agent รุ่นใหม่ แล้วเตรียมลง Facebook",
            "parameters": {
                "channels": ["facebook"],
                "correlation_id": "turn-123",
            },
        },
    )

    assert result["schema"] == "hirda-skill-run-v1"
    assert result["skill_id"] == "social.publish"
    assert result["publish_policy"] == "review"
    assert result["comment_policy"] == "track"
    assert result["result"]["status"] == "needs_review"

    name, args, context = integrations.calls[0]
    assert name == "hirda__social_hub_task__social_task_create"
    assert args["publish_policy"] == "review"
    assert args["comment_policy"] == "track"
    assert args["channels"] == ["facebook"]
    assert context["skill_id"] == "social.publish"
    assert context["conversation_id"] == "gws-test"
    assert context["parent_request_id"] == "turn-123"
    assert db.events[0][0] == "skill.executed"


@pytest.mark.asyncio
async def test_hirda_skill_run_owns_deterministic_pstack_lifecycle(tmp_path):
    db = FakeDb()

    async def get_gateway_session(gateway_session_id):
        assert gateway_session_id == "gws-test"
        return {"managed_session_id": "ms-test"}

    async def get_managed_session(managed_session_id):
        assert managed_session_id == "ms-test"
        return {
            "id": "ms-test",
            "project_path": "/ws/social",
            "workspace_key": "social",
        }

    db.get_gateway_session = get_gateway_session
    db.get_managed_session = get_managed_session
    manager = GatewaySessionManager(
        settings(tmp_path),
        db,
        integrations=FakeIntegrations(),
        skill_runtime=runtime(),
    )
    calls = []
    task = {
        "task_id": "TASK-SKILL",
        "trace_id": "TRACE-SKILL",
        "workflow": "feature",
        "status": "ready_for_hirda",
        "lifecycle": {"status": "active", "current_phase": "admitted"},
    }

    async def active(*args, **kwargs):
        return None

    async def open_task(*args, **kwargs):
        calls.append(("open", kwargs))
        assert kwargs["task_overrides"]["deterministic_action"] is True
        return dict(task)

    async def record(*args, **kwargs):
        calls.append(("evidence", kwargs["tool_name"], kwargs["status"]))
        return dict(task)

    async def checkpoint(*args, **kwargs):
        calls.append(("checkpoint", kwargs["phase"], kwargs["status"]))
        return {
            **task,
            "status": "completed" if kwargs["phase"] == "terminal" else "active",
            "lifecycle": {
                "status": "completed" if kwargs["phase"] == "terminal" else "active",
                "current_phase": kwargs["phase"],
            },
        }

    async def run_action(*args, **kwargs):
        calls.append(("action",))
        return {
            "schema": "hirda-skill-run-v1",
            "skill_id": "social.publish",
            "skill_version": "0.2.0",
            "result": {
                "task_id": "social-task-1",
                "status": "needs_review",
            },
        }

    manager._active_pstack_gateway_task = active
    manager._open_pstack_gateway_task = open_task
    manager._record_pstack_lifecycle_tool_evidence = record
    manager._checkpoint_active_pstack_task = checkpoint
    manager._run_action_skill = run_action

    result = await manager._run_governed_action_skill(
        "gws-test",
        {
            "skill_id": "social.publish",
            "instruction": "create governed draft",
            "parameters": {"publish_policy": "draft"},
        },
    )

    assert result["result"]["task_id"] == "social-task-1"
    assert result["pstack_task"]["status"] == "completed"
    assert [item for item in calls if item[0] == "checkpoint"] == [
        ("checkpoint", "admitted", "passed"),
        ("checkpoint", "working", "passed"),
        ("checkpoint", "verified", "passed"),
        ("checkpoint", "terminal", "passed"),
    ]
    assert ("action",) in calls


@pytest.mark.asyncio
async def test_skill_run_fail_closed_for_unregistered_or_unavailable_execution(tmp_path):
    manager = GatewaySessionManager(
        settings(tmp_path),
        FakeDb(),
        integrations=FakeIntegrations(),
        skill_runtime=runtime(),
    )
    with pytest.raises(ManagedSessionError, match="skill_not_found_or_not_applicable"):
        await manager._run_action_skill(
            "gws-test",
            {
                "skill_id": "oneiros.motion.shot-regeneration",
                "instruction": "regenerate this shot",
            },
        )

    unavailable = GatewaySessionManager(
        settings(tmp_path),
        FakeDb(),
        integrations=FakeIntegrations(available=False),
        skill_runtime=runtime(),
    )
    with pytest.raises(ManagedSessionError, match="skill_capability_unavailable"):
        await unavailable._run_action_skill(
            "gws-test",
            {
                "skill_id": "social.publish",
                "instruction": "โพสต์ลง facebook",
            },
        )
