import asyncio
from types import SimpleNamespace

import pytest

from mcp_studio.db import Database
from mcp_studio.execution import (
    ExecutionSupervisor,
    ToolSchemaError,
    build_tool_arguments,
    classify_failure,
)


PROMPT_TOOL = {
    "name": "herdr_prompt_agent",
    "inputSchema": {
        "type": "object",
        "properties": {
            "pane_id": {"type": "string"},
            "prompt": {"type": "string"},
        },
        "required": ["pane_id", "prompt"],
    },
}

READ_TOOL = {
    "name": "herdr_read_agent",
    "inputSchema": {
        "type": "object",
        "properties": {"pane_id": {"type": "string"}},
        "required": ["pane_id"],
    },
}


def studio_settings(*, automatic=False, read_result=False):
    return SimpleNamespace(
        studio=SimpleNamespace(
            worker_server_id="serena-8001",
            request_timeout_seconds=2,
            execution_enabled=automatic,
            execution_interval_seconds=1.0,
            execution_stall_seconds=60,
            execution_max_recoveries=6,
            execution_read_result=read_result,
            execution_safe_redispatch=False,
            execution_parallelism=4,
            resilience_test_mode=False,
        )
    )


class FakeClient:
    def __init__(self, calls):
        self.calls = calls

    async def call_tool(self, name, arguments, client_name="test"):
        self.calls.append((name, arguments, client_name))
        if name == "herdr_prompt_agent":
            return {"content": [{"type": "text", "text": '{"accepted":true}'}]}
        if name == "herdr_read_agent":
            return {"content": [{"type": "text", "text": "M3_CERT_OK"}]}
        return {}


class FakeHerdr:
    def __init__(self):
        self.calls = []
        self.snapshot = {
            "status": "healthy",
            "panes": {
                "panes": [
                    {
                        "pane_id": "wF:p1",
                        "cwd": "/ws/demo",
                        "foreground_cwd": "/ws/demo",
                        "agent": "claude",
                        "agent_status": "idle",
                        "revision": 10,
                        "state_change_seq": 20,
                        "agent_session": {"value": "agent-session-1"},
                    }
                ]
            },
        }
        self.tools = {
            "herdr_prompt_agent": PROMPT_TOOL,
            "herdr_read_agent": READ_TOOL,
        }

    async def refresh(self):
        return self.snapshot

    def tool(self, name):
        return self.tools.get(name)

    def client(self):
        return FakeClient(self.calls)

    def find_pane(self, *, pane_id=None, workspace=None, agent=None):
        panes = self.snapshot["panes"]["panes"]
        if pane_id:
            for pane in panes:
                if pane["pane_id"] == pane_id:
                    return pane
        for pane in panes:
            if workspace in {pane.get("cwd"), pane.get("foreground_cwd")}:
                return pane
        return None


class FakeScheduler:
    def __init__(self):
        self.kicks = 0

    def kick(self):
        self.kicks += 1


async def create_running_herdr_work(db):
    await db.ensure_workers(1, "serena-8001")
    work = await db.create_work(
        {
            "label": "M3 test",
            "workspace": "/ws/demo",
            "priority": 50,
            "lease_mode": "write",
            "pane": "wF:p1",
            "agent": "claude",
            "dispatch_mode": "herdr",
            "instruction": "Reply M3_CERT_OK only",
            "metadata": {},
        },
        "serena-8001",
    )
    return await db.assign_work(work["id"], "worker-1", pane="wF:p1", agent="claude")


def test_schema_driven_prompt_mapping():
    pane = {"pane_id": "wF:p1", "agent": "claude", "agent_session": {"value": "abc"}}
    args = build_tool_arguments(PROMPT_TOOL, pane=pane, pane_id="wF:p1", agent="claude", prompt="hello")
    assert args == {"pane_id": "wF:p1", "prompt": "hello"}


def test_unknown_required_field_fails_closed():
    tool = {
        "name": "herdr_prompt_agent",
        "inputSchema": {
            "type": "object",
            "properties": {"mystery": {"type": "string"}},
            "required": ["mystery"],
        },
    }
    with pytest.raises(ToolSchemaError):
        build_tool_arguments(tool, pane={"pane_id": "p1"}, pane_id="p1", agent="claude", prompt="hello")


def test_timeout_classified_ambiguous():
    import httpx

    assert classify_failure(httpx.ReadTimeout("timeout")) == "dispatch_uncertain"


@pytest.mark.asyncio
async def test_dispatch_then_monitor_completion_releases_worker(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    running = await create_running_herdr_work(db)
    herdr = FakeHerdr()
    scheduler = FakeScheduler()
    supervisor = ExecutionSupervisor(studio_settings(read_result=True), db, herdr, scheduler)

    dispatched = await supervisor.dispatch(running["id"], explicit=True)
    assert dispatched["execution_state"] == "waiting_agent"
    assert dispatched["dispatch_attempts"] == 1
    assert herdr.calls[0][0] == "herdr_prompt_agent"

    # Agent has changed revision and returned to idle: this proves activity
    # happened after the dispatch and is safe to treat as terminal.
    pane = herdr.snapshot["panes"]["panes"][0]
    pane["revision"] = 11
    pane["state_change_seq"] = 21
    pane["agent_status"] = "idle"

    await supervisor.run_once()
    finished = await db.get_work(running["id"])
    assert finished["state"] == "completed"
    assert finished["execution_state"] == "completed"
    worker = await db.get_worker("worker-1")
    assert worker["state"] == "idle"
    assert worker["workspace"] is None
    assert scheduler.kicks == 1


@pytest.mark.asyncio
async def test_recovery_rebinds_without_prompt_redispatch(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    running = await create_running_herdr_work(db)
    herdr = FakeHerdr()
    scheduler = FakeScheduler()
    supervisor = ExecutionSupervisor(studio_settings(), db, herdr, scheduler)

    await supervisor.dispatch(running["id"], explicit=True)
    prompt_calls = len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"])

    await db.update_work_execution(
        running["id"], execution_state="reconnecting", failure_class="pane_missing"
    )
    await supervisor.recover_work(running["id"])

    recovered = await db.get_work(running["id"])
    assert recovered["execution_state"] == "waiting_agent"
    assert recovered["recovery_count"] == 1
    assert len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"]) == prompt_calls

@pytest.mark.asyncio
async def test_pre_dispatch_recovery_returns_to_assigned_without_sending_prompt(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    running = await create_running_herdr_work(db)
    herdr = FakeHerdr()
    scheduler = FakeScheduler()
    supervisor = ExecutionSupervisor(studio_settings(), db, herdr, scheduler)

    await db.update_work_execution(
        running["id"], execution_state="reconnecting", failure_class="pane_missing"
    )
    await supervisor.recover_work(running["id"])
    recovered = await db.get_work(running["id"])
    assert recovered["execution_state"] == "assigned"
    assert recovered["dispatch_attempts"] == 0
    assert not herdr.calls


@pytest.mark.asyncio
async def test_uncertain_redispatch_requires_acknowledgement(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    running = await create_running_herdr_work(db)
    herdr = FakeHerdr()
    scheduler = FakeScheduler()
    supervisor = ExecutionSupervisor(studio_settings(), db, herdr, scheduler)

    await db.update_work_execution(
        running["id"], execution_state="dispatch_uncertain", failure_class="dispatch_uncertain"
    )
    with pytest.raises(ValueError, match="acknowledge_duplicate_risk"):
        await supervisor.retry_dispatch(running["id"], acknowledge_duplicate_risk=False)

REAL_HERDR_PROMPT_TOOL = {
    "name": "herdr_prompt_agent",
    "inputSchema": {
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Unique Herdr agent name or pane ID.",
            },
            "message": {"type": "string", "description": "Prompt text passed literally to Herdr."},
            "wait": {"default": False, "type": "boolean"},
            "timeout_ms": {"default": 30000, "type": "number"},
        },
        "required": ["agent_id", "message"],
        "type": "object",
    },
}

REAL_HERDR_READ_TOOL = {
    "name": "herdr_read_agent",
    "inputSchema": {
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Unique Herdr agent name or pane ID.",
            },
            "lines": {"default": 100, "type": "number"},
        },
        "required": ["agent_id"],
        "type": "object",
    },
}

REAL_HERDR_WAIT_TOOL = {
    "name": "herdr_wait_agent",
    "inputSchema": {
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Unique Herdr agent name or pane ID.",
            },
            "until": {"default": "", "type": "string"},
            "timeout_ms": {"default": 30000, "type": "number"},
        },
        "required": ["agent_id"],
        "type": "object",
    },
}


def test_live_herdr_prompt_schema_uses_pane_id_for_agent_id_without_session():
    # Hermes panes may not expose agent_session. The Herdr contract explicitly
    # permits a pane ID, so Studio must not require an agent-session UUID.
    pane = {"pane_id": "w1:p7", "agent": "hermes"}
    args = build_tool_arguments(
        REAL_HERDR_PROMPT_TOOL,
        pane=pane,
        pane_id="w1:p7",
        agent="hermes",
        prompt="M3_CERT_OK",
    )
    assert args == {"agent_id": "w1:p7", "message": "M3_CERT_OK"}


def test_live_herdr_prompt_schema_prefers_pane_over_session_uuid():
    pane = {
        "pane_id": "wF:p1",
        "agent": "claude",
        "agent_session": {"value": "5f35c41d-6791-4c25-9d85-baa6197423b5"},
    }
    args = build_tool_arguments(
        REAL_HERDR_PROMPT_TOOL,
        pane=pane,
        pane_id="wF:p1",
        agent="claude",
        prompt="hello",
    )
    assert args["agent_id"] == "wF:p1"
    assert args["message"] == "hello"


def test_live_herdr_read_and_wait_schema_mapping():
    pane = {"pane_id": "w1:p7", "agent": "hermes"}
    read_args = build_tool_arguments(
        REAL_HERDR_READ_TOOL,
        pane=pane,
        pane_id="w1:p7",
        agent="hermes",
    )
    wait_args = build_tool_arguments(
        REAL_HERDR_WAIT_TOOL,
        pane=pane,
        pane_id="w1:p7",
        agent="hermes",
        timeout_seconds=30,
    )
    assert read_args == {"agent_id": "w1:p7"}
    assert wait_args == {"agent_id": "w1:p7", "timeout_ms": 30000}


class SlowFakeClient(FakeClient):
    async def call_tool(self, name, arguments, client_name="test"):
        if name == "herdr_prompt_agent":
            await asyncio.sleep(0.05)
        return await super().call_tool(name, arguments, client_name)


class MultiPaneHerdr(FakeHerdr):
    def __init__(self, count=4, slow=False):
        super().__init__()
        self.slow = slow
        panes = []
        for i in range(1, count + 1):
            panes.append({
                "pane_id": f"p{i}",
                "cwd": f"/ws/{i}",
                "foreground_cwd": f"/ws/{i}",
                "agent": "claude",
                "agent_status": "idle",
                "revision": 10,
                "state_change_seq": 20,
                "agent_session": {"value": f"session-{i}"},
            })
        self.snapshot["panes"] = {"panes": panes}

    def client(self):
        cls = SlowFakeClient if self.slow else FakeClient
        return cls(self.calls)


def m4_settings(*, automatic=True, resilience=False):
    value = studio_settings(automatic=automatic, read_result=False)
    value.studio.execution_parallelism = 4
    value.studio.resilience_test_mode = resilience
    return value


@pytest.mark.asyncio
async def test_m4_parallel_dispatch_reaches_multiple_inflight_calls(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(4, "serena-8001")
    herdr = MultiPaneHerdr(4, slow=True)
    scheduler = FakeScheduler()

    for i in range(1, 5):
        work = await db.create_work(
            {
                "label": f"parallel-{i}",
                "workspace": f"/ws/{i}",
                "priority": 50,
                "lease_mode": "write",
                "pane": f"p{i}",
                "agent": "claude",
                "dispatch_mode": "herdr",
                "instruction": f"reply {i}",
                "metadata": {},
            },
            "serena-8001",
        )
        await db.assign_work(work["id"], f"worker-{i}", pane=f"p{i}", agent="claude")

    supervisor = ExecutionSupervisor(m4_settings(), db, herdr, scheduler)
    await supervisor.run_once()
    assert supervisor.snapshot["peak_parallel_dispatches"] >= 2
    running = await db.running_work()
    assert len(running) == 4
    assert all(w["execution_state"] == "waiting_agent" for w in running)
    assert len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"]) == 4


@pytest.mark.asyncio
async def test_m4_fault_isolation_recovers_without_redispatch(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    running = await create_running_herdr_work(db)
    herdr = FakeHerdr()
    scheduler = FakeScheduler()
    settings = m4_settings(automatic=False, resilience=True)
    supervisor = ExecutionSupervisor(settings, db, herdr, scheduler)

    await supervisor.dispatch(running["id"], explicit=True)
    prompt_calls = len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"])
    await supervisor.inject_fault(running["id"], kind="pane_missing", ticks=1, note="test")
    await supervisor.run_once()
    faulted = await db.get_work(running["id"])
    assert faulted["execution_state"] == "reconnecting"
    assert (await db.get_worker("worker-1"))["state"] == "degraded"

    await supervisor.run_once()
    recovered = await db.get_work(running["id"])
    assert recovered["execution_state"] == "waiting_agent"
    assert recovered["recovery_count"] == 1
    assert (await db.get_worker("worker-1"))["state"] == "busy"
    assert len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"]) == prompt_calls


@pytest.mark.asyncio
async def test_m4_runtime_metrics_track_worker_success_and_latency(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(1, "serena-8001")
    work = await db.create_work(
        {
            "label": "metrics", "workspace": "/ws/metrics", "priority": 50,
            "lease_mode": "write", "metadata": {},
        },
        "serena-8001",
    )
    await db.assign_work(work["id"], "worker-1")
    await db.finish_work(work["id"], state="completed", result={"ok": True})
    metrics = await db.runtime_metrics()
    assert metrics["completed"] == 1
    assert metrics["success_rate"] == 100.0
    assert metrics["per_worker"][0]["worker_id"] == "worker-1"
    assert metrics["per_worker"][0]["completed"] == 1

@pytest.mark.asyncio
async def test_m4_post_dispatch_fault_armed_before_dispatch_is_not_consumed_early(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    running = await create_running_herdr_work(db)
    herdr = FakeHerdr()
    scheduler = FakeScheduler()
    settings = m4_settings(automatic=False, resilience=True)
    supervisor = ExecutionSupervisor(settings, db, herdr, scheduler)

    armed = await supervisor.inject_fault(
        running["id"],
        kind="pane_missing",
        ticks=1,
        note="armed before dispatch",
        stage="post_dispatch",
    )
    assert armed["stage"] == "post_dispatch"

    # execution_enabled=false and a post-dispatch fault must stay armed while
    # the work has never been sent to Herdr.
    await supervisor.run_once()
    before = await db.get_work(running["id"])
    assert before["execution_state"] == "assigned"
    assert before["dispatch_attempts"] == 0

    dispatched = await supervisor.dispatch(running["id"], explicit=True)
    assert dispatched["execution_state"] == "waiting_agent"
    assert dispatched["dispatch_attempts"] == 1
    prompt_calls = len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"])
    assert prompt_calls == 1

    # Now the armed fault is eligible and simulates a transient loss after
    # Herdr already accepted the prompt.
    await supervisor.run_once()
    faulted = await db.get_work(running["id"])
    assert faulted["execution_state"] == "reconnecting"
    assert faulted["dispatch_attempts"] == 1

    await supervisor.run_once()
    recovered = await db.get_work(running["id"])
    assert recovered["execution_state"] == "waiting_agent"
    assert recovered["recovery_count"] == 1
    assert recovered["dispatch_attempts"] == 1
    assert len([c for c in herdr.calls if c[0] == "herdr_prompt_agent"]) == prompt_calls

@pytest.mark.asyncio
async def test_m4_resilience_test_mode_uses_manual_execution_ticks(tmp_path):
    """Certification mode must not start or wake a background execution loop."""
    settings = studio_settings()
    settings.studio.resilience_test_mode = True
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(4, settings.studio.worker_server_id)
    scheduler = FakeScheduler()
    herdr = FakeHerdr()
    supervisor = ExecutionSupervisor(settings, db, scheduler, herdr)

    await supervisor.start()
    assert supervisor._task is None
    assert supervisor.snapshot["manual_tick_mode"] is True
    assert supervisor.snapshot["status"] == "certification_manual"

    # kick() must not create or wake a background task in certification mode.
    supervisor.kick()
    assert supervisor._task is None
    await supervisor.stop()
