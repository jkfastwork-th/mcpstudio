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
    work_rows = {}

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

    async def create_work(payload, default_server_id):
        row = {
            "id": "work-skill",
            "state": "queued",
            "execution_state": "none",
            "metadata": payload["metadata"],
            "result": {},
        }
        work_rows[row["id"]] = row
        return dict(row)

    async def start_deterministic_work(work_id):
        row = work_rows[work_id]
        row.update({"state": "running", "execution_state": "running_action"})
        return dict(row)

    async def finish_work(work_id, *, state, result=None, error=None):
        row = work_rows[work_id]
        row.update(
            {
                "state": state,
                "execution_state": state,
                "result": result or {},
                "error": error,
            }
        )
        return dict(row)

    db.get_gateway_session = get_gateway_session
    db.get_managed_session = get_managed_session
    db.create_work = create_work
    db.start_deterministic_work = start_deterministic_work
    db.finish_work = finish_work

    manager = GatewaySessionManager(
        settings(tmp_path),
        db,
        integrations=FakeIntegrations(),
        skill_runtime=runtime(),
    )

    class FakeSkillGateway:
        def prepare_run(self, **kwargs):
            return {
                "invocation": {
                    "invocation_id": "skill-inv-test",
                    "intent": kwargs["intent"],
                    "domain": kwargs["domain"],
                    "skill_ids": [kwargs["skill_id"]],
                    "priority": kwargs["priority"],
                    "input": kwargs["input_data"],
                },
                "task_overrides": {
                    "priority": kwargs["priority"],
                    "skill_invocation": {
                        "invocation_id": "skill-inv-test",
                        "intent": kwargs["intent"],
                        "domain": kwargs["domain"],
                        "skill_ids": [kwargs["skill_id"]],
                        "priority": kwargs["priority"],
                        "input": kwargs["input_data"],
                    },
                    "skill_resolution": {},
                    "capability_resolution": {"resolved": True},
                    "runtime_constraints": [],
                },
            }

    manager.skill_gateway = FakeSkillGateway()
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
        assert kwargs["task_overrides"]["skill_invocation"]["invocation_id"] == "skill-inv-test"
        return dict(task)

    async def persist(*args, **kwargs):
        calls.append(("persist", args[1].get("work_id")))
        return args[1]

    async def record(*args, **kwargs):
        calls.append(("evidence", kwargs["tool_name"], kwargs["status"]))
        return dict(task)

    async def checkpoint(*args, **kwargs):
        calls.append(("checkpoint", kwargs["phase"], kwargs["status"]))
        return {
            **task,
            "work_id": "work-skill",
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
            "publish_policy": "draft",
            "comment_policy": "off",
            "result": {
                "task_id": "social-task-1",
                "status": "needs_review",
            },
        }

    manager._active_pstack_gateway_task = active
    manager._open_pstack_gateway_task = open_task
    manager._persist_active_pstack_task = persist
    manager._record_pstack_lifecycle_tool_evidence = record
    manager._checkpoint_active_pstack_task = checkpoint
    manager._run_action_skill = run_action

    result = await manager._run_governed_action_skill(
        "gws-test",
        {
            "skill_id": "social.publish",
            "instruction": "create governed draft",
            "parameters": {
                "publish_policy": "draft",
                "comment_policy": "off",
                "channels": ["facebook"],
            },
        },
    )

    assert result["result"]["task_id"] == "social-task-1"
    assert result["work_id"] == "work-skill"
    assert result["pstack_task"]["status"] == "completed"
    completed_work = work_rows["work-skill"]
    assert completed_work["state"] == "completed"
    assert completed_work["result"]["action_verification"]["passed"] is True
    assert completed_work["metadata"]["skill_invocation"]["invocation_id"] == "skill-inv-test"
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
