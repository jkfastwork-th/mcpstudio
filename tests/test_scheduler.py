from types import SimpleNamespace

import pytest

from mcp_studio.db import Database
from mcp_studio.scheduler import Scheduler


def scheduler_for(db, herdr_snapshot=None):
    settings = SimpleNamespace(
        studio=SimpleNamespace(
            worker_server_id="serena-8001",
            scheduler_interval_seconds=1.0,
            scheduler_max_queue=1000,
        )
    )
    health = SimpleNamespace(snapshots={})
    herdr = SimpleNamespace(snapshot=herdr_snapshot or {"panes": {"panes": []}})
    return Scheduler(settings, db, health, herdr)


async def submit(
    db,
    label,
    workspace,
    priority=50,
    pane=None,
    agent=None,
    metadata=None,
):
    return await db.create_work(
        {
            "label": label,
            "workspace": workspace,
            "priority": priority,
            "lease_mode": "write",
            "pane": pane,
            "agent": agent,
            "metadata": metadata or {},
        },
        "serena-8001",
    )


@pytest.mark.asyncio
async def test_scheduler_priority_assigns_highest_first(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(2, "serena-8001")
    low = await submit(db, "low", "/ws/low", priority=10)
    high = await submit(db, "high", "/ws/high", priority=90)

    result = await scheduler_for(db).schedule_once()
    assert result["started"] == 2
    assert (await db.get_work(high["id"]))["worker_id"] == "worker-1"
    assert (await db.get_work(low["id"]))["worker_id"] == "worker-2"


@pytest.mark.asyncio
async def test_scheduler_waits_for_dependency_before_assignment(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(1, "serena-8001")
    prerequisite = await submit(db, "pre", "/ws/pre", priority=10)
    dependent = await submit(
        db,
        "dependent",
        "/ws/dependent",
        priority=100,
        metadata={
            "scheduler_contract": {
                "depends_on": [prerequisite["id"]],
            }
        },
    )

    result = await scheduler_for(db).schedule_once()
    assert result["started"] == 1
    assert (await db.get_work(prerequisite["id"]))["state"] == "running"
    waiting = await db.get_work(dependent["id"])
    assert waiting["state"] == "queued"
    assert waiting["execution_state"] == "waiting_dependency"
    assert waiting["execution"]["scheduler_wait"]["reason"] == "waiting_dependency"


@pytest.mark.asyncio
async def test_scheduler_enforces_related_group_concurrency(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(2, "serena-8001")
    metadata = {
        "scheduler_contract": {
            "related_group": "social-hub",
            "max_concurrency": 1,
        }
    }
    first = await submit(db, "first", "/ws/first", priority=90, metadata=metadata)
    second = await submit(db, "second", "/ws/second", priority=80, metadata=metadata)

    result = await scheduler_for(db).schedule_once()
    assert result["started"] == 1
    assert (await db.get_work(first["id"]))["state"] == "running"
    waiting = await db.get_work(second["id"])
    assert waiting["state"] == "queued"
    assert waiting["execution_state"] == "waiting_concurrency"
    assert waiting["execution"]["scheduler_wait"]["limit"] == 1


@pytest.mark.asyncio
async def test_scheduler_jev_priority_delta_is_advisory_input(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(1, "serena-8001")
    base_high = await submit(db, "base-high", "/ws/high", priority=60)
    jev_boosted = await submit(
        db,
        "jev-boosted",
        "/ws/jev",
        priority=50,
        metadata={
            "jev_triage": {
                "priority_delta": 20,
                "related_group": "phase-4",
                "authority": "advisory_only",
            }
        },
    )

    result = await scheduler_for(db).schedule_once()
    assert result["started"] == 1
    assert (await db.get_work(jev_boosted["id"]))["state"] == "running"
    assert (await db.get_work(base_high["id"]))["state"] == "queued"


@pytest.mark.asyncio
async def test_workspace_conflict_does_not_block_other_workspace(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(2, "serena-8001")
    first = await submit(db, "A-high", "/ws/A", priority=100)
    blocked = await submit(db, "A-next", "/ws/A", priority=90)
    other = await submit(db, "B", "/ws/B", priority=10)

    result = await scheduler_for(db).schedule_once()
    assert result["started"] == 2
    assert (await db.get_work(first["id"]))["state"] == "running"
    assert (await db.get_work(blocked["id"]))["state"] == "queued"
    assert (await db.get_work(other["id"]))["state"] == "running"


@pytest.mark.asyncio
async def test_completion_releases_worker_and_next_workspace_job_runs(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(1, "serena-8001")
    first = await submit(db, "first", "/ws/A", priority=50)
    second = await submit(db, "second", "/ws/A", priority=50)
    scheduler = scheduler_for(db)

    await scheduler.schedule_once()
    first_running = await db.get_work(first["id"])
    assert first_running["state"] == "running"
    assert (await db.get_work(second["id"]))["state"] == "queued"

    await db.finish_work(first["id"], state="completed", result={"ok": True})
    await scheduler.schedule_once()
    second_running = await db.get_work(second["id"])
    assert second_running["state"] == "running"
    assert second_running["worker_id"] == "worker-1"


@pytest.mark.asyncio
async def test_scheduler_resolves_matching_herdr_pane(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.ensure_workers(1, "serena-8001")
    work = await submit(db, "earth", "/data/earth-616")
    scheduler = scheduler_for(
        db,
        {
            "panes": {
                "panes": [
                    {
                        "pane_id": "wF:p6",
                        "cwd": "/data/earth-616",
                        "foreground_cwd": "/data/earth-616",
                        "agent_status": "unknown",
                    }
                ]
            }
        },
    )
    await scheduler.schedule_once()
    item = await db.get_work(work["id"])
    assert item["pane"] == "wF:p6"
    assert item["state"] == "running"
