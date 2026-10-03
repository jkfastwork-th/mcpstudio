from __future__ import annotations

import time
from typing import Any, Iterable


EXECUTION_ENVIRONMENT_SCHEMA = "hirda-execution-environment-v1"
_PRIVATE_CONSTRAINTS = {
    "computer.private",
    "private_computer",
    "isolation.private",
    "isolation.task",
}
_TEAM_CONSTRAINTS = {
    "computer.team",
    "team_computer",
    "isolation.team",
    "isolation.workspace",
}


def _constraints(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip().casefold()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def plan_execution_environment(
    *,
    workspace: str | None,
    managed_session: dict[str, Any] | None = None,
    task_id: str | None = None,
    runtime_constraints: Iterable[Any] = (),
    pstack_task: bool = False,
) -> dict[str, Any]:
    """Choose Team vs Private Computer semantics without selecting a provider.

    Team means shared workspace/machine scope. Private means a task-owned,
    disposable HERDR tab inside that team computer. It is process/screen
    isolation, not a claim of VM/host isolation.
    """
    constraints = _constraints(runtime_constraints)
    private_required = any(item in _PRIVATE_CONSTRAINTS for item in constraints)
    team_required = any(item in _TEAM_CONSTRAINTS for item in constraints)
    if private_required and team_required:
        raise ValueError("execution_environment_constraints_conflict")

    reasons: list[str] = []
    if private_required:
        mode = "private"
        reasons.append("skill_requires_private_computer")
    elif team_required:
        mode = "team"
        reasons.append("skill_requires_team_computer")
    elif pstack_task:
        mode = "private"
        reasons.append("pstack_task_isolated_by_default")
    else:
        mode = "team"
        reasons.append("shared_workspace_default")

    managed = managed_session if isinstance(managed_session, dict) else {}
    metadata = managed.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    machine_id = str(metadata.get("machine_id") or "").strip() or None
    workspace_key = str(managed.get("workspace_key") or "").strip() or None
    managed_session_id = str(managed.get("id") or "").strip() or None
    task_key = str(task_id or "").strip() or None
    workspace_value = str(workspace or managed.get("project_path") or "").strip() or None

    private_ready = bool(pstack_task and task_key)
    if mode == "private":
        environment_id = f"pstack:{task_key}" if task_key else "private:pending"
        status = "planned" if private_ready else "requires_pstack"
        isolation_level = "task_tab" if private_ready else "unbound"
        lifecycle = "disposable"
        owner_type = "pstack_task" if private_ready else "task"
        owner_id = task_key
    else:
        stable_owner = workspace_key or workspace_value or managed_session_id or "default"
        environment_id = f"team:{stable_owner}"
        status = "active"
        isolation_level = "workspace"
        lifecycle = "persistent"
        owner_type = "workspace"
        owner_id = workspace_key or workspace_value

    return {
        "schema": EXECUTION_ENVIRONMENT_SCHEMA,
        "environment_id": environment_id,
        "mode": mode,
        "status": status,
        "host_scope": "team_computer",
        "isolation_level": isolation_level,
        "physical_host_isolation": False,
        "lifecycle": lifecycle,
        "owner": {
            "type": owner_type,
            "id": owner_id,
        },
        "workspace": workspace_value,
        "workspace_key": workspace_key,
        "managed_session_id": managed_session_id,
        "machine_id": machine_id,
        "runtime_constraints": constraints,
        "reasons": reasons,
        "planned_at": time.time(),
    }


def bind_private_task_tab(
    environment: dict[str, Any] | None,
    tab: dict[str, Any] | None,
) -> dict[str, Any]:
    current = dict(environment or {})
    tab_value = dict(tab or {})
    if str(current.get("mode") or "").casefold() != "private":
        return current
    tab_id = str(tab_value.get("tab_id") or "").strip()
    if not tab_id:
        return current
    return {
        **current,
        "status": "active",
        "isolation_level": "task_tab",
        "binding": {
            "workspace_id": tab_value.get("workspace_id"),
            "tab_id": tab_id,
            "root_pane_id": tab_value.get("root_pane_id"),
        },
        "activated_at": time.time(),
    }


def close_execution_environment(
    environment: dict[str, Any] | None,
    tab: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = dict(environment or {})
    if not current:
        return current
    if str(current.get("lifecycle") or "").casefold() != "disposable":
        return current
    tab_value = dict(tab or {})
    binding = dict(current.get("binding") or {})
    if tab_value:
        binding.update(
            {
                key: value
                for key, value in {
                    "workspace_id": tab_value.get("workspace_id"),
                    "tab_id": tab_value.get("tab_id"),
                    "root_pane_id": tab_value.get("root_pane_id"),
                }.items()
                if value
            }
        )
    return {
        **current,
        "status": "closed",
        "binding": binding,
        "closed_at": time.time(),
    }
