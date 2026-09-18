from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .settings import Settings


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObservabilityManager:
    """M6.2 production observability sampler and SLO evaluator.

    Sampling is intentionally local and low-cardinality. No request body, auth
    token, OAuth code, or header value is persisted by this manager.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self.snapshot: dict[str, Any] = {
            "enabled": settings.studio.observability_enabled,
            "status": "disabled" if not settings.studio.observability_enabled else "starting",
            "ticks": 0,
            "last_sample_at": None,
            "last_error": None,
        }

    async def start(self) -> None:
        if not self.settings.studio.observability_enabled:
            return
        await self.run_once()
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-observability")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = max(10.0, float(self.settings.studio.observability_interval_seconds))
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                try:
                    await self.run_once()
                except Exception as exc:  # supervisor boundary
                    self.snapshot["status"] = "degraded"
                    self.snapshot["last_error"] = str(exc)
                    await self.db.add_event(
                        "observability.error",
                        f"Observability sampling failed: {exc}",
                        severity="warning",
                    )

    async def run_once(self) -> dict[str, Any]:
        if not self.settings.studio.observability_enabled:
            return dict(self.snapshot)
        if self._run_lock.locked():
            return dict(self.snapshot)
        async with self._run_lock:
            workers = await self.db.list_workers(enabled_only=True)
            work = await self.db.work_summary()
            alerts = await self.db.list_alerts(status="open", limit=1000)
            health = await self.db.recent_health_summary()
            total_workers = len(workers)
            busy_workers = sum(1 for w in workers if w.get("state") == "busy")
            sample = {
                "upstream_ok": bool(health.get("all_healthy")),
                "upstream_status": health.get("overall", "unknown"),
                "worker_busy": busy_workers,
                "worker_total": total_workers,
                "queue_depth": int(work.get("queue") or 0),
                "active_work": int(work.get("active") or 0),
                "open_alerts": len(alerts),
            }
            await self.db.record_observability_sample(sample)
            await self.db.cleanup_observability(self.settings.studio.observability_retention_days)
            report = await self.report()
            if self.settings.studio.observability_alerts_enabled:
                await self._sync_alerts(report)
            self.snapshot.update({
                "status": "healthy" if report.get("overall_state") != "down" else "degraded",
                "ticks": int(self.snapshot.get("ticks") or 0) + 1,
                "last_sample_at": _iso(),
                "last_error": None,
                "overall_state": report.get("overall_state"),
            })
            return dict(self.snapshot)

    def _latest_restore_drill(self) -> dict[str, Any]:
        report_dir = self.settings.config_path.parent / "backups" / "restore-drills"
        try:
            items = sorted(report_dir.glob("restore-drill-*.json"), reverse=True)
            if not items:
                return {"status": "unknown", "ok": None, "created_at": None}
            data = json.loads(items[0].read_text(encoding="utf-8"))
            return {
                "status": "pass" if data.get("ok") else "fail",
                "ok": bool(data.get("ok")),
                "created_at": data.get("created_at"),
                "schema_version": data.get("schema_version"),
            }
        except Exception as exc:
            return {"status": "unknown", "ok": None, "created_at": None, "error": str(exc)}

    async def report(self) -> dict[str, Any]:
        report = await self.db.observability_report(
            window_minutes=self.settings.studio.observability_window_minutes,
            availability_target=self.settings.studio.slo_availability_target_percent,
            mcp_success_target=self.settings.studio.slo_mcp_success_target_percent,
            queue_p95_limit_ms=self.settings.studio.slo_queue_p95_limit_ms,
            worker_saturation_warn_percent=self.settings.studio.slo_worker_saturation_warn_percent,
            reconnect_rate_warn_per_100=self.settings.studio.slo_reconnect_rate_warn_per_100_requests,
            oauth_refresh_failures_max=self.settings.studio.slo_oauth_refresh_failures_max,
            orphan_events_max=self.settings.studio.slo_orphan_events_max,
        )
        report["restore_drill"] = self._latest_restore_drill()
        report["manager"] = dict(self.snapshot)
        states = [x.get("state") for x in report.get("objectives", {}).values()]
        restore = report["restore_drill"].get("status")
        if "down" in states or restore == "fail":
            overall = "down"
        elif "degraded" in states:
            overall = "degraded"
        elif any(x == "unknown" for x in states) or restore == "unknown":
            overall = "unknown"
        else:
            overall = "healthy"
        report["overall_state"] = overall
        return report

    async def _sync_alerts(self, report: dict[str, Any]) -> None:
        for name, item in report.get("objectives", {}).items():
            key = f"slo:{name}"
            state = item.get("state")
            if state in {"degraded", "down"}:
                severity = "critical" if state == "down" else "warning"
                await self.db.open_alert(
                    key,
                    f"slo.{name}",
                    item.get("message") or f"SLO threshold breached: {name}",
                    severity=severity,
                    data={k: v for k, v in item.items() if k != "message"},
                )
            else:
                await self.db.resolve_alert(key)

        restore = report.get("restore_drill", {})
        key = "slo:restore_drill"
        if restore.get("status") == "fail":
            await self.db.open_alert(
                key, "slo.restore_drill", "Latest production restore drill failed",
                severity="critical", data=restore,
            )
        elif restore.get("status") == "pass":
            await self.db.resolve_alert(key)
