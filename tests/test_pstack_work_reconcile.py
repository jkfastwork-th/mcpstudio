from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mcp_studio.db import Database
from mcp_studio.gateway import GatewaySessionManager


@pytest_asyncio.fixture
async def database(tmp_path):
    db = Database(str(tmp_path / "pstack-work-reconcile.sqlite"))
    await db.init()
    return db


def gateway_with_db(db):
    manager = object.__new__(GatewaySessionManager)
    manager.db = db
    return manager


async def create_pstack_work(db, task, *, label="PStack Work"):
    return await db.create_work({
        "label": label,
        "workspace": "/isolated/pstack",
        "dispatch_mode": "herdr",
        "metadata": {"pstack_task_id": task},
    }, "serena-8001")


def task_envelope(task_id, *, status="completed", lifecycle="completed",
                  revision=4, updated=100.0):
    return {
        "task_id": task_id,
        "trace_id": "trace-" + task_id,
        "status": status,
        "lifecycle": {
            "status": lifecycle, "revision": revision, "updated_at": updated,
        },
    }


@pytest.mark.asyncio
async def test_queued_terminal_pstack_cancels_only_exact_undispatched_work(database):
    first = await create_pstack_work(database, "TASK-DONE")
    other = await create_pstack_work(database, "TASK-ACTIVE")
    gateway = gateway_with_db(database)
    result = await gateway._reconcile_terminal_pstack_work_for_task(
        task_envelope("TASK-DONE"))
    assert result == {"cancelled": [first["id"]], "uncertain": []}
    assert (await database.get_work(first["id"]))["state"] == "cancelled"
    assert (await database.get_work(other["id"]))["state"] == "queued"
    assert await gateway._reconcile_terminal_pstack_work_for_task(
        task_envelope("TASK-DONE")) == {"cancelled": [], "uncertain": []}


@pytest.mark.asyncio
async def test_dispatched_pstack_work_not_cancelled_or_unleased(database):
    dispatched = await create_pstack_work(database, "TASK-DONE")
    with database._connect() as conn:
        conn.execute(
            """UPDATE work_items SET state='running', execution_state='waiting_agent',
               dispatch_attempts=1, dispatched_at=?, worker_id=?
               WHERE id=?""",
            ("2026-10-08T10:00:00+00:00", "worker-1", dispatched["id"]),
        )
    gateway = gateway_with_db(database)
    result = await gateway._reconcile_terminal_pstack_work_for_task(
        task_envelope("TASK-DONE"))
    assert result == {"cancelled": [], "uncertain": [dispatched["id"]]}
    work = await database.get_work(dispatched["id"])
    assert work["state"] == "running"
    assert work["execution_state"] == "waiting_agent"
    assert work["worker_id"] == "worker-1"


@pytest.mark.asyncio
async def test_blocked_or_active_lifecycle_does_not_cancel(database):
    work = await create_pstack_work(database, "TASK-BLOCKED")
    gateway = gateway_with_db(database)
    for status, lifecycle in [("blocked", "blocked"), ("active", "active")]:
        assert await gateway._reconcile_terminal_pstack_work_for_task(
            task_envelope("TASK-BLOCKED", status=status, lifecycle=lifecycle)
        ) == {"cancelled": [], "uncertain": []}
    assert (await database.get_work(work["id"]))["state"] == "queued"


@pytest.mark.asyncio
async def test_startup_sweeps_old_gateway_history_without_touching_new_task(database):
    old = await create_pstack_work(database, "TASK-OLD")
    newer = await create_pstack_work(database, "TASK-NEW")
    # Logical copies prove cleanup is not tied to the *currently* active
    # managed session envelope.
    await database.create_session({
        "client_id": "completed-task-history", "server_id": "serena-8001",
        "metadata": {"active_pstack_task": task_envelope("TASK-OLD")},
    })
    await database.create_session({
        "client_id": "new-active", "server_id": "serena-8001",
        "metadata": {"active_pstack_task": task_envelope(
            "TASK-NEW", status="active", lifecycle="active")},
    })
    gateway = gateway_with_db(database)
    result = await gateway.reconcile_pstack_terminal_work()
    assert result["cancelled"] == 1
    assert result["terminal_task_ids"] == ["TASK-OLD"]
    assert (await database.get_work(old["id"]))["state"] == "cancelled"
    assert (await database.get_work(newer["id"]))["state"] == "queued"
    assert (await gateway.reconcile_pstack_terminal_work())["cancelled"] == 0


@pytest.mark.asyncio
async def test_equal_revision_conflicting_replicas_fail_closed(database):
    work = await create_pstack_work(database, "TASK-CONFLICT")
    for i, status in enumerate(("completed", "active")):
        await database.create_session({
            "client_id": f"replica-{i}", "server_id": "serena-8001",
            "metadata": {"active_pstack_task": task_envelope(
                "TASK-CONFLICT", status=status, lifecycle=status,
                revision=5, updated=100.0)},
        })
    result = await gateway_with_db(database).reconcile_pstack_terminal_work()
    assert result["cancelled"] == 0
    assert (await database.get_work(work["id"]))["state"] == "queued"


@pytest.mark.asyncio
async def test_queued_but_dispatch_marked_preserves_uncertainty(database):
    work = await create_pstack_work(database, "TASK-DONE")
    with database._connect() as conn:
        conn.execute(
            """UPDATE work_items SET dispatch_attempts=1, dispatched_at=?
               WHERE id=?""", ("2026-10-08T10:00:00+00:00", work["id"]),
        )
    result = await gateway_with_db(database)._reconcile_terminal_pstack_work_for_task(
        task_envelope("TASK-DONE"))
    assert result["cancelled"] == []
    assert result["uncertain"] == [work["id"]]
    assert (await database.get_work(work["id"]))["state"] == "queued"


@pytest.mark.asyncio
async def test_old_managed_session_copy_cannot_requeue_terminal_pstack_work():
    async def managed(*_args, **_kwargs):
        return [{
            "id": "ms-stale", "project_path": "/isolated/pstack",
            "metadata": {"active_pstack_task": task_envelope(
                "TASK-DONE", status="active", lifecycle="active",
                revision=2, updated=80.0)},
        }]

    gateway = gateway_with_db(SimpleNamespace(list_managed_sessions=managed))
    # The authoritative newer gateway lifecycle is already terminal.
    result = await gateway.reconcile_pstack_execution_work(
        skip_terminal_task_ids={"TASK-DONE"}
    )
    assert result == {"scanned": 0, "queued": 0, "updated": 0}


@pytest.mark.asyncio
async def test_late_execution_state_cannot_reopen_terminal_task(database):
    terminal = task_envelope("TASK-CLOSED", status="completed",
                             lifecycle="completed")
    active = task_envelope("TASK-STILL-ACTIVE", status="active",
                           lifecycle="active")
    await database.create_session({
        "client_id": "closed-task", "server_id": "serena-8001",
        "metadata": {"active_pstack_task": terminal},
    })
    await database.create_session({
        "client_id": "active-task", "server_id": "serena-8001",
        "metadata": {"active_pstack_task": active},
    })
    changed = await database.patch_pstack_task(
        "TASK-CLOSED", {"status": "running", "execution_state": "waiting_agent"}
    )
    assert changed == 0
    sessions = await database.list_sessions(limit=100)
    retained = next(
        x["metadata"]["active_pstack_task"]
        for x in sessions
        if x["metadata"]["active_pstack_task"]["task_id"] == "TASK-CLOSED"
    )
    assert retained == terminal
    assert await database.patch_pstack_task(
        "TASK-STILL-ACTIVE", {"status": "running"}
    ) == 1
