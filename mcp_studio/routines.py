from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, cast


class RoutineManager:
    """Persistent Routine -> PStack lineage -> runtime routing -> Scheduler bridge.

    Routine definitions never own runtime authority. Each run asks HIRDA's
    cognitive router again so JEV, semantic ranking, supervisor policy and
    runtime availability are evaluated at execution time.
    """

    def __init__(
        self,
        settings: Any,
        db: Any,
        *,
        cognitive_router: Any | None = None,
        scheduler: Any | None = None,
        execution: Any | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.cognitive_router = cognitive_router
        self.scheduler = scheduler
        self.execution = execution

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        cadence = str(payload.get("cadence") or "manual").strip().casefold()
        interval = payload.get("interval_seconds")
        if cadence == "interval" and int(interval or 0) < 60:
            raise ValueError("interval routine requires interval_seconds >= 60")
        return await self.db.create_routine(payload)

    async def _runtime_plan(
        self,
        *,
        routine: dict[str, Any],
        run_id: str,
        pstack_task_id: str,
        work_template: dict[str, Any],
    ) -> dict[str, Any]:
        router = self.cognitive_router
        bootstrap = getattr(router, "bootstrap_runtime", None)
        if not callable(bootstrap):
            return {
                "runtime": work_template.get("agent"),
                "authority": "hirda",
                "reason": "cognitive_router_unavailable",
                "candidates": [],
            }

        metadata = work_template.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        capability = str(
            metadata.get("routine_capability")
            or metadata.get("capability")
            or "coding"
        )
        request = {
            "schema": "hirda-cognitive-request-v1",
            "request_id": f"ROUTINE-{run_id}",
            "prompt": str(
                work_template.get("instruction")
                or work_template.get("label")
                or routine.get("name")
                or "Execute scheduled routine."
            ),
            "capability": capability,
            "preferred_depth": str(
                metadata.get("preferred_depth") or "auto"
            ),
            "allow_fallback": True,
            "context": {
                "routine_id": routine["id"],
                "routine_run_id": run_id,
                "pstack_task_id": pstack_task_id,
                "runtime_authority": "hirda",
                "workflow_provider": "pstack",
            },
        }
        try:
            plan = await bootstrap(request)
        except Exception as exc:
            return {
                "runtime": work_template.get("agent"),
                "authority": "hirda",
                "reason": f"runtime_bootstrap_failed:{type(exc).__name__}",
                "error": str(exc)[:1000],
                "candidates": [],
            }
        return {
            "runtime": str(plan.get("runtime") or "").strip().casefold() or None,
            "authority": "hirda",
            "lane": plan.get("lane"),
            "semantic_routing": plan.get("semantic_routing"),
            "supervisor_policy": plan.get("supervisor_policy"),
            "candidates": list(plan.get("candidates") or []),
            "reason": None,
        }

    async def trigger(
        self,
        routine_id: str,
        *,
        trigger: str = "manual",
        scheduled_for: str | None = None,
    ) -> dict[str, Any]:
        routine = await self.db.get_routine(routine_id)
        if trigger != "manual" and not routine.get("enabled"):
            raise ValueError("routine is disabled")

        pstack_task_id = f"TASK-R-{uuid.uuid4().hex[:12].upper()}"
        run = await self.db.create_routine_run(
            routine_id=routine_id,
            scheduled_for=scheduled_for,
            pstack_task_id=pstack_task_id,
        )
        work_payload = copy.deepcopy(routine.get("work_template") or {})
        metadata = work_payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        work_payload["metadata"] = metadata
        routine_metadata = routine.get("metadata")
        routine_metadata = (
            routine_metadata if isinstance(routine_metadata, dict) else {}
        )
        for policy_key in ("scheduler_contract", "jev_triage"):
            policy_value = routine_metadata.get(policy_key)
            if (
                policy_key not in metadata
                and isinstance(policy_value, dict)
            ):
                metadata[policy_key] = copy.deepcopy(policy_value)

        runtime_plan = await self._runtime_plan(
            routine=routine,
            run_id=run["id"],
            pstack_task_id=pstack_task_id,
            work_template=work_payload,
        )
        selected_runtime = runtime_plan.get("runtime")
        if selected_runtime:
            work_payload["agent"] = selected_runtime

        metadata.update(
            {
                "routine_id": routine_id,
                "routine_run_id": run["id"],
                "routine_trigger": trigger,
                "routine_pstack_task_id": pstack_task_id,
                "runtime_selection": runtime_plan,
                "orchestration_contract": {
                    "schema": "hirda-routine-orchestration-v1",
                    "authority": "hirda",
                    "methodology": "pstack",
                    "flow": [
                        "routine",
                        "pstack",
                        "jev_triage",
                        "semantic_runtime_ranking",
                        "supervisor_policy",
                        "capability_resolution",
                        "scheduler",
                        "execution",
                    ],
                },
            }
        )

        item = await self.db.create_work(
            work_payload,
            self.settings.studio.worker_server_id,
        )
        run = await self.db.bind_routine_run_work(
            run_id=run["id"],
            work_id=item["id"],
        )
        await self.db.add_event(
            "routine.run.queued",
            f"Routine {routine_id} queued run {run['id']}.",
            server_id=item["server_id"],
            data={
                "routine_id": routine_id,
                "routine_run_id": run["id"],
                "pstack_task_id": pstack_task_id,
                "work_id": item["id"],
                "trigger": trigger,
                "runtime_selection": runtime_plan,
            },
            stream_id=f"routine:{routine_id}",
            correlation_id=run["id"],
        )
        if self.scheduler is not None:
            self.scheduler.kick()
        if self.execution is not None:
            self.execution.kick()
        return {
            "routine": routine,
            "run": run,
            "work": item,
            "runtime_selection": runtime_plan,
        }

    async def enqueue_due(self, *, limit: int = 50) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        due = await self.db.due_routines(now=now, limit=limit)
        queued: list[dict[str, Any]] = []
        for routine in due:
            queued.append(
                await self.trigger(
                    routine["id"],
                    trigger="schedule",
                    scheduled_for=routine.get("next_run_at"),
                )
            )
        return {
            "due": len(due),
            "queued": len(queued),
            "run_ids": [item["run"]["id"] for item in queued],
        }

    async def reconcile_runs(self, *, limit_per_routine: int = 100) -> dict[str, int]:
        routines = await self.db.list_routines()
        completed = 0
        failed = 0
        cancelled = 0
        for routine in routines:
            runs = await self.db.list_routine_runs(
                routine["id"],
                limit=limit_per_routine,
            )
            for run in runs:
                if run.get("status") not in {"queued", "running"}:
                    continue
                work_id = str(run.get("work_id") or "").strip()
                if not work_id:
                    continue
                try:
                    work = await self.db.get_work(work_id)
                except KeyError:
                    continue
                state = str(work.get("state") or "").casefold()
                if state not in {"completed", "failed", "cancelled"}:
                    continue
                evidence = {
                    "schema": "hirda-routine-run-evidence-v1",
                    "work_state": state,
                    "execution_state": work.get("execution_state"),
                    "failure_class": work.get("failure_class"),
                    "semantic_completion": (
                        (work.get("result") or {}).get("semantic_completion")
                        if isinstance(work.get("result"), dict)
                        else None
                    ),
                }
                await self.db.finish_routine_run(
                    run_id=run["id"],
                    status=state,
                    evidence=evidence,
                    error=work.get("error"),
                )
                if state == "completed":
                    completed += 1
                elif state == "failed":
                    failed += 1
                else:
                    cancelled += 1
        return {
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
        }
