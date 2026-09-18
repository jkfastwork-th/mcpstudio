import pytest

from mcp_studio.db import Database, LeaseConflict


@pytest.mark.asyncio
async def test_worker_pool_and_write_lease_conflict(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(4, "serena-8001")

    workers = await db.list_workers()
    assert [w["id"] for w in workers] == ["worker-1", "worker-2", "worker-3", "worker-4"]
    assert all(w["state"] == "idle" for w in workers)

    w1 = await db.bind_worker(
        "worker-1",
        {"workspace": "/data/earth-616", "pane": "wF:p6", "agent": "claude", "lease_mode": "write"},
    )
    assert w1["workspace"] == "/data/earth-616"
    assert w1["lease_mode"] == "write"
    assert await db.list_workspace_leases() == []

    # M6.2.1: idle binding is affinity only. Exclusivity begins when work starts.
    w2 = await db.bind_worker(
        "worker-2",
        {"workspace": "/data/earth-616", "pane": "wF:p7", "agent": "claude", "lease_mode": "write"},
    )
    assert w2["workspace"] == "/data/earth-616"
    await db.set_worker_state("worker-1", "busy", "test")
    with pytest.raises(LeaseConflict):
        await db.set_worker_state("worker-2", "busy", "test")

    await db.set_worker_state("worker-1", "idle")
    assert await db.list_workspace_leases() == []
    await db.set_worker_state("worker-2", "busy", "test")
    assert (await db.list_workspace_leases())[0]["worker_id"] == "worker-2"


@pytest.mark.asyncio
async def test_busy_worker_requires_binding(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(1, "serena-8001")

    with pytest.raises(ValueError):
        await db.set_worker_state("worker-1", "busy", "test")

    await db.bind_worker("worker-1", {"workspace": "/tmp/demo", "lease_mode": "write"})
    worker = await db.set_worker_state("worker-1", "busy", "test")
    assert worker["state"] == "busy"
    assert worker["work_label"] == "test"
