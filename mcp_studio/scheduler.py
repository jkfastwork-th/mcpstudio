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
        self.execution: Any | None = None
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


    @staticmethod
    def _scheduler_contract(work: dict[str, Any]) -> dict[str, Any]:
        metadata = work.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        contract = metadata.get("scheduler_contract")
        contract = contract if isinstance(contract, dict) else {}
        depends_on = [
            str(item).strip()
            for item in (contract.get("depends_on") or [])
            if str(item or "").strip()
        ]
        related_group = str(contract.get("related_group") or "").strip() or None
        max_concurrency = contract.get("max_concurrency")
        try:
            max_concurrency = int(max_concurrency) if max_concurrency is not None else None
        except (TypeError, ValueError):
            max_concurrency = None
        if max_concurrency is not None and max_concurrency < 1:
            max_concurrency = None
        priority_boost = contract.get("priority_boost", 0)
        try:
            priority_boost = int(priority_boost or 0)
        except (TypeError, ValueError):
            priority_boost = 0
        jev = metadata.get("jev_triage")
        jev = jev if isinstance(jev, dict) else {}
        try:
            priority_boost += int(jev.get("priority_delta") or 0)
        except (TypeError, ValueError):
            pass
        if not related_group:
            related_group = str(jev.get("related_group") or "").strip() or None
        return {
            "depends_on": depends_on,
            "related_group": related_group,
            "max_concurrency": max_concurrency,
            "priority_boost": priority_boost,
            "jev_advisory": jev,
        }

    async def _scheduler_gate(
        self,
        work: dict[str, Any],
        *,
        running_group_counts: dict[str, int],
    ) -> tuple[bool, dict[str, Any]]:
        contract = self._scheduler_contract(work)
        pending_dependencies: list[str] = []
        failed_dependencies: list[str] = []
        for dependency_id in contract["depends_on"]:
            try:
                dependency = await self.db.get_work(dependency_id)
            except KeyError:
                pending_dependencies.append(dependency_id)
                continue
            state = str(dependency.get("state") or "").casefold()
            if state == "completed":
                continue
            if state in {"failed", "cancelled", "detached"}:
                failed_dependencies.append(dependency_id)
            else:
                pending_dependencies.append(dependency_id)

        if failed_dependencies:
            return False, {
                "reason": "dependency_failed",
                "dependencies": failed_dependencies,
                "contract": contract,
            }
        if pending_dependencies:
            return False, {
                "reason": "waiting_dependency",
                "dependencies": pending_dependencies,
                "contract": contract,
            }

        group = contract.get("related_group")
        limit = contract.get("max_concurrency")
        if group and limit is not None:
            active = int(running_group_counts.get(group, 0))
            if active >= limit:
                return False, {
                    "reason": "waiting_concurrency",
                    "related_group": group,
                    "active": active,
                    "limit": limit,
                    "contract": contract,
                }

        return True, {"reason": None, "contract": contract}

    @staticmethod
    def _effective_priority(
        work: dict[str, Any],
        *,
        running_groups: set[str],
    ) -> int:
        contract = Scheduler._scheduler_contract(work)
        score = int(work.get("priority") or 0) + int(contract.get("priority_boost") or 0)
        group = contract.get("related_group")
        if group and group in running_groups:
            score += 5
        return score

    async def schedule_once(self) -> dict[str, int]:
        async with self._schedule_lock:
            routines = getattr(self, "routines", None)
            if routines is not None:
                reconcile_runs = getattr(routines, "reconcile_runs", None)
                if callable(reconcile_runs):
                    await reconcile_runs()
                enqueue_due = getattr(routines, "enqueue_due", None)
                if callable(enqueue_due):
                    await enqueue_due()

            await self.db.heartbeat_running_work()

            if not self._upstream_available():
                return {"started": 0, "queued": (await self.db.work_summary())["queue"]}

            workers = await self.db.idle_unbound_workers()
            if not workers:
                return {"started": 0, "queued": (await self.db.work_summary())["queue"]}

            queued = await self.db.queued_work(limit=self.settings.studio.scheduler_max_queue)
            running = await self.db.list_work(
                state="running",
                limit=self.settings.studio.scheduler_max_queue,
            )
            running_group_counts: dict[str, int] = {}
            for item in running:
                group = self._scheduler_contract(item).get("related_group")
                if group:
                    running_group_counts[group] = running_group_counts.get(group, 0) + 1
            running_groups = set(running_group_counts)

            queued = sorted(
                queued,
                key=lambda item: (
                    -self._effective_priority(item, running_groups=running_groups),
                    str(item.get("queued_at") or ""),
                    str(item.get("id") or ""),
                ),
            )

            started = 0
            free_workers = list(workers)

            for work in queued:
                if not free_workers:
                    break
                if work.get("server_id") != self.settings.studio.worker_server_id:
                    continue

                eligible, gate = await self._scheduler_gate(
                    work,
                    running_group_counts=running_group_counts,
                )
                if not eligible:
                    await self.db.update_queued_work_wait(
                        work["id"],
                        execution_state=str(gate.get("reason") or "waiting_scheduler"),
                        wait_detail=gate,
                    )
                    await self.db.add_event(
                        "scheduler.waiting",
                        f"{work['id']} waiting: {gate.get('reason')}",
                        server_id=work["server_id"],
                        data={
                            "work_id": work["id"],
                            "reason": gate.get("reason"),
                            "dependencies": gate.get("dependencies"),
                            "related_group": gate.get("related_group"),
                            "active": gate.get("active"),
                            "limit": gate.get("limit"),
                        },
                    )
                    continue

                pane, agent = self.resolve_binding(work)
                worker = free_workers[0]
                try:
                    assigned = await self.db.assign_work(
                        work["id"], worker["id"], pane=pane, agent=agent
                    )
                except LeaseConflict:
                    continue
                except ValueError:
                    continue

                free_workers.pop(0)
                started += 1
                contract = self._scheduler_contract(assigned)
                related_group = contract.get("related_group")
                if related_group:
                    running_group_counts[related_group] = (
                        running_group_counts.get(related_group, 0) + 1
                    )
                    running_groups.add(related_group)

                metadata = assigned.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                pstack_task_id = str(
                    metadata.get("pstack_task_id") or ""
                ).strip()
                pstack_trace_id = str(
                    metadata.get("pstack_trace_id") or ""
                ).strip()
                if pstack_task_id:
                    await self.db.patch_pstack_task(
                        pstack_task_id,
                        {
                            "work_id": assigned["id"],
                            "status": "waiting",
                            "execution_state": "assigned",
                        },
                    )
                    if pstack_trace_id:
                        await self.db.add_event(
                            "pstack.execution.assigned",
                            f"PStack task {pstack_task_id} assigned to Scheduler worker",
                            data={
                                "task_id": pstack_task_id,
                                "trace_id": pstack_trace_id,
                                "work_id": assigned["id"],
                                "actor": "hirda",
                                "phase": "scheduler_assigned",
                                "title": "Scheduler assigned",
                                "detail": (
                                    f"Work {assigned['id']} was assigned to "
                                    f"{worker['id']}; execution has not started yet."
                                ),
                                "status": "waiting",
                                "trace_status": "waiting",
                                "metadata": {
                                    "worker_id": worker["id"],
                                    "pane": assigned.get("pane"),
                                    "scheduler_contract": contract,
                                },
                            },
                            stream_id=f"pstack:{pstack_trace_id}",
                            correlation_id=pstack_trace_id,
                        )
                execution = getattr(self, "execution", None)
                if execution is not None:
                    execution.kick()
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
                        "effective_priority": self._effective_priority(
                            assigned,
                            running_groups=running_groups,
                        ),
                        "scheduler_contract": contract,
                    },
                )

            return {"started": started, "queued": (await self.db.work_summary())["queue"]}
