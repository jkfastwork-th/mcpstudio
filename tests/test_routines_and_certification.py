from types import SimpleNamespace

import pytest

from mcp_studio.certification import certify_ai_quality, certify_execution
from mcp_studio.db import Database
from mcp_studio.routines import RoutineManager


class _Router:
    async def bootstrap_runtime(self, request):
        assert request["allow_fallback"] is True
        assert request["context"]["workflow_provider"] == "pstack"
        return {
            "runtime": "codex",
            "lane": "deep",
            "candidates": ["codex", "claude", "hermes"],
            "semantic_routing": {
                "schema": "hirda-semantic-runtime-ranking-v1",
                "applied": True,
            },
            "supervisor_policy": {
                "schema": "hirda-supervisor-policy-v1",
                "applied": True,
            },
        }


class _Kick:
    def __init__(self):
        self.kicks = 0

    def kick(self):
        self.kicks += 1


def _settings():
    return SimpleNamespace(
        studio=SimpleNamespace(worker_server_id="serena-8001")
    )


@pytest.mark.asyncio
async def test_routine_definition_and_run_persist_pstack_routing_lineage(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    scheduler = _Kick()
    execution = _Kick()
    manager = RoutineManager(
        _settings(),
        db,
        cognitive_router=_Router(),
        scheduler=scheduler,
        execution=execution,
    )

    routine = await manager.create(
        {
            "name": "Daily certification",
            "enabled": True,
            "cadence": "manual",
            "interval_seconds": None,
            "work_template": {
                "label": "routine smoke",
                "workspace": "/ws/demo",
                "priority": 60,
                "lease_mode": "write",
                "dispatch_mode": "herdr",
                "instruction": "Reply ROUTINE_OK",
                "metadata": {
                    "capability_resolution": {
                        "schema": "hirda-capability-resolution-v1",
                        "status": "ready",
                    }
                },
            },
            "metadata": {"owner": "test"},
        }
    )

    assert routine["id"].startswith("routine-")
    assert routine["enabled"] is True
    assert routine["cadence"] == "manual"

    triggered = await manager.trigger(routine["id"])
    run = triggered["run"]
    work = triggered["work"]
    metadata = work["metadata"]

    assert run["status"] == "running"
    assert run["work_id"] == work["id"]
    assert run["pstack_task_id"].startswith("TASK-R-")
    assert metadata["routine_id"] == routine["id"]
    assert metadata["routine_run_id"] == run["id"]
    assert metadata["routine_pstack_task_id"] == run["pstack_task_id"]
    assert metadata["runtime_selection"]["runtime"] == "codex"
    assert metadata["orchestration_contract"]["flow"] == [
        "routine",
        "pstack",
        "jev_triage",
        "semantic_runtime_ranking",
        "supervisor_policy",
        "capability_resolution",
        "scheduler",
        "execution",
    ]
    assert work["requested_agent"] == "codex"
    assert scheduler.kicks == 1
    assert execution.kicks == 1


@pytest.mark.asyncio
async def test_routine_propagates_scheduler_and_jev_advisory_contracts(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    manager = RoutineManager(
        _settings(),
        db,
        cognitive_router=_Router(),
        scheduler=_Kick(),
        execution=_Kick(),
    )

    routine = await manager.create(
        {
            "name": "Grouped routine",
            "enabled": True,
            "cadence": "manual",
            "interval_seconds": None,
            "work_template": {
                "label": "grouped work",
                "workspace": "/ws/grouped",
                "priority": 50,
                "lease_mode": "write",
                "dispatch_mode": "herdr",
                "instruction": "Reply OK",
                "metadata": {},
            },
            "metadata": {
                "scheduler_contract": {
                    "related_group": "social-hub",
                    "max_concurrency": 1,
                },
                "jev_triage": {
                    "priority_delta": 15,
                    "related_group": "social-hub",
                    "authority": "advisory_only",
                },
            },
        }
    )

    triggered = await manager.trigger(routine["id"])
    metadata = triggered["work"]["metadata"]
    assert metadata["scheduler_contract"]["related_group"] == "social-hub"
    assert metadata["scheduler_contract"]["max_concurrency"] == 1
    assert metadata["jev_triage"]["priority_delta"] == 15
    assert metadata["jev_triage"]["authority"] == "advisory_only"


@pytest.mark.asyncio
async def test_interval_routine_has_durable_next_run_and_no_immediate_duplicate(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    manager = RoutineManager(_settings(), db, cognitive_router=_Router())

    routine = await manager.create(
        {
            "name": "Interval",
            "enabled": True,
            "cadence": "interval",
            "interval_seconds": 60,
            "work_template": {
                "label": "interval work",
                "workspace": "/ws/demo",
                "priority": 50,
                "lease_mode": "read",
                "dispatch_mode": "manual",
                "instruction": None,
                "metadata": {},
            },
            "metadata": {},
        }
    )

    assert routine["next_run_at"] is not None
    due = await db.due_routines()
    assert due == []


@pytest.mark.asyncio
async def test_routine_reconcile_records_terminal_work_evidence(tmp_path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    manager = RoutineManager(_settings(), db, cognitive_router=_Router())
    routine = await manager.create(
        {
            "name": "Reconcile",
            "enabled": True,
            "cadence": "manual",
            "work_template": {
                "label": "run",
                "workspace": "/ws/demo",
                "priority": 50,
                "lease_mode": "write",
                "dispatch_mode": "manual",
                "metadata": {},
            },
            "metadata": {},
        }
    )
    triggered = await manager.trigger(routine["id"])
    work = triggered["work"]

    await db.ensure_workers(1, "serena-8001")
    running = await db.assign_work(work["id"], "worker-1")
    await db.finish_work(
        running["id"],
        state="completed",
        result={
            "semantic_completion": {"status": "success"},
        },
    )

    result = await manager.reconcile_runs()
    assert result["completed"] == 1
    run = await db.get_routine_run(triggered["run"]["id"])
    assert run["status"] == "completed"
    assert run["evidence"]["work_state"] == "completed"


def _successful_work():
    return {
        "id": "work-ok",
        "state": "completed",
        "execution_state": "completed",
        "failure_class": None,
        "error": None,
        "execution": {
            "runtime_readiness": {
                "ready": True,
                "model": "cx/gpt-6-luna[1m]",
            }
        },
        "result": {
            "runtime_readiness": {"ready": True},
            "semantic_completion": {
                "status": "success",
                "marker": "success",
            },
        },
    }


def test_execution_certification_is_deterministic_and_quality_free():
    result = certify_execution(_successful_work())

    assert result["passed"] is True
    assert result["outcome"] == "PASS"
    assert result["deterministic"] is True
    assert result["quality_judgement_included"] is False
    assert all(check["passed"] for check in result["checks"])


def test_execution_certification_requires_closed_pstack_resources():
    task = {
        "status": "completed",
        "lifecycle": {
            "status": "completed",
            "current_phase": "terminal",
        },
        "herdr_tab": {
            "status": "open",
            "persistent": False,
        },
        "execution_environment": {
            "status": "active",
            "lifecycle": "disposable",
        },
    }

    result = certify_execution(_successful_work(), pstack_task=task)

    assert result["passed"] is False
    failed = {
        item["name"]
        for item in result["checks"]
        if not item["passed"]
    }
    assert "disposable_tab_closed" in failed
    assert "execution_environment_closed" in failed


def test_ai_quality_certification_never_mutates_execution_semantics():
    work = _successful_work()
    execution = certify_execution(work)
    quality = certify_ai_quality(
        work,
        {
            "score": 0.2,
            "passed": False,
            "evaluator": "human",
            "notes": "Answer quality needs work.",
        },
        execution_certification=execution,
    )

    assert execution["passed"] is True
    assert quality["outcome"] == "FAIL"
    assert quality["execution_certification"]["outcome"] == "PASS"
    assert quality["execution_state_mutated"] is False


def test_ai_quality_is_ineligible_when_execution_failed():
    work = _successful_work()
    work["execution_state"] = "failed"
    execution = certify_execution(work)
    quality = certify_ai_quality(
        work,
        {
            "score": 1.0,
            "passed": True,
            "evaluator": "judge",
        },
        execution_certification=execution,
    )

    assert execution["passed"] is False
    assert quality["outcome"] == "INELIGIBLE"
    assert quality["passed"] is False
