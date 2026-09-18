from __future__ import annotations

import asyncio
from typing import Any

from .db import Database, LeaseConflict
from .health import HealthManager
from .herdr import HerdrManager
from .settings import Settings


class Scheduler:
    """Persistent priority queue + workspace-aware logical worker assignment.

    In M3 this component still owns assignment/leases only. The separate
    ExecutionSupervisor owns Herdr prompt dispatch, monitoring and recovery.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        health: HealthManager,
        herdr: HerdrManager,
    ):
        self.settings = settings
        self.db = db
        self.health = health
        self.herdr = herdr
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._schedule_lock = asyncio.Lock()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-scheduler")
        self.kick()

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def kick(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        interval = max(0.2, float(self.settings.studio.scheduler_interval_seconds))
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stopping.is_set():
                break
            try:
                await self.schedule_once()
            except Exception as exc:
                await self.db.add_event(
                    "scheduler.error",
                    f"Scheduler tick failed: {exc}",
                    severity="warning",
                    server_id=self.settings.studio.worker_server_id,
                )

    def _upstream_available(self) -> bool:
        snapshot = self.health.snapshots.get(self.settings.studio.worker_server_id or "")
        return snapshot is None or snapshot.status in {"healthy", "degraded"}

    @staticmethod
    def _pane_list(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        value = snapshot.get("panes")
        if isinstance(value, dict) and isinstance(value.get("panes"), list):
            return [x for x in value["panes"] if isinstance(x, dict)]
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        return []

    def resolve_binding(self, work: dict[str, Any]) -> tuple[str | None, str | None]:
        requested_pane = work.get("requested_pane")
        requested_agent = work.get("requested_agent")
        panes = self._pane_list(self.herdr.snapshot)

        if requested_pane:
            pane = next((p for p in panes if p.get("pane_id") == requested_pane), None)
            return requested_pane, requested_agent or (pane or {}).get("agent")

        workspace = work.get("workspace")
        candidates = [
            p for p in panes
            if p.get("cwd") == workspace or p.get("foreground_cwd") == workspace
        ]
        if not candidates:
            return None, requested_agent

        def score(p: dict[str, Any]) -> tuple[int, int, int, str]:
            status_rank = {"idle": 0, "done": 1, "unknown": 2}.get(str(p.get("agent_status")), 3)
            agent_rank = 0 if p.get("agent") else 1
            cwd_rank = 0 if p.get("cwd") == workspace else 1
            focused_rank = 1 if p.get("focused") else 0
            return (status_rank, agent_rank, cwd_rank + focused_rank, str(p.get("pane_id") or ""))

        chosen = sorted(candidates, key=score)[0]
        return chosen.get("pane_id"), requested_agent or chosen.get("agent")

    async def schedule_once(self) -> dict[str, int]:
        async with self._schedule_lock:
            # Scheduler-managed logical workers need a heartbeat while a work item is
            # assigned. M3 will replace this with execution-aware liveness.
            await self.db.heartbeat_running_work()

            if not self._upstream_available():
                return {"started": 0, "queued": (await self.db.work_summary())["queue"]}

            workers = await self.db.idle_unbound_workers()
            if not workers:
                return {"started": 0, "queued": (await self.db.work_summary())["queue"]}

            queued = await self.db.queued_work(limit=self.settings.studio.scheduler_max_queue)
            started = 0
            free_workers = list(workers)

            for work in queued:
                if not free_workers:
                    break
                # Current worker pool belongs to one Serena server only.
                if work.get("server_id") != self.settings.studio.worker_server_id:
                    continue

                pane, agent = self.resolve_binding(work)
                worker = free_workers[0]
                try:
                    assigned = await self.db.assign_work(
                        work["id"], worker["id"], pane=pane, agent=agent
                    )
                except LeaseConflict:
                    # Do not let one workspace at the head of the queue block work
                    # for other workspaces.
                    continue
                except ValueError:
                    continue

                free_workers.pop(0)
                started += 1
                await self.db.add_event(
                    "work.started",
                    f"{assigned['id']} -> {worker['id']} ({assigned['workspace']})",
                    server_id=assigned["server_id"],
                    data={
                        "work_id": assigned["id"],
                        "worker_id": worker["id"],
                        "workspace": assigned["workspace"],
                        "pane": assigned.get("pane"),
                        "agent": assigned.get("agent"),
                        "priority": assigned["priority"],
                    },
                )

            return {"started": started, "queued": (await self.db.work_summary())["queue"]}
