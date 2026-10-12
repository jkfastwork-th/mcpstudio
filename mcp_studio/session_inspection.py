"""Read-only Session → Herdr/worker view.

This is observation, never an authorization or lease source. The managed
session's principal is authorized by the caller *before* this projection runs.
"""
from __future__ import annotations

from typing import Any


def _rows(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get(key)
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def inspect_session_bindings(
    session: dict[str, Any],
    *,
    gateways: list[dict[str, Any]],
    workers: list[dict[str, Any]],
    leases: list[dict[str, Any]],
    herdr_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve only exact owner references; never infer ownership from CWD."""
    snapshot = herdr_snapshot if isinstance(herdr_snapshot, dict) else {}
    status = str(snapshot.get("status") or "unknown")
    observed_at = snapshot.get("last_refreshed_at")
    panes = _rows(snapshot.get("panes"), "panes")
    agents = _rows(snapshot.get("agents"), "agents")
    panes_by_id = {str(p.get("pane_id")): p for p in panes if p.get("pane_id")}
    agents_by_pane = {
        str(a.get("pane_id")): a for a in agents if a.get("pane_id")
    }
    logical_ids = {
        str(g["studio_session_id"])
        for g in gateways
        if g.get("managed_session_id") == session.get("id")
        and g.get("studio_session_id")
    }
    workspace_key = str(session.get("workspace_key") or "")

    def observation(
        pane_id: str | None,
        *, tab_id: str | None = None, herdr_workspace_id: str | None = None
    ) -> dict[str, Any]:
        pane = panes_by_id.get(str(pane_id)) if pane_id else None
        agent = agents_by_pane.get(str(pane_id)) if pane_id else None
        # A pane's ID alone cannot establish ownership of a recycled tab.
        exact = bool(pane) and (
            not tab_id or pane.get("tab_id") == tab_id
        ) and (
            not herdr_workspace_id or pane.get("workspace_id") == herdr_workspace_id
        )
        # The snapshot is observational and is never an authority to grant leases.
        state = (
            "observed" if exact and status == "healthy"
            else "not_observed" if pane is None and status == "healthy"
            else "unverified"
        )
        return {
            "state": state,
            "pane_status": pane.get("agent_status") if pane else None,
            "agent": (pane.get("agent") if pane else None)
                or (agent.get("agent") if agent else None),
        }

    bindings: list[dict[str, Any]] = []
    for worker in workers:
        if (
            not worker.get("owner_session_id")
            or str(worker["owner_session_id"]) not in logical_ids
            or str(worker.get("workspace") or "") != workspace_key
        ):
            continue
        worker_id = str(worker.get("id") or "")
        pane_id = str(worker.get("pane") or "") or None
        has_write_lease = any(
            str(lease.get("worker_id") or "") == worker_id
            and str(lease.get("workspace") or "") == workspace_key
            for lease in leases
        )
        bindings.append({
            "source": "worker",
            "worker_id": worker_id,
            "logical_session_id": worker["owner_session_id"],
            "workspace_key": workspace_key,
            "herdr_workspace_id": None,
            "tab_id": None,
            "pane_id": pane_id,
            "agent": worker.get("agent"),
            "worker_state": worker.get("state"),
            "write_lease_active": has_write_lease,
            "observation": observation(pane_id),
        })

    meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    task = meta.get("active_pstack_task")
    if (
        isinstance(task, dict)
        and str(task.get("managed_session_id") or "") == str(session.get("id") or "")
        and str(task.get("workspace_key") or "") == workspace_key
    ):
        tab = task.get("herdr_tab")
        if isinstance(tab, dict) and tab.get("tab_id"):
            pane_id = str(tab.get("root_pane_id") or "") or None
            tab_observation = observation(
                pane_id,
                tab_id=str(tab.get("tab_id")),
                herdr_workspace_id=str(tab.get("workspace_id") or "") or None,
            )
            if (
                str(tab.get("status") or "").casefold() != "open"
                or str(task.get("status") or "").casefold()
                in {"completed", "failed", "cancelled", "expired"}
            ):
                tab_observation["state"] = "historical"
            bindings.append({
                "source": "pstack_task",
                "task_id": task.get("task_id"),
                "task_status": task.get("status"),
                "tab_status": tab.get("status"),
                "workspace_key": workspace_key,
                "herdr_workspace_id": tab.get("workspace_id"),
                "tab_id": tab.get("tab_id"),
                "pane_id": pane_id,
                "agent": None,
                "write_lease_active": None,
                "observation": tab_observation,
            })

    return {
        "schema": "hirda-session-inspection-v1",
        "managed_session_id": session.get("id"),
        "workspace_key": workspace_key,
        "herdr_snapshot_status": status,
        "herdr_observed_at": observed_at,
        "bindings": bindings,
        "binding_count": len(bindings),
        "authority": "read_only_observation",
    }


async def inspect_session_from_db(
    db: Any, session: dict[str, Any], herdr_snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    """Use current logical project pins, not historical transport, for ownership."""
    gateways = await db.list_gateway_sessions_for_managed_session(str(session["id"]))
    verified_gateways: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gateway in gateways:
        logical_id = str(gateway.get("studio_session_id") or "")
        if not logical_id or logical_id in seen:
            continue
        seen.add(logical_id)
        try:
            logical = await db.get_session(logical_id)
        except KeyError:
            continue
        if str(logical.get("managed_session_id") or "") == str(session["id"]):
            verified_gateways.append(gateway)
    return inspect_session_bindings(
        session,
        gateways=verified_gateways,
        workers=await db.list_workers(),
        leases=await db.list_workspace_leases(),
        herdr_snapshot=herdr_snapshot,
    )
