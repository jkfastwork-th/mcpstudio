from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mcp_studio.db import Database, PStackPreDispatchBlocked
from mcp_studio.scheduler import Scheduler
from mcp_studio.execution import ExecutionSupervisor


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "dispatch-gate.sqlite"))
    await database.init()
    await database.ensure_workers(1, "serena-8001")
    return database


async def create(db, task_id=None, *, status="active", lc="active", revision=5):
    meta = {"pstack_task_id": task_id, "pstack_auto_dispatch": True} if task_id else {}
    work = await db.create_work({
        "label": "Pre-dispatch test", "workspace":"/tmp/isolated-pstack-test",
        "lease_mode":"write", "dispatch_mode":"herdr", "instruction":"No real prompt",
        "metadata":meta,
    }, "serena-8001")
    if task_id:
        await db.create_session({
            "client_id": "test-" + work["id"], "server_id": "serena-8001",
            "metadata":{"active_pstack_task":{
                "task_id":task_id, "status":status,
                "lifecycle":{"status":lc,"revision":revision,"updated_at":100.0}
            }},
        })
    return work


def scheduler(db):
    studio = SimpleNamespace(worker_server_id="serena-8001",
                             scheduler_max_queue=1000, scheduler_interval_seconds=10)
    return Scheduler(SimpleNamespace(studio=studio),db,
                     SimpleNamespace(snapshots={}),
                     SimpleNamespace(snapshot={"panes":{"panes":[]}}))


async def set_canonical(db, task_id, *, status, lifecycle_status, revision=7):
    with db._connect() as conn:
        for table in ("sessions","gateway_sessions","managed_sessions"):
            rows=conn.execute(f"SELECT id,metadata_json FROM {table}").fetchall()
            for row in rows:
                metadata=json.loads(row["metadata_json"] or "{}")
                task=metadata.get("active_pstack_task")
                if isinstance(task,dict) and task.get("task_id")==task_id:
                    task["status"]=status
                    task["lifecycle"]["status"]=lifecycle_status
                    task["lifecycle"]["revision"]=revision
                    task["lifecycle"]["updated_at"]=200.0
                    conn.execute(f"UPDATE {table} SET metadata_json=? WHERE id=?",
                                 (json.dumps(metadata),row["id"]))


@pytest.mark.asyncio
@pytest.mark.parametrize(("status","lc"), [
    ("completed","completed"),("expired","completed"),("cancelled","completed"),
])
async def test_terminal_queued_retired_even_when_no_idle_worker(db,status,lc):
    work=await create(db,"TASK-DONE",status=status,lc=lc)
    # Even if all workers are otherwise unavailable, never leave old Work.
    await db.set_worker_state("worker-1","dead")
    assert (await scheduler(db).schedule_once())["started"]==0
    assert (await db.get_work(work["id"]))["state"]=="cancelled"
    assert not await db.list_workspace_leases()
    assert (await scheduler(db).schedule_once())["started"]==0


@pytest.mark.asyncio
async def test_blocked_task_never_assigned_but_remains_recoverable(db):
    work=await create(db,"TASK-BLOCKED",status="blocked",lc="blocked")
    assert (await scheduler(db).schedule_once())["started"]==0
    item=await db.get_work(work["id"])
    assert item["state"]=="queued"
    assert item["execution_state"]=="pstack_gate_pstack_blocked"
    assert not await db.list_workspace_leases()
    await set_canonical(db,"TASK-BLOCKED",status="active",lifecycle_status="active")
    assert (await scheduler(db).schedule_once())["started"]==1


@pytest.mark.asyncio
async def test_missing_pstack_fails_closed_without_disrupting_manual_work(db):
    tagged=await db.create_work({
        "label":"orphan task", "workspace":"/tmp/isolated-pstack-test",
        "metadata":{"pstack_task_id":"TASK-UNKNOWN"},
    },"serena-8001")
    assert (await scheduler(db).schedule_once())["started"]==0
    assert (await db.get_work(tagged["id"]))["execution_state"]=="pstack_gate_pstack_missing"
    manual=await create(db)
    assert (await scheduler(db).schedule_once())["started"]==1
    assert (await db.get_work(manual["id"]))["state"]=="running"


@pytest.mark.asyncio
async def test_assignment_transaction_rejects_task_closed_after_queue_check(db):
    work=await create(db,"TASK-RACE")
    assert (await db.pstack_pre_dispatch_gate(work["id"]))["allowed"]
    await set_canonical(db,"TASK-RACE",status="completed",lifecycle_status="completed")
    with pytest.raises(PStackPreDispatchBlocked,match="pstack_terminal"):
        await db.assign_work(work["id"],"worker-1")
    assert (await db.get_work(work["id"]))["state"]=="queued"
    assert not await db.list_workspace_leases()


@pytest.mark.asyncio
async def test_dispatch_attempt_transaction_cannot_start_for_newly_blocked_pstack(db):
    work=await create(db,"TASK-DISPATCH-RACE")
    assigned=await db.assign_work(work["id"],"worker-1")
    assert assigned["state"]=="running"
    await set_canonical(db,"TASK-DISPATCH-RACE",status="blocked",lifecycle_status="blocked")
    with pytest.raises(PStackPreDispatchBlocked,match="pstack_blocked"):
        await db.update_work_execution(
            work["id"],execution_state="dispatching",increment_dispatch=True,
        )
    unchanged=await db.get_work(work["id"])
    assert unchanged["dispatch_attempts"]==0
    assert unchanged["dispatched_at"] is None


@pytest.mark.asyncio
async def test_execution_first_guard_cancels_unprompted_assigned_work(db):
    work=await create(db,"TASK-EXEC-RACE")
    await db.assign_work(work["id"],"worker-1")
    await set_canonical(db,"TASK-EXEC-RACE",status="expired",lifecycle_status="completed")
    studio=SimpleNamespace(execution_enabled=True,execution_parallelism=1,
                           resilience_test_mode=False)
    sentinel=SimpleNamespace(snapshot={"status":"healthy"},client=lambda: (_ for _ in ()).throw(AssertionError("must never prompt")))
    supervisor=ExecutionSupervisor(SimpleNamespace(studio=studio),db,sentinel,None)
    with pytest.raises(PStackPreDispatchBlocked,match="pstack_terminal"):
        await supervisor.dispatch(work["id"])
    assert (await db.get_work(work["id"]))["state"]=="cancelled"
    assert not await db.list_workspace_leases()


@pytest.mark.asyncio
async def test_late_terminal_replica_cannot_reactivate_older_task(db):
    work=await create(db,"TASK-SPLIT")
    await db.create_session({
        "client_id":"second-replica","server_id":"serena-8001",
        "metadata":{"active_pstack_task":{
            "task_id":"TASK-SPLIT", "status":"completed",
            "lifecycle":{"status":"completed","revision":1,"updated_at":1.0},
        }},
    })
    status=await db.pstack_pre_dispatch_gate(work["id"])
    assert not status["allowed"]
    assert status["reason"]=="pstack_terminal"
    with pytest.raises(PStackPreDispatchBlocked):
        await db.assign_work(work["id"],"worker-1")


@pytest.mark.asyncio
async def test_lifecycle_blocked_during_runtime_readiness_never_prompts(db):
    work=await create(db,"TASK-MID-DISPATCH")
    await db.assign_work(work["id"],"worker-1",pane="wH:p1",agent="claude")
    studio=SimpleNamespace(execution_enabled=True,execution_parallelism=1,
                           resilience_test_mode=False)
    client_calls=[]
    class Herdr:
        snapshot={"status":"healthy"}
        def tool(self,name):
            return {
                "name":"herdr_prompt_agent",
                "inputSchema":{"type":"object",
                   "properties":{"pane_id":{"type":"string"},
                                 "prompt":{"type":"string"}},
                   "required":["pane_id","prompt"]},
            }
        def client(self):
            client_calls.append("CLIENT_WAS_OPENED")
            raise AssertionError("must never submit HERDR prompt")

    supervisor=ExecutionSupervisor(SimpleNamespace(studio=studio),db,Herdr(),None)
    pane={"pane_id":"wH:p1","agent":"claude","agent_status":"idle"}
    supervisor._pane_for_work=lambda _work: pane
    async def prepare(_work,_pane):
        return _pane
    async def readiness(_work,_pane):
        await set_canonical(db,"TASK-MID-DISPATCH",status="blocked",
                            lifecycle_status="blocked",revision=8)
        return {"ready":True}
    supervisor._ensure_runtime_agent=prepare
    supervisor._probe_runtime_readiness=readiness
    with pytest.raises(PStackPreDispatchBlocked,match="pstack_blocked"):
        await supervisor.dispatch(work["id"],refresh=False)
    assert not client_calls
    final=await db.get_work(work["id"])
    assert final["state"]=="cancelled"
    assert final["dispatch_attempts"]==0
    assert not await db.list_workspace_leases()
