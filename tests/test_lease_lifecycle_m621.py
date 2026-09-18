from pathlib import Path

import pytest

from mcp_studio.db import Database, LeaseConflict


@pytest.mark.asyncio
async def test_idle_binding_is_affinity_not_write_lease(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(2, "serena-8001")

    await db.bind_worker("worker-1", {"workspace": "/ws/demo/", "lease_mode": "write"})
    await db.bind_worker("worker-2", {"workspace": "/ws/demo", "lease_mode": "write"})

    assert await db.list_workspace_leases() == []
    assert (await db.get_worker("worker-1"))["workspace"] == "/ws/demo"
    assert (await db.get_worker("worker-2"))["workspace"] == "/ws/demo"


@pytest.mark.asyncio
async def test_manual_busy_acquires_and_idle_releases_lease(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(2, "serena-8001")
    await db.bind_worker("worker-1", {"workspace": "/ws/demo", "lease_mode": "write"})
    await db.bind_worker("worker-2", {"workspace": "/ws/demo", "lease_mode": "write"})

    await db.set_worker_state("worker-1", "busy", "one")
    leases = await db.list_workspace_leases()
    assert len(leases) == 1 and leases[0]["worker_id"] == "worker-1"

    with pytest.raises(LeaseConflict):
        await db.set_worker_state("worker-2", "busy", "two")

    await db.set_worker_state("worker-1", "idle")
    assert await db.list_workspace_leases() == []

    await db.set_worker_state("worker-2", "busy", "two")
    leases = await db.list_workspace_leases()
    assert len(leases) == 1 and leases[0]["worker_id"] == "worker-2"


@pytest.mark.asyncio
async def test_degraded_running_work_keeps_exclusive_lease(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(2, "serena-8001")
    work = await db.create_work({
        "label": "demo",
        "workspace": "/ws/demo",
        "priority": 50,
        "lease_mode": "write",
        "dispatch_mode": "herdr",
        "instruction": "hello",
        "metadata": {},
    }, "serena-8001")
    await db.assign_work(work["id"], "worker-1")
    await db.mark_worker_degraded_for_work(work["id"])

    actions = await db.reconcile_operations(300)
    assert not any(x.get("kind") == "lease_released" for x in actions)
    leases = await db.list_workspace_leases()
    assert len(leases) == 1
    assert leases[0]["worker_id"] == "worker-1"
    assert leases[0]["work_id"] == work["id"]

    # A second writer must still be fenced out while ownership is uncertain.
    work2 = await db.create_work({
        "label": "demo2",
        "workspace": "/ws/demo",
        "priority": 50,
        "lease_mode": "write",
        "dispatch_mode": "manual",
        "metadata": {},
    }, "serena-8001")
    with pytest.raises(LeaseConflict):
        await db.assign_work(work2["id"], "worker-2")


@pytest.mark.asyncio
async def test_reconcile_removes_legacy_idle_lease(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    await db.bind_worker("worker-1", {"workspace": "/ws/demo", "lease_mode": "write"})

    # Simulate a legacy pre-M6.2.1 idle lease left in the database.
    def seed():
        with db._connect() as conn:  # test-only fixture
            conn.execute(
                "INSERT INTO workspace_leases(workspace,worker_id,session_id,work_id,acquired_at,refreshed_at) VALUES(?,?,?,?,datetime('now'),datetime('now'))",
                ("/ws/demo", "worker-1", None, None),
            )
    await db._run(seed)

    actions = await db.reconcile_operations(300)
    assert any(x.get("kind") == "lease_released" for x in actions)
    assert await db.list_workspace_leases() == []


@pytest.mark.asyncio
async def test_scheduler_lease_is_tied_to_work_id_and_released_on_finish(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    work = await db.create_work({
        "label": "demo",
        "workspace": "/ws/demo",
        "priority": 50,
        "lease_mode": "write",
        "dispatch_mode": "manual",
        "metadata": {},
    }, "serena-8001")
    await db.assign_work(work["id"], "worker-1")
    leases = await db.list_workspace_leases()
    assert leases[0]["work_id"] == work["id"]
    await db.finish_work(work["id"], state="completed")
    assert await db.list_workspace_leases() == []

@pytest.mark.asyncio
async def test_active_worker_cannot_be_rebound_or_released_without_finishing_work(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    work = await db.create_work({
        "label": "demo",
        "workspace": "/ws/a",
        "priority": 50,
        "lease_mode": "write",
        "dispatch_mode": "manual",
        "metadata": {},
    }, "serena-8001")
    await db.assign_work(work["id"], "worker-1")

    with pytest.raises(ValueError):
        await db.bind_worker("worker-1", {"workspace": "/ws/b", "lease_mode": "write"})
    with pytest.raises(ValueError):
        await db.release_worker("worker-1")
    with pytest.raises(ValueError):
        await db.set_worker_state("worker-1", "idle")

    lease = (await db.list_workspace_leases())[0]
    assert lease["workspace"] == "/ws/a"
    assert lease["worker_id"] == "worker-1"
