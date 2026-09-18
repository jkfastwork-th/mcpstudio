from __future__ import annotations

import asyncio
from typing import Any

from .db import Database
from .settings import Settings


class WorkerManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        await self.db.ensure_workers(
            self.settings.studio.max_workers,
            self.settings.studio.worker_server_id or self.settings.servers[0].id,
        )
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-worker-monitor")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = max(2, self.settings.studio.worker_monitor_interval_seconds)
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                stale = await self.db.mark_stale_busy_workers(
                    self.settings.studio.worker_heartbeat_timeout_seconds
                )
                for worker in stale:
                    await self.db.add_event(
                        "worker.dead",
                        f"{worker['id']} missed heartbeat; write lease released",
                        severity="warning",
                        server_id=worker["server_id"],
                        data={"worker_id": worker["id"], "workspace": worker.get("workspace")},
                    )

    async def summary(self) -> dict[str, Any]:
        workers = await self.db.list_workers()
        counts = {state: sum(1 for w in workers if w["state"] == state) for state in ("idle", "busy", "degraded", "dead")}
        workspaces = sorted({w["workspace"] for w in workers if w.get("workspace")})
        return {
            "total": len(workers),
            "counts": counts,
            "bound": sum(1 for w in workers if w.get("workspace")),
            "workspaces": workspaces,
        }
