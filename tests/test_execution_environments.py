import pytest

from mcp_studio.execution_environments import (
    bind_private_task_tab,
    close_execution_environment,
    plan_execution_environment,
)


def test_team_environment_is_default_and_persistent():
    plan = plan_execution_environment(workspace="/work/repo")
    assert plan["mode"] == "team"
    assert plan["status"] == "active"
    assert plan["lifecycle"] == "persistent"
    assert plan["isolation_level"] == "workspace"


def test_pstack_gets_private_disposable_task_tab_environment():
    plan = plan_execution_environment(
        workspace="/work/repo",
        managed_session={
            "id": "ms-1",
            "workspace_key": "repo",
            "metadata": {"machine_id": "openclaw"},
        },
        task_id="TASK-1",
        pstack_task=True,
    )
    assert plan["mode"] == "private"
    assert plan["status"] == "planned"
    assert plan["environment_id"] == "pstack:TASK-1"
    assert plan["machine_id"] == "openclaw"
    assert plan["physical_host_isolation"] is False

    active = bind_private_task_tab(
        plan,
        {
            "workspace_id": "wH",
            "tab_id": "wH:t9",
            "root_pane_id": "wH:p9",
        },
    )
    assert active["status"] == "active"
    assert active["binding"]["tab_id"] == "wH:t9"

    closed = close_execution_environment(active)
    assert closed["status"] == "closed"
    assert closed["lifecycle"] == "disposable"


def test_private_constraint_fails_closed_without_pstack_isolation():
    plan = plan_execution_environment(
        workspace="/work/repo",
        runtime_constraints=["computer.private"],
        pstack_task=False,
    )
    assert plan["mode"] == "private"
    assert plan["status"] == "requires_pstack"


def test_conflicting_environment_constraints_are_rejected():
    with pytest.raises(ValueError, match="constraints_conflict"):
        plan_execution_environment(
            workspace="/work/repo",
            runtime_constraints=["computer.private", "computer.team"],
        )
