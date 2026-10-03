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


    async def list_sessions(self, limit=500):
        assert limit == 500
        return []


class _OverflowSnapshotDb:
    def __init__(self):
        self.calls = []
        self.opened = {
            "id": 1,
            "created_at": "2026-10-02T10:00:00+00:00",
            "kind": "pstack.task.opened",
            "message": "Task opened",
            "data": {
                "task_id": "TASK-OVERFLOW",
                "trace_id": "TRACE-OVERFLOW",
                "workspace_key": "overflow",
                "goal": "Finish after the event window fills",
                "workflow": "feature",
                "actor": "hirda",
                "phase": "task_opened",
                "title": "HIRDA task envelope opened",
                "status": "ready",
                "trace_status": "ready_for_hirda",
            },
        }
        self.tab_opened = {
            "id": 5001,
            "created_at": "2026-10-02T10:05:00+00:00",
            "kind": "pstack.herdr_tab.opened",
            "message": "Pstack HERDR task tab opened.",
            "data": {
                "task_id": "TASK-OVERFLOW",
                "trace_id": "TRACE-OVERFLOW",
                "workspace_key": "overflow",
                "tab_id": "wH:t9",
                "tab_label": "[PStack] TASK-OVERFLOW",
                "herdr_workspace_id": "wH",
            },
        }
        self.completed = {
            "id": 5002,
            "created_at": "2026-10-02T10:06:00+00:00",
            "kind": "pstack.lifecycle.checkpoint",
            "message": "Pstack done checkpoint passed.",
            "data": {
                "task_id": "TASK-OVERFLOW",
                "trace_id": "TRACE-OVERFLOW",
                "workspace_key": "overflow",
                "goal": "Finish after the event window fills",
                "workflow": "feature",
                "actor": "hirda",
                "phase": "lifecycle_done_passed",
                "title": "Done passed",
                "status": "passed",
                "trace_status": "completed",
                "lifecycle": {
                    "schema": "hirda-pstack-lifecycle-v1",
                    "workflow": "feature",
                    "status": "completed",
                    "current_phase": "done",
                    "revision": 75,
                    "stages": [
                        {"phase": "verify", "status": "passed", "evidence": []},
                        {"phase": "done", "status": "passed", "evidence": []},
                    ],
                },
            },
        }
        self.tab_closed = {
            "id": 5003,
            "created_at": "2026-10-02T10:06:01+00:00",
            "kind": "pstack.herdr_tab.closed",
            "message": "Pstack HERDR task tab closed.",
            "data": {
                "task_id": "TASK-OVERFLOW",
                "trace_id": "TRACE-OVERFLOW",
                "workspace_key": "overflow",
                "tab_id": "wH:t9",
                "tab_label": "[PStack] TASK-OVERFLOW",
            },
        }

    async def list_events(self, **kwargs):
        self.calls.append(dict(kwargs))
        kind_prefix = kwargs.get("kind_prefix")
        ascending = kwargs.get("ascending", True)
        if kind_prefix == "pstack.task.opened":
            return [dict(self.opened)]
        if kind_prefix == "pstack.":
            if ascending:
                # Reproduce the old bug: LIMIT on ascending order sees only
                # the stale beginning of an overfull event ledger.
                return [dict(self.opened)]
            return [
                dict(self.tab_closed),
                dict(self.completed),
                dict(self.tab_opened),
            ]
        return []


    async def list_sessions(self, limit=500):
        assert limit == 500
        return []


class _CanonicalSessionSnapshotDb:
    async def list_events(self, **_kwargs):
        return []

    async def list_sessions(self, limit=500):
        assert limit == 500
        return [
            {
                "id": "gws-canonical",
                "last_seen_at": "2026-10-02T10:30:00+00:00",
                "metadata": {
                    "active_pstack_task": {
                        "task_id": "TASK-CANONICAL",
                        "trace_id": "TRACE-CANONICAL",
                        "goal": "Canonical task survives ledger truncation",
                        "workflow": "feature",
                        "workspace_key": "canonical",
                        "managed_session_id": "ms-canonical",
                        "gateway_session_id": "gws-canonical",
                        "status": "completed",
                        "lifecycle": {
                            "schema": "hirda-pstack-lifecycle-v1",
                            "workflow": "feature",
                            "status": "completed",
                            "current_phase": "done",
                            "revision": 75,
                            "updated_at": 1790936846.0,
                            "stages": [
                                {"phase": "verify", "status": "passed", "evidence": []},
                                {"phase": "done", "status": "passed", "evidence": []},
                            ],
                        },
                        "herdr_tab": {
                            "status": "closed",
                            "owner": "pstack",
                            "persistent": False,
                            "workspace_id": "wH",
                            "tab_id": "wH:t7",
                            "label": "[PStack] TASK-CANONICAL",
                        },
                    }
                },
            }
        ]


class _ExpiredSessionSnapshotDb(_CanonicalSessionSnapshotDb):
    async def list_sessions(self, limit=500):
        rows = await super().list_sessions(limit=limit)
        task = rows[0]["metadata"]["active_pstack_task"]
        task["task_id"] = "TASK-EXPIRED"
        task["trace_id"] = "TRACE-EXPIRED"
        task["goal"] = "Expired task must stay terminal"
        task["status"] = "expired"
        task["terminal_disposition"] = "expired"
        task["lifecycle"]["terminal_disposition"] = "expired"
        return rows


class _GatewayOnlyExpiredSnapshotDb(_ExpiredSessionSnapshotDb):
    async def list_sessions(self, limit=500):
        assert limit == 500
        return []

    async def list_gateway_sessions(self, limit=500):
        assert limit == 500
        return await super().list_sessions(limit=limit)

    async def list_managed_sessions(self, limit=500):
        assert limit == 500
        return []


class _OrphanOwnedSnapshotDb:
    async def list_events(self, **kwargs):
        row = {
            "id": 901,
            "created_at": "2026-10-03T05:03:12+00:00",
            "kind": "pstack.task.opened",
            "message": "Orphaned task opened",
            "data": {
                "task_id": "TASK-ORPHAN",
                "trace_id": "TRACE-ORPHAN",
                "workspace_key": "mcp-studio",
                "managed_session_id": "ms-orphan",
                "gateway_session_id": "gws-orphan",
                "goal": "Old managed task with no canonical owner",
                "workflow": "feature",
                "actor": "hirda",
                "phase": "task_opened",
                "title": "HIRDA task envelope opened",
                "status": "running",
                "trace_status": "active",
                "lifecycle": {
                    "schema": "hirda-pstack-lifecycle-v1",
                    "workflow": "feature",
                    "status": "active",
                    "current_phase": "analyze",
                    "stages": [{"phase": "analyze", "status": "active", "evidence": []}],
                },
            },
        }
        prefix = kwargs.get("kind_prefix")
        if prefix and not row["kind"].startswith(prefix):
            return []
        return [row]

    async def list_sessions(self, limit=500):
        assert limit == 500
        return []

    async def list_gateway_sessions(self, limit=500):
        assert limit == 500
        return []

    async def list_managed_sessions(self, limit=500):
        assert limit == 500
        return []


class _WritableSnapshotDb:
    def __init__(self):
        self.rows = []
        self.gateway_metadata = {"active_pstack_task": {
            "trace_id": "TRACE-COGNITIVE",
            "status": "ready_for_hirda",
        }}
        self.logical_metadata = {}

    async def add_event(self, kind, message, **kwargs):
        data = dict(kwargs.get("data") or {})
        self.rows.append({
            "created_at": data.get("activity_timestamp") or "2026-09-28T12:00:00+00:00",
            "kind": kind,
            "message": message,
            "data": data,
            "correlation_id": kwargs.get("correlation_id"),
        })

    async def list_events(self, **_kwargs):
        rows = list(self.rows)
        if _kwargs.get("kind_prefix"):
            rows = [row for row in rows if row["kind"].startswith(_kwargs["kind_prefix"])]
        if _kwargs.get("correlation_id"):
            rows = [row for row in rows if row.get("correlation_id") == _kwargs["correlation_id"]]
        return rows if _kwargs.get("ascending", True) else list(reversed(rows))


    async def list_sessions(self, limit=500):
        assert limit == 500
        return []

    async def get_gateway_session(self, _gateway_id):
        return {
            "id": "gws-cognitive",
            "studio_session_id": "studio-cognitive",
            "metadata": dict(self.gateway_metadata),
        }

    async def update_gateway_session_metadata(self, _gateway_id, patch):
        self.gateway_metadata.update(dict(patch))

    async def heartbeat_session(self, _session_id, patch):
        self.logical_metadata.update(dict(patch.get("metadata") or {}))


class _RecordingActivityAdapter:
    def __init__(self):
        self.events = []
        self.trace = {
            "trace_id": "TRACE-COGNITIVE",
            "status": "ready_for_hirda",
            "updated_at": "2026-09-28T11:59:00+00:00",
        }

    def record_external_event(self, trace_id, **event):
        assert trace_id == self.trace["trace_id"]
        recorded = {
            "trace_id": trace_id,
            "timestamp": f"2026-09-28T12:00:0{len(self.events)}+00:00",
            "sequence": len(self.events) + 1,
            **event,
        }
        self.events.append(recorded)
        self.trace["status"] = event.get("trace_status") or self.trace["status"]
        self.trace["updated_at"] = recorded["timestamp"]
        return recorded

    def activity_snapshot(self, *, since=0, limit=250):
        return {"traces": [dict(self.trace)], "events": list(self.events)}


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


@pytest.mark.asyncio
async def test_pstack_snapshot_prefers_recent_completion_beyond_event_window(monkeypatch):
    database = _OverflowSnapshotDb()
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())

    snapshot = await main._pstack_activity_snapshot(limit=100)

    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-OVERFLOW")
    assert trace["task_id"] == "TASK-OVERFLOW"
    assert trace["goal"] == "Finish after the event window fills"
    assert trace["status"] == "completed"
    assert trace["lifecycle"]["status"] == "completed"
    assert trace["lifecycle"]["current_phase"] == "done"
    assert trace["current_step"]["phase"] == "done"
    assert trace["current_step"]["title"] == "Done"
    assert trace["herdr_tab"]["status"] == "closed"
    assert trace["herdr_tab"]["tab_id"] == "wH:t9"

    pstack_calls = [
        call for call in database.calls
        if call.get("kind_prefix") == "pstack."
    ]
    assert pstack_calls
    assert pstack_calls[-1]["ascending"] is False
    assert pstack_calls[-1]["limit"] == 5000


@pytest.mark.asyncio
async def test_pstack_snapshot_merges_canonical_gateway_task_when_ledger_has_no_trace(monkeypatch):
    database = _CanonicalSessionSnapshotDb()
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())

    snapshot = await main._pstack_activity_snapshot(limit=100)

    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-CANONICAL")
    assert trace["task_id"] == "TASK-CANONICAL"
    assert trace["goal"] == "Canonical task survives ledger truncation"
    assert trace["status"] == "completed"
    assert trace["lifecycle"]["status"] == "completed"
    assert trace["lifecycle"]["current_phase"] == "done"
    assert trace["current_step"] == {
        "phase": "done",
        "title": "Done",
        "status": "passed",
        "actor": "hirda",
    }
    assert trace["identity"]["gateway_session_id"] == "gws-canonical"
    assert trace["herdr_tab"]["status"] == "closed"
    assert trace["herdr_tab"]["tab_id"] == "wH:t7"


@pytest.mark.asyncio
async def test_pstack_snapshot_preserves_expired_terminal_state(monkeypatch):
    database = _ExpiredSessionSnapshotDb()
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())

    snapshot = await main._pstack_activity_snapshot(limit=100)

    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-EXPIRED")
    assert trace["task_id"] == "TASK-EXPIRED"
    assert trace["status"] == "expired"
    assert trace["lifecycle"]["status"] == "completed"
    assert snapshot["active_trace"] is None


@pytest.mark.asyncio
async def test_pstack_snapshot_uses_gateway_terminal_state_when_logical_copy_is_missing(monkeypatch):
    database = _GatewayOnlyExpiredSnapshotDb()
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())

    snapshot = await main._pstack_activity_snapshot(limit=100)

    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-EXPIRED")
    assert trace["status"] == "expired"
    assert snapshot["active_trace"] is None


@pytest.mark.asyncio
async def test_pstack_snapshot_expires_orphaned_managed_trace(monkeypatch):
    database = _OrphanOwnedSnapshotDb()
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())

    snapshot = await main._pstack_activity_snapshot(limit=100)

    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-ORPHAN")
    assert trace["status"] == "expired"
    assert trace["terminal_disposition"] == "expired"
    assert trace["projection_reason"] == "canonical_owner_missing"
    assert trace["lifecycle"]["status"] == "completed"
    assert trace["lifecycle"]["current_phase"] == "done"
    assert snapshot["active_trace"] is None


@pytest.mark.asyncio
async def test_pstack_snapshot_merges_memory_events_for_persisted_trace(monkeypatch):
    class _MemoryWithExistingTrace:
        def activity_snapshot(self, *, since=0, limit=250):
            return {
                "traces": [{
                    "trace_id": "TRACE-1",
                    "status": "executing",
                    "updated_at": "2026-09-28T12:00:00+00:00",
                }],
                "events": [{
                    "trace_id": "TRACE-1",
                    "timestamp": "2026-09-28T12:00:00+00:00",
                    "actor": "hirda",
                    "phase": "memory_only_hirda_event",
                    "title": "HIRDA event",
                    "detail": "Still visible while trace is in DB.",
                    "status": "running",
                }],
            }

    monkeypatch.setattr(main, "db", _SnapshotDb())
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemoryWithExistingTrace())
    snapshot = await main._pstack_activity_snapshot(limit=100)
    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-1")
    assert trace["status"] == "executing"
    assert any(event["phase"] == "memory_only_hirda_event" for event in snapshot["events"])


@pytest.mark.asyncio
async def test_hirda_start_and_completion_persist_across_adapter_restart(monkeypatch):
    database = _WritableSnapshotDb()
    adapter = _RecordingActivityAdapter()
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: adapter)
    pstack = {
        "trace_id": "TRACE-COGNITIVE",
        "workflow": "feature",
        "context": {
            "task_id": "COG-1",
            "managed_session_id": "MS-1",
            "workspace_key": "ws",
        },
    }
    database.rows.append({
        "created_at": "2026-09-28T11:59:00+00:00",
        "kind": "pstack.task.opened",
        "message": "Task opened",
        "data": {"trace_id": "TRACE-COGNITIVE", "gateway_session_id": "gws-cognitive"},
        "correlation_id": "TRACE-COGNITIVE",
    })
    await main._record_pstack_hirda_event(
        pstack,
        phase="hirda_execution_started",
        title="HIRDA execution started",
        detail="Runtime started.",
        status="running",
        trace_status="executing",
    )
    assert database.gateway_metadata["active_pstack_task"]["status"] == "active"
    assert database.logical_metadata["active_pstack_task"]["status"] == "active"
    await main._record_pstack_hirda_event(
        pstack,
        phase="hirda_execution_completed",
        title="HIRDA execution completed",
        detail="Execution completed.",
        status="passed",
        trace_status="completed",
    )
    assert database.gateway_metadata["active_pstack_task"]["status"] == "completed"
    assert database.logical_metadata["active_pstack_task"]["status"] == "completed"

    hirda_rows = [row for row in database.rows if row["kind"].startswith("pstack.hirda_")]
    assert [row["correlation_id"] for row in hirda_rows] == [
        "TRACE-COGNITIVE", "TRACE-COGNITIVE",
    ]
    monkeypatch.setattr(main, "_pstack_activity_adapter", lambda: _MemorylessAdapter())
    snapshot = await main._pstack_activity_snapshot(limit=100)
    trace = next(item for item in snapshot["traces"] if item["trace_id"] == "TRACE-COGNITIVE")
    phases = [event["phase"] for event in snapshot["events"] if event["trace_id"] == "TRACE-COGNITIVE" and event["phase"].startswith("hirda_")]
    assert trace["status"] == "completed"
    assert phases == ["hirda_execution_started", "hirda_execution_completed"]
    assert trace["identity"]["managed_session_id"] == "MS-1"
    database.gateway_metadata["active_pstack_task"]["status"] = "active"
    database.logical_metadata["active_pstack_task"]["status"] = "active"
    await main._record_pstack_hirda_event(
        pstack,
        phase="hirda_execution_failed",
        title="HIRDA execution failed",
        detail="Runtime failed.",
        status="failed",
        trace_status="failed",
    )
    assert database.gateway_metadata["active_pstack_task"]["status"] == "failed"
    assert database.logical_metadata["active_pstack_task"]["status"] == "failed"


class _GatewayDb:
    def __init__(self):
        self.metadata = {}
        self.logical_metadata = {}
        self.managed_metadata = {}
        self.sibling_gateways = []
        self.gateway_sessions = {}
        self.events = []

    async def get_gateway_session(self, gateway_id):
        if gateway_id in self.gateway_sessions:
            return dict(self.gateway_sessions[gateway_id])
        return {
            "id": gateway_id,
            "studio_session_id": "studio-1",
            "managed_session_id": "ms-1",
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
            "metadata": dict(self.managed_metadata),
        }

    async def update_managed_session_metadata(self, session_id, metadata):
        assert session_id == "ms-1"
        self.managed_metadata = dict(metadata)
        return await self.get_managed_session(session_id)

    async def list_gateway_sessions_for_managed_session(self, managed_session_id):
        assert managed_session_id == "ms-1"
        return [dict(item) for item in self.sibling_gateways]

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


class _GatewayHerdr:
    def __init__(self):
        self.created = []
        self.closed = []

    async def create_task_tab(self, task, *, cwd=None):
        self.created.append((dict(task), cwd))
        return {
            "status": "open",
            "owner": "pstack",
            "persistent": False,
            "workspace_id": "wH",
            "tab_id": "wH:t9",
            "root_pane_id": "wH:p9",
            "label": f"[PStack] {task['task_id']}",
        }

    async def close_task_tab(self, tab):
        self.closed.append(dict(tab))
        return {
            **tab,
            "status": "closed",
            "closed_at": "2026-10-02T10:00:00+00:00",
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
    assert gateway.integrations.calls[0][1]["workflow_hint"] == "engineering_task"
    assert "HIRDA PSTACK LIFECYCLE CONTROL" in task["workflow_directive"]["prompt"]
    assert "mcpstudio_pstack_checkpoint" in task["workflow_directive"]["prompt"]
    assert "PSTACK METHODOLOGY" in task["workflow_directive"]["prompt"]
    assert gateway.db.metadata["active_pstack_task"]["task_id"] == task["task_id"]
    assert gateway.db.logical_metadata["active_pstack_task"]["trace_id"] == "TRACE-GW"
    kinds = [item[0] for item in gateway.db.events]
    assert kinds.count("pstack.trace.event") == 2
    assert "pstack.task.opened" in kinds

    session = {
        "id": "gws-1",
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
    assert gateway.db.metadata["active_pstack_task"]["status"] == "active"


@pytest.mark.asyncio
async def test_pstack_task_owns_disposable_herdr_tab_until_done():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.herdr = _GatewayHerdr()

    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session={
            "id": "ms-1",
            "workspace_key": "mcp-studio",
            "project_path": "/work/mcp-studio",
        },
        goal="Create one HERDR tab per PStack task",
    )

    assert task is not None
    assert task["herdr_tab"]["status"] == "open"
    assert task["herdr_tab"]["tab_id"] == "wH:t9"
    assert task["herdr_tab"]["persistent"] is False
    assert gateway.herdr.created[0][1] == "/work/mcp-studio"

    task = await gateway._record_pstack_lifecycle_tool_evidence(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        tool_name="find_symbol",
        tool_class="read",
        status="passed",
        detail="Analyzed HERDR task-tab ownership.",
    )
    task = await gateway._checkpoint_active_pstack_task(
        "gws-1", phase="analyze", status="passed", evidence="Analyze complete"
    )
    task = await gateway._checkpoint_active_pstack_task(
        "gws-1", phase="plan", status="passed", evidence="Plan complete"
    )
    task = await gateway._record_pstack_lifecycle_tool_evidence(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        tool_name="replace_content",
        tool_class="write",
        status="passed",
        detail="Implemented HERDR task-tab lifecycle.",
    )
    task = await gateway._checkpoint_active_pstack_task(
        "gws-1", phase="implement", status="passed", evidence="Implement complete"
    )
    task = await gateway._record_pstack_lifecycle_tool_evidence(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        tool_name="find_symbol",
        tool_class="read",
        status="passed",
        detail="Reviewed HERDR task-tab lifecycle.",
    )
    task = await gateway._checkpoint_active_pstack_task(
        "gws-1", phase="review", status="passed", evidence="Review complete"
    )
    task = await gateway._record_pstack_lifecycle_tool_evidence(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        tool_name="pytest",
        tool_class="execute",
        status="passed",
        detail="Verified HERDR task-tab lifecycle.",
    )
    task = await gateway._checkpoint_active_pstack_task(
        "gws-1", phase="verify", status="passed", evidence="Verify complete"
    )
    task = await gateway._checkpoint_active_pstack_task(
        "gws-1", phase="done", status="passed", evidence="Done"
    )

    assert task["status"] == "completed"
    assert task["lifecycle"]["status"] == "completed"
    assert task["herdr_tab"]["status"] == "closed"
    assert len(gateway.herdr.closed) == 1
    assert gateway.herdr.closed[0]["tab_id"] == "wH:t9"
    persisted = gateway.db.metadata["active_pstack_task"]
    assert persisted["herdr_tab"]["status"] == "closed"

    kinds = [item[0] for item in gateway.db.events]
    assert "pstack.herdr_tab.opened" in kinds
    assert "pstack.herdr_tab.closed" in kinds


@pytest.mark.asyncio
async def test_gateway_pstack_failed_tool_persists_terminal_status():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session={"id": "ms-1", "workspace_key": "mcp-studio"},
        goal="Persist failed Pstack execution state",
    )
    assert task["status"] == "ready_for_hirda"
    session = {
        "id": "gws-1",
        "studio_session_id": "studio-1",
        "metadata": {"active_pstack_task": task},
    }
    await gateway._record_active_pstack_tool(
        session,
        tool_name="run_tests",
        status="running",
        detail="HIRDA dispatched tests.",
    )
    assert gateway.db.metadata["active_pstack_task"]["status"] == "active"
    await gateway._record_active_pstack_tool(
        session,
        tool_name="run_tests",
        status="failed",
        detail="Tests failed.",
    )
    assert gateway.db.metadata["active_pstack_task"]["status"] == "failed"
    assert gateway.db.logical_metadata["active_pstack_task"]["status"] == "failed"
    terminal = [item for item in gateway.db.events if item[0] == "pstack.tool_completed"][-1]
    assert terminal[2]["data"]["trace_status"] == "failed"


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
    for label in ("Task", "Pstack", "HIRDA", "Runtime / Tools", "Verify", "Done"):
        assert label in app
    assert "{label:'JEV'" not in app
    assert "JEV advisory" in app
    assert "HIRDA remains routing and action authority" in app
    assert "downstreamSeen?'skipped':'waiting'" in app
    assert "const hirdaReached=hirdaSeen||hirdaDone||toolStarted||toolDone" in app
    assert "hirdaDone?'done':(hirdaReached?'active':'waiting')" in app
    assert "lifecycleStages" in app
    assert "stage&&stage.phase" in app
    assert "pstackCanonicalState" in app
    assert "canonicalCompleted" in app
    assert "HERDR tab" in app
    assert "lifecycleRevision" in app
    assert "BLOCKED" in app
    assert "EXPIRED" in app
    assert "terminalDisposition" in app
    assert ".pstack-flow-step.skipped" in css
    assert ".pstack-jev-advisory" in css
    assert "repeat(6,minmax(88px,1fr))" in css
    assert ".pstack-flow-step" in css
    assert ".pstack-task-row" in css
    assert ".pstack-ledger-card" in css



@pytest.mark.asyncio
async def test_gateway_does_not_adopt_higher_revision_task_from_other_conversation():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.db.metadata["active_pstack_task"] = {
        "task_id": "TASK-CURRENT",
        "trace_id": "TRACE-CURRENT",
        "goal": "Current tab work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "gateway_session_id": "gws-1",
        "status": "active",
        "opened_at": 20.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "analyze",
            "revision": 1,
            "updated_at": 20.0,
            "stages": [],
        },
    }
    other_task = {
        "task_id": "TASK-OTHER",
        "trace_id": "TRACE-OTHER",
        "goal": "Other tab work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "gateway_session_id": "gws-other",
        "status": "active",
        "opened_at": 10.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "implement",
            "revision": 184,
            "updated_at": 19.0,
            "stages": [],
        },
    }
    gateway.db.managed_metadata["active_pstack_task"] = dict(other_task)
    gateway.db.sibling_gateways = [{
        "id": "gws-other",
        "studio_session_id": "studio-other",
        "managed_session_id": "ms-1",
        "status": "stale",
        "metadata": {"active_pstack_task": dict(other_task)},
    }]
    gateway.db.gateway_sessions["gws-other"] = gateway.db.sibling_gateways[0]

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
    )

    assert task is not None
    assert task["task_id"] == "TASK-CURRENT"
    assert task["trace_id"] == "TRACE-CURRENT"
    assert gateway.db.metadata["active_pstack_task"]["task_id"] == "TASK-CURRENT"
    assert gateway.db.logical_metadata["active_pstack_task"]["task_id"] == "TASK-CURRENT"


@pytest.mark.asyncio
async def test_gateway_fresh_conversation_does_not_inherit_other_tab_task():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    other_task = {
        "task_id": "TASK-OTHER",
        "trace_id": "TRACE-OTHER",
        "goal": "Other tab work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "gateway_session_id": "gws-other",
        "status": "active",
        "opened_at": 10.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "implement",
            "revision": 184,
            "updated_at": 19.0,
            "stages": [],
        },
    }
    gateway.db.managed_metadata["active_pstack_task"] = dict(other_task)
    gateway.db.sibling_gateways = [{
        "id": "gws-other",
        "studio_session_id": "studio-other",
        "managed_session_id": "ms-1",
        "status": "stale",
        "metadata": {"active_pstack_task": dict(other_task)},
    }]
    gateway.db.gateway_sessions["gws-other"] = gateway.db.sibling_gateways[0]

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
    )

    assert task is None
    assert "active_pstack_task" not in gateway.db.metadata
    assert "active_pstack_task" not in gateway.db.logical_metadata


@pytest.mark.asyncio
async def test_gateway_compares_revision_only_within_same_task_lineage():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    old_completed = {
        "task_id": "TASK-OLD",
        "trace_id": "TRACE-OLD",
        "goal": "Old work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "gateway_session_id": "gws-old",
        "status": "completed",
        "opened_at": 10.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "completed",
            "current_phase": "done",
            "revision": 184,
            "updated_at": 18.0,
            "stages": [],
        },
    }
    new_active = {
        "task_id": "TASK-NEW",
        "trace_id": "TRACE-NEW",
        "goal": "New work",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "gateway_session_id": "gws-new",
        "status": "active",
        "opened_at": 20.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "analyze",
            "revision": 1,
            "updated_at": 20.0,
            "stages": [],
        },
    }
    gateway.db.sibling_gateways = [
        {
            "id": "gws-old",
            "studio_session_id": "studio-1",
            "managed_session_id": "ms-1",
            "metadata": {"active_pstack_task": old_completed},
        },
        {
            "id": "gws-new",
            "studio_session_id": "studio-1",
            "managed_session_id": "ms-1",
            "metadata": {"active_pstack_task": new_active},
        },
    ]

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
    )

    assert task is not None
    assert task["task_id"] == "TASK-NEW"
    assert task["trace_id"] == "TRACE-NEW"

@pytest.mark.asyncio
async def test_gateway_rehydrates_pstack_task_from_managed_session_after_transport_rollover():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.db.managed_metadata["active_pstack_task"] = {
        "task_id": "TASK-MANAGED",
        "trace_id": "TRACE-MANAGED",
        "goal": "Engineering work in mcp-studio",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "gateway_session_id": "gws-old",
        "status": "active",
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "implement",
            "revision": 9,
            "updated_at": 9.0,
            "stages": [],
        },
    }
    gateway.db.gateway_sessions["gws-old"] = {
        "id": "gws-old",
        "studio_session_id": "studio-1",
        "managed_session_id": "ms-1",
        "metadata": {},
    }

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
    )

    assert task is not None
    assert task["trace_id"] == "TRACE-MANAGED"
    assert gateway.db.metadata["active_pstack_task"]["trace_id"] == "TRACE-MANAGED"
    assert gateway.db.logical_metadata["active_pstack_task"]["trace_id"] == "TRACE-MANAGED"


@pytest.mark.asyncio
async def test_gateway_recovers_pstack_task_from_sibling_transport_and_seeds_managed_session():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    gateway.db.sibling_gateways = [{
        "id": "gws-old",
        "studio_session_id": "studio-1",
        "metadata": {
            "active_pstack_task": {
                "task_id": "TASK-SIBLING",
                "trace_id": "TRACE-SIBLING",
                "goal": "Engineering work in mcp-studio",
                "workflow": "feature",
                "workspace_key": "mcp-studio",
                "managed_session_id": "ms-1",
                "status": "active",
                "lifecycle": {
                    "schema": "hirda-pstack-lifecycle-v1",
                    "workflow": "feature",
                    "status": "active",
                    "current_phase": "analyze",
                    "revision": 4,
                    "updated_at": 4.0,
                    "stages": [],
                },
            }
        },
    }]

    task = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
    )

    assert task is not None
    assert task["trace_id"] == "TRACE-SIBLING"
    assert gateway.db.managed_metadata["active_pstack_task"]["trace_id"] == "TRACE-SIBLING"
    assert gateway.db.metadata["active_pstack_task"]["trace_id"] == "TRACE-SIBLING"


@pytest.mark.asyncio
async def test_gateway_prefers_new_active_task_over_high_revision_completed_sibling():
    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _GatewayIntegrations()
    completed = {
        "task_id": "TASK-OLD-DONE",
        "trace_id": "TRACE-OLD-DONE",
        "goal": "Engineering work in mcp-studio",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "status": "completed",
        "opened_at": 10.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "completed",
            "current_phase": "done",
            "revision": 126,
            "updated_at": 126.0,
            "stages": [],
        },
    }
    active = {
        "task_id": "TASK-NEW-ACTIVE",
        "trace_id": "TRACE-NEW-ACTIVE",
        "goal": "Engineering work in mcp-studio",
        "workflow": "feature",
        "workspace_key": "mcp-studio",
        "managed_session_id": "ms-1",
        "status": "active",
        "opened_at": 200.0,
        "lifecycle": {
            "schema": "hirda-pstack-lifecycle-v1",
            "workflow": "feature",
            "status": "active",
            "current_phase": "analyze",
            "revision": 1,
            "updated_at": 200.0,
            "stages": [],
        },
    }
    gateway.db.metadata["active_pstack_task"] = active
    gateway.db.logical_metadata["active_pstack_task"] = completed
    gateway.db.managed_metadata["active_pstack_task"] = completed
    gateway.db.sibling_gateways = [
        {"id": "gws-old", "metadata": {"active_pstack_task": completed}}
    ]

    selected = await gateway._active_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
    )

    assert selected is not None
    assert selected["task_id"] == "TASK-NEW-ACTIVE"
    assert gateway.db.logical_metadata["active_pstack_task"]["task_id"] == "TASK-NEW-ACTIVE"
    assert gateway.db.managed_metadata["active_pstack_task"]["task_id"] == "TASK-NEW-ACTIVE"


@pytest.mark.asyncio
async def test_gateway_self_heals_missing_pstack_registration_before_admission():
    class _RecoveringIntegrations(_GatewayIntegrations):
        def __init__(self):
            super().__init__()
            self.available = False
            self.activations = 0

        def get(self, integration_id):
            assert integration_id == "pstack"
            if not self.available:
                raise KeyError("pstack")
            return self.registration

        def activate_trusted_plugins(self):
            self.activations += 1
            self.available = True
            return {"activated": ["pstack"]}

    gateway = object.__new__(GatewaySessionManager)
    gateway.db = _GatewayDb()
    gateway.integrations = _RecoveringIntegrations()

    task = await gateway._open_pstack_gateway_task(
        "gws-1",
        managed_session=await gateway.db.get_managed_session("ms-1"),
        goal="Engineering work in mcp-studio",
    )

    assert task is not None
    assert task["trace_id"] == "TRACE-GW"
    assert gateway.integrations.activations == 1
