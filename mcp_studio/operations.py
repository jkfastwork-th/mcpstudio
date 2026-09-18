from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .settings import Settings


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationsManager:
    """M6.1 conservative operations reconciliation and retention supervisor.

    The manager only repairs Studio-local bookkeeping. It never kills an agent,
    restarts Serena, or claims an already-dispatched prompt was cancelled.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._cleanup_ticks = 0
        self.snapshot: dict[str, Any] = {
            "enabled": settings.studio.operations_enabled,
            "status": "disabled" if not settings.studio.operations_enabled else "starting",
            "ticks": 0,
            "actions": 0,
            "last_tick_at": None,
            "last_cleanup": None,
            "last_error": None,
        }

    async def start(self) -> None:
        if not self.settings.studio.operations_enabled:
            return
        # Reconcile once before accepting a long-running production steady state.
        await self.run_once(force_cleanup=True)
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-operations")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = max(2.0, float(self.settings.studio.operations_interval_seconds))
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await self.run_once()
                except Exception as exc:  # defensive supervisor boundary
                    self.snapshot["status"] = "degraded"
                    self.snapshot["last_error"] = str(exc)
                    await self.db.add_event(
                        "operations.error",
                        f"Operations reconciliation failed: {exc}",
                        severity="warning",
                    )
                    await self.db.add_audit(
                        "operations.reconcile",
                        actor="system",
                        outcome="error",
                        data={"error": str(exc)},
                    )

    async def run_once(self, *, force_cleanup: bool = False) -> dict[str, Any]:
        if not self.settings.studio.operations_enabled:
            return dict(self.snapshot)
        if self._run_lock.locked():
            return dict(self.snapshot)
        async with self._run_lock:
            actions = await self.db.reconcile_operations(
                self.settings.studio.cancel_pending_detach_seconds
            )
            for action in actions:
                kind = str(action.get("kind") or "operations.action")
                target_id = action.get("work_id") or action.get("worker_id") or action.get("workspace")
                await self.db.add_event(
                    f"operations.{kind}",
                    f"Operations reconciled {kind}: {target_id}",
                    severity="warning" if kind == "work_detached" else "info",
                    data=action,
                )
                await self.db.add_audit(
                    f"operations.{kind}",
                    actor="system",
                    target_type=("work" if action.get("work_id") else "worker" if action.get("worker_id") else "workspace"),
                    target_id=str(target_id) if target_id is not None else None,
                    data=action,
                )
                if self.settings.studio.operations_alerts_enabled and kind == "work_detached":
                    await self.db.open_alert(
                        f"detached:{action['work_id']}",
                        "work.detached",
                        f"Work {action['work_id']} detached from Studio tracking ({action.get('reason')})",
                        severity="warning",
                        data=action,
                    )

            # Cleanup runs hourly-ish by tick count, and always once at startup.
            self._cleanup_ticks += 1
            interval = max(2.0, float(self.settings.studio.operations_interval_seconds))
            cleanup_every = max(1, int(3600 / interval))
            cleanup = None
            if force_cleanup or self._cleanup_ticks >= cleanup_every:
                cleanup = await self.db.cleanup_ephemera(
                    self.settings.studio.operations_ephemera_retention_days,
                    self.settings.studio.audit_retention_days,
                )
                self._cleanup_ticks = 0
                self.snapshot["last_cleanup"] = cleanup
                if any(cleanup.values()):
                    await self.db.add_audit(
                        "operations.retention_cleanup",
                        actor="system",
                        data=cleanup,
                    )

            self.snapshot.update({
                "status": "healthy",
                "ticks": int(self.snapshot.get("ticks") or 0) + 1,
                "actions": int(self.snapshot.get("actions") or 0) + len(actions),
                "last_tick_at": _iso(),
                "last_actions": actions[-20:],
                "last_error": None,
            })
            return dict(self.snapshot)
