from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.db import Database
from mcp_studio.operations import OperationsManager


async def _work(db: Database, *, dispatch_mode="herdr"):
    item = await db.create_work(
        {
            "label": "ops-test",
            "workspace": "/ws/ops",
            "priority": 50,
            "lease_mode": "write",
            "dispatch_mode": dispatch_mode,
            "instruction": "hello" if dispatch_mode == "herdr" else None,
            "metadata": {},
        },
        "serena-8001",
    )
    return item


@pytest.mark.asyncio
async def test_cancel_before_dispatch_is_terminal_and_releases_worker(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    work = await _work(db)
    await db.assign_work(work["id"], "worker-1")

    item, disposition = await db.request_cancel_work(work["id"])
    assert disposition == "cancelled"
    assert item["state"] == "cancelled"
    worker = await db.get_worker("worker-1")
    assert worker["state"] == "idle"
    assert worker["workspace"] is None
    assert await db.list_workspace_leases() == []


@pytest.mark.asyncio
async def test_cancel_after_dispatch_becomes_pending_not_false_cancel(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    work = await _work(db)
    await db.assign_work(work["id"], "worker-1")
    await db.update_work_execution(
        work["id"], execution_state="waiting_agent", increment_dispatch=True, mark_dispatched=True
    )

    item, disposition = await db.request_cancel_work(work["id"])
    assert disposition == "cancel_pending"
    assert item["state"] == "running"
    assert item["execution_state"] == "cancel_pending"
    assert item["cancel_requested_at"]
    worker = await db.get_worker("worker-1")
    assert worker["state"] == "busy"
    assert len(await db.list_workspace_leases()) == 1


@pytest.mark.asyncio
async def test_cancel_pending_timeout_detaches_and_releases_local_ownership(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    work = await _work(db)
    await db.assign_work(work["id"], "worker-1")
    await db.update_work_execution(
        work["id"], execution_state="waiting_agent", increment_dispatch=True, mark_dispatched=True
    )
    await db.request_cancel_work(work["id"])

    actions = await db.reconcile_operations(0)
    assert any(x["kind"] == "work_detached" for x in actions)
    item = await db.get_work(work["id"])
    assert item["state"] == "detached"
    assert item["execution_state"] == "detached"
    assert item["detached_reason"] == "cancel_pending_timeout"
    assert (await db.get_worker("worker-1"))["state"] == "idle"
    assert await db.list_workspace_leases() == []


@pytest.mark.asyncio
async def test_manual_detach_is_explicit_and_preserves_reason(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    work = await _work(db)
    await db.assign_work(work["id"], "worker-1")
    item = await db.detach_work(work["id"], reason="operator_review")
    assert item["state"] == "detached"
    assert item["detached_reason"] == "operator_review"
    assert (await db.get_worker("worker-1"))["state"] == "idle"


@pytest.mark.asyncio
async def test_audit_alert_and_schema_migration_tables(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    schema = await db.schema_status()
    assert schema["current_version"] >= 2
    assert schema["integrity"] == "ok"

    await db.add_audit("test.action", actor="tester", target_type="work", target_id="w1", data={"x": 1})
    rows = await db.list_audit()
    assert rows[0]["action"] == "test.action"
    assert rows[0]["data"] == {"x": 1}

    alert = await db.open_alert("test:one", "test.kind", "test alert")
    assert alert["status"] == "open"
    ack = await db.acknowledge_alert(alert["id"], "tester", "seen")
    assert ack["status"] == "acknowledged"
    assert ack["data"]["ack_note"] == "seen"


@pytest.mark.asyncio
async def test_operations_manager_reconciles_orphan_busy_worker(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    await db.bind_worker("worker-1", {"workspace": "/ws/orphan", "lease_mode": "write", "metadata": {}})
    await db.set_worker_state("worker-1", "busy", work_label="orphan")
    settings = SimpleNamespace(studio=SimpleNamespace(
        operations_enabled=True,
        operations_interval_seconds=15.0,
        cancel_pending_detach_seconds=300,
        operations_ephemera_retention_days=7,
        audit_retention_days=90,
        operations_alerts_enabled=True,
    ))
    ops = OperationsManager(settings, db)
    result = await ops.run_once(force_cleanup=True)
    assert result["status"] == "healthy"
    assert (await db.get_worker("worker-1"))["state"] == "idle"
