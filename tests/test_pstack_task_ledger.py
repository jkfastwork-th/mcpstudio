from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_studio import main
from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.pstack_lifecycle import PstackLifecycleError


class _MemorylessAdapter:
    def activity_snapshot(self, *, since=0, limit=250):
        return {"traces": [], "events": []}


class _Registration:
    def __init__(self, adapter=None):
        self.stage = "ready"
        self.adapter = adapter or _MemorylessAdapter()


class _SnapshotDb:
    async def list_events(self, **_kwargs):
        return [
            {
                "created_at": "2026-09-28T10:00:00+00:00",
                "kind": "pstack.task.opened",
                "message": "Task one opened",
                "data": {
                    "task_id": "TASK-1",
                    "trace_id": "TRACE-1",
                    "workspace_key": "alpha",
                    "goal": "Fix reconnect crash",
                    "workflow": "bug-fix",
                    "actor": "hirda",
                    "phase": "task_opened",
                    "title": "HIRDA task envelope opened",
                    "detail": "opened",
                    "status": "running",
                    "trace_status": "active",
                },
            },
            {
                "created_at": "2026-09-28T10:01:00+00:00",
                "kind": "pstack.tool_completed",
                "message": "find_symbol completed",
                "data": {
                    "task_id": "TASK-1",
                    "trace_id": "TRACE-1",
                    "workspace_key": "alpha",
                    "goal": "Fix reconnect crash",
                    "workflow": "bug-fix",
                    "actor": "tool",
                    "phase": "tool_completed",
                    "title": "find_symbol completed",
                    "detail": "done",
                    "status": "passed",
                    "tool": "find_symbol",
                    "trace_status": "active",
                },
            },
            {
                "created_at": "2026-09-28T11:00:00+00:00",
                "kind": "pstack.task.opened",
                "message": "Task two opened",
                "data": {
                    "task_id": "TASK-2",
                    "trace_id": "TRACE-2",
                    "workspace_key": "beta",
                    "goal": "Add activity timeline",
                    "workflow": "feature",
                    "actor": "hirda",
                    "phase": "task_opened",
                    "title": "HIRDA task envelope opened",
                    "detail": "opened",
                    "status": "running",
                    "trace_status": "active",
                    "lifecycle": {
                        "schema": "hirda-pstack-lifecycle-v1",
                        "workflow": "feature",
                        "status": "active",
                        "current_phase": "analyze",
                        "stages": [
                            {"phase": "analyze", "status": "active", "evidence": []},
                            {"phase": "plan", "status": "pending", "evidence": []},
                        ],
                    },
                },
            },
        ]


@pytest.mark.asyncio
async def test_pstack_activity_snapshot_reconstructs_multiple_persistent_tasks(
    monkeypatch,
):
    monkeypatch.setattr(main, "db", _SnapshotDb())
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())

    snapshot = await main._pstack_activity_snapshot(limit=100)

    assert snapshot["persistent"] is True
    assert snapshot["trace_count"] == 2
    assert snapshot["event_count"] == 3
    assert [item["trace_id"] for item in snapshot["traces"]] == [
        "TRACE-2",
        "TRACE-1",
    ]
    assert snapshot["traces"][0]["goal"] == "Add activity timeline"
    assert snapshot["traces"][0]["lifecycle"]["current_phase"] == "analyze"
    assert snapshot["traces"][1]["identity"]["workspace_key"] == "alpha"


class _GatewayDb:
    def __init__(self):
        self.metadata = {}
        self.logical_metadata = {}
        self.events = []

    async def get_gateway_session(self, gateway_id):
        return {
            "id": gateway_id,
            "studio_session_id": "studio-1",
            "metadata": dict(self.metadata),
        }

    async def update_gateway_session_metadata(self, gateway_id, patch):
        self.metadata.update(patch)
        return {"id": gateway_id, "metadata": dict(self.metadata)}

    async def heartbeat_session(self, session_id, patch):
        assert session_id == "studio-1"
        self.logical_metadata.update(dict(patch.get("metadata") or {}))
        return {
            "id": session_id,
            "metadata": dict(self.logical_metadata),
        }

    async def get_session(self, session_id):
        assert session_id == "studio-1"
        return {
            "id": session_id,
            "metadata": dict(self.logical_metadata),
        }

    async def get_managed_session(self, session_id):
        assert session_id == "ms-1"
        return {
            "id": "ms-1",
            "workspace_key": "mcp-studio",
        }

    async def add_event(self, kind, message, **kwargs):
        self.events.append((kind, message, kwargs))


class _GatewayPstackAdapter:
    def activity_snapshot(self, *, since=0, limit=250):
        return {
            "events": [
                {
                    "trace_id": "TRACE-GW",
                    "actor": "pstack",
                    "phase": "received",
                    "title": "Task received",
                    "detail": "received",
                    "status": "running",
                    "workflow": "feature",
                    "role": None,
                    "tool": "route_task",
                    "metadata": {},
                },
                {
                    "trace_id": "TRACE-GW",
                    "actor": "pstack",
                    "phase": "handoff_ready",
                    "title": "Ready for HIRDA",
                    "detail": "ready",
                    "status": "ready",
                    "workflow": "feature",
                    "role": "coordinator",
                    "tool": "route_task",
                    "metadata": {},
                },
            ]
        }


class _GatewayIntegrations:
    def __init__(self):
        self.registration = _Registration(_GatewayPstackAdapter())
        self.calls = []

    def get(self, integration_id):
        assert integration_id == "pstack"
        return self.registration

    async def reconcile(self, integration_id):
        return {"id": integration_id, "stage": "ready"}

    async def call_backend_tool(self, name, arguments, *, context):
        self.calls.append((name, dict(arguments), dict(context)))
        return {
            "trace_id": "TRACE-GW",
            "workflow": "feature",
            "coordinator": {
                "hirda_request": {
                    "request_id": "PSTACK-REQ-1",
                    "capability": "coding",
                    "preferred_depth": "deep",
                    "prompt": "PSTACK METHODOLOGY: feature\n\nTASK: Add persistent Pstack task ledger",
                    "context": {
                        "workflow_provider": "pstack",
                        "workflow": "feature",
                    },
                }
            },
        }


@pytest.mark.asyncio
async def test_gateway_task_envelope_persists_trace_and_tool_activity():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()

    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session={
            "id": "ms-1",
            "workspace_key": "mcp-studio",
        },
        goal="Add persistent Pstack task ledger",
    )

    assert task is not None
    assert task["trace_id"] == "TRACE-GW"
    assert task["workspace_key"] == "mcp-studio"
    assert task["status"] == "ready_for_hirda"
    assert task["lifecycle"]["current_phase"] == "analyze"
    assert task["lifecycle"]["status"] == "active"
    assert task["workflow_directive"]["capability"] == "coding"
    assert task["workflow_directive"]["context"]["workflow_provider"] == "pstack"
    assert "HIRDA PSTACK LIFECYCLE CONTROL" in task["workflow_directive"]["prompt"]
    assert "mcpstudio_pstack_checkpoint" in task["workflow_directive"]["prompt"]
    assert "PSTACK METHODOLOGY" in task["workflow_directive"]["prompt"]
    assert gateway.db.metadata["active_pstack_task"]["task_id"] == task["task_id"]
    assert gateway.db.logical_metadata["active_pstack_task"]["trace_id"] == "TRACE-GW"
    kinds = [item[0] for item in gateway.db.events]
    assert kinds.count("pstack.trace.event") == 2
    assert "pstack.task.opened" in kinds

    session = {
        "studio_session_id": "studio-1",
        "metadata": {},
    }
    await gateway._record_active_pstack_tool(
        session,
        tool_name="find_symbol",
        status="running",
        detail="HIRDA dispatched find_symbol.",
        metadata={"argument_keys": ["name_path_pattern"]},
    )
    await gateway._record_active_pstack_tool(
        session,
        tool_name="find_symbol",
        status="passed",
        detail="find_symbol finished.",
    )

    tool_events = [
        item for item in gateway.db.events
        if item[0] in {"pstack.tool_started", "pstack.tool_completed"}
    ]
    assert len(tool_events) == 2
    assert tool_events[0][2]["data"]["trace_id"] == "TRACE-GW"
    assert tool_events[1][2]["data"]["status"] == "passed"
    hirda_events = [
        item for item in gateway.db.events
        if item[0] == "pstack.hirda_execution_started"
    ]
    assert len(hirda_events) == 1
    assert hirda_events[0][2]["data"]["trace_status"] == "executing"
    assert gateway.db.logical_metadata["active_pstack_task"]["status"] == "active"


@pytest.mark.asyncio
async def test_gateway_finalize_tool_mirrors_passed_evidence_into_lifecycle():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()

    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        goal="Mirror tool completion into Pstack lifecycle",
    )
    assert task is not None

    await gateway._finalize_pstack_tool(
        "gws-1",
        session={
            "studio_session_id": "studio-1",
            "managed_session_id": "ms-1",
            "metadata": {"active_pstack_task": task},
        },
        pstack_task=task,
        pstack_managed={"id": "ms-1", "workspace_key": "mcp-studio"},
        tool_name="find_symbol",
        tool_class="read",
        status_code=200,
    )

    lifecycle = gateway.db.metadata["active_pstack_task"]["lifecycle"]
    evidence = lifecycle["stages"][0]["evidence"]
    assert evidence[-1]["tool"] == "find_symbol"
    assert evidence[-1]["tool_class"] == "read"
    assert evidence[-1]["status"] == "passed"


@pytest.mark.asyncio
async def test_gateway_pstack_checkpoint_persists_enforced_lifecycle():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()

    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        goal="Implement enforced Pstack lifecycle",
    )
    assert task is not None

    task = await gateway._record_pstack_lifecycle_tool_evidence(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        tool_name="find_symbol",
        tool_class="read",
        status="passed",
        detail="Inspected the affected subsystem.",
    )
    assert task is not None
    assert task["lifecycle"]["current_phase"] == "analyze"

    task = await gateway._checkpoint_active_pstack_task(
        "gws-1",
        phase="analyze",
        status="passed",
        evidence="Inspected the gateway and identified the workflow boundary.",
    )
    assert task["lifecycle"]["current_phase"] == "plan"

    task = await gateway._checkpoint_active_pstack_task(
        "gws-1",
        phase="plan",
        status="passed",
        evidence="Plan the lifecycle state, gateway enforcement, and regression tests.",
    )
    assert task["lifecycle"]["current_phase"] == "implement"
    assert gateway.db.metadata["active_pstack_task"]["lifecycle"]["current_phase"] == "implement"
    assert gateway.db.logical_metadata["active_pstack_task"]["lifecycle"]["current_phase"] == "implement"

    checkpoint_events = [
        item for item in gateway.db.events
        if item[0] == "pstack.lifecycle.checkpoint"
    ]
    assert [item[2]["data"]["phase"] for item in checkpoint_events] == [
        "lifecycle_analyze_passed",
        "lifecycle_plan_passed",
    ]


@pytest.mark.asyncio
async def test_gateway_prefers_newer_logical_pstack_revision_over_stale_gateway_copy():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.db.metadata["active_pstack_task"] = {
        "task_id": "TASK-SAME",
        "trace_id": "TRACE-SAME",
        "goal": "Continue engineering work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "status": "active",
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "analyze",
            "revision": 1,
            "updated_at": 1.0,
            "stages": [],
        },
    }
    gateway.db.logical_metadata["active_pstack_task"] = {
        **gateway.db.metadata["active_pstack_task"],
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "implement",
            "revision": 8,
            "updated_at": 8.0,
            "stages": [],
        },
    }

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
    )

    assert task is not None
    assert task["lifecycle"]["revision"] == 8
    assert task["lifecycle"]["current_phase"] == "implement"
    assert gateway.db.metadata["active_pstack_task"]["lifecycle"]["revision"] == 8


@pytest.mark.asyncio
async def test_gateway_reuses_logical_pstack_task_on_fresh_transport():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.db.logical_metadata["active_pstack_task"] = {
        "task_id": "TASK-LOGICAL",
        "trace_id": "TRACE-LOGICAL",
        "goal": "Continue engineering work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "status": "ready_for_hirda",
    }

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
    )

    assert task is not None
    assert task["trace_id"] == "TRACE-LOGICAL"
    assert gateway.db.metadata["active_pstack_task"]["trace_id"] == "TRACE-LOGICAL"


@pytest.mark.asyncio
async def test_gateway_opens_new_task_after_completed_same_goal():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.db.metadata["active_pstack_task"] = {
        "task_id": "TASK-DONE",
        "trace_id": "TRACE-DONE",
        "goal": "Repeatable engineering task",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "status": "completed",
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "completed",
            "current_phase": "done",
            "revision": 6,
            "stages": [
                {"phase": "analyze", "status": "passed", "evidence": []},
                {"phase": "plan", "status": "passed", "evidence": []},
                {"phase": "implement", "status": "passed", "evidence": []},
                {"phase": "review", "status": "passed", "evidence": []},
                {"phase": "verify", "status": "passed", "evidence": []},
                {"phase": "done", "status": "passed", "evidence": []},
            ],
        },
    }

    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        goal="Repeatable engineering task",
    )

    assert task is not None
    assert task["task_id"] != "TASK-DONE"
    assert task["trace_id"] == "TRACE-GW"
    assert task["lifecycle"]["current_phase"] == "analyze"


def test_gateway_requires_pstack_admission_when_integration_is_configured():
    gateway = object.__new__(GatewaySessionManager)
    gateway.integrations = _GatewayIntegrations()

    with pytest.raises(PstackLifecycleError) as exc:
        gateway._require_pstack_admission(None)

    assert exc.value.code == "PSTACK_WORKFLOW_REQUIRED"


def test_pstack_ui_exposes_multi_task_ledger():
    root = main.ROOT
    app = (root / "static" / "app.pstack.js").read_text(encoding="utf-8")
    css = (root / "static" / "pstack.css").read_text(encoding="utf-8")

    assert "Recent Pstack tasks" in app
    assert "data-pstack-trace" in app
    assert "pstackSelectedTraceId" in app
    assert "persistent task traces" in app
    assert "HIRDA STACK FLOW" in app
    assert "Task flow overview" in app
    for label in ("Task", "Pstack", "JEV", "HIRDA", "Runtime / Tools", "Verify", "Done"):
        assert label in app
    assert "downstreamSeen?'skipped':'waiting'" in app
    assert "lifecycleStages" in app
    assert "stage&&stage.phase" in app
    assert "BLOCKED" in app
    assert ".pstack-flow-step.skipped" in css
    assert ".pstack-flow-step" in css
    assert ".pstack-task-row" in css
    assert ".pstack-ledger-card" in css
