from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx

from .db import Database
from .herdr import HerdrManager, _decode_text_content
from .settings import Settings


TERMINAL_AGENT_STATES = {"idle", "done"}
ACTIVE_AGENT_STATES = {"busy", "running", "working", "thinking", "executing", "active"}


class ToolSchemaError(RuntimeError):
    pass


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_dt()).isoformat()


def classify_failure(exc: Exception) -> str:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "mcp_transport"
    if isinstance(exc, httpx.TimeoutException):
        return "dispatch_uncertain"
    text = str(exc).lower()
    if "connection refused" in text or "connect error" in text:
        return "mcp_transport"
    if "timeout" in text or "timed out" in text:
        return "dispatch_uncertain"
    if "schema" in text or "required argument" in text or "cannot map" in text:
        return "tool_schema"
    if "not available" in text or "not found" in text:
        return "tool_missing"
    if "iserror=true" in text or "tools/call" in text:
        return "tool_error"
    return "execution_error"


def _properties(tool: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    return (props if isinstance(props, dict) else {}, required if isinstance(required, list) else [])


def _agent_session_value(pane: dict[str, Any] | None) -> str | None:
    if not pane:
        return None
    session = pane.get("agent_session")
    if isinstance(session, dict) and session.get("value"):
        return str(session["value"])
    return None


def build_tool_arguments(
    tool: dict[str, Any],
    *,
    pane: dict[str, Any] | None,
    pane_id: str | None,
    agent: str | None,
    prompt: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Map Studio's generic target/prompt fields to the live Herdr tool schema.

    M3 reads tools/list rather than assuming one local Herdr schema. Required
    fields are satisfied first; when a schema offers several optional target
    selectors, Studio sends only one to avoid ambiguous/exclusive selectors.
    """
    props, required = _properties(tool)
    required_set = set(required)
    args: dict[str, Any] = {}
    session_value = _agent_session_value(pane)
    resolved_pane = pane_id or (pane or {}).get("pane_id")
    resolved_agent = agent or (pane or {}).get("agent")

    pane_names = ("pane_id", "pane", "target_pane", "target_pane_id")
    # Herdr's live schema uses `agent_id` for either an agent name *or a pane ID*.
    # Keep true session identifiers separate so an agent-session UUID is never
    # preferred over a pane ID when the schema explicitly documents pane/name.
    agent_ref_names = ("agent_id",)
    session_names = ("agent_session_id", "session_id", "agent_session")
    agent_names = ("agent", "agent_name")
    generic_target_names = ("target", "target_id", "id", "agent_ref")
    prompt_names = ("prompt", "message", "text", "input", "instruction", "request", "content", "query")
    timeout_names = ("timeout_ms", "timeout", "timeout_seconds", "wait_seconds", "seconds")
    target_names = pane_names + agent_ref_names + session_names + agent_names + generic_target_names

    def target_value(name: str) -> Any:
        if name in pane_names:
            return resolved_pane
        if name in agent_ref_names:
            spec = props.get(name) or {}
            desc = str(spec.get("description") or "").lower() if isinstance(spec, dict) else ""
            # Current Herdr contract: `agent_id` = unique agent name OR pane ID.
            if "pane id" in desc or "agent name" in desc:
                return resolved_pane or resolved_agent or session_value
            return session_value or resolved_pane or resolved_agent
        if name in session_names:
            return session_value
        if name in agent_names:
            return resolved_agent
        if name in generic_target_names:
            return resolved_pane or resolved_agent or session_value
        return None

    # Fill every required target selector, since schemas occasionally require a
    # pane plus an agent/session identifier.
    mapped_required_target = False
    for name in required:
        if name in target_names:
            value = target_value(name)
            if value is not None:
                args[name] = value
                mapped_required_target = True

    # If there is no required target selector, send exactly one optional target.
    if not mapped_required_target:
        for group in (pane_names, agent_ref_names, session_names, agent_names, generic_target_names):
            chosen = next((name for name in group if name in props and target_value(name) is not None), None)
            if chosen:
                args[chosen] = target_value(chosen)
                break

    if prompt is not None:
        prompt_candidates = [name for name in prompt_names if name in props]
        if not prompt_candidates:
            raise ToolSchemaError(f"cannot map prompt argument for Herdr tool {tool.get('name')!r}")
        chosen_prompt = next((name for name in prompt_candidates if name in required_set), prompt_candidates[0])
        args[chosen_prompt] = prompt

    if timeout_seconds is not None:
        timeout_candidates = [name for name in timeout_names if name in props]
        if timeout_candidates:
            chosen_timeout = next((name for name in timeout_candidates if name in required_set), timeout_candidates[0])
            args[chosen_timeout] = (
                int(timeout_seconds * 1000) if chosen_timeout.endswith("_ms") else timeout_seconds
            )

    if props and not any(name in args for name in target_names):
        raise ToolSchemaError(f"cannot map target argument for Herdr tool {tool.get('name')!r}")

    # Honor harmless schema defaults before declaring an unsupported required arg.
    for name in required:
        if name in args:
            continue
        spec = props.get(name) or {}
        if isinstance(spec, dict) and "default" in spec:
            args[name] = spec["default"]
            continue
        raise ToolSchemaError(
            f"cannot map required argument {name!r} for Herdr tool {tool.get('name')!r}"
        )
    return args


class ExecutionSupervisor:
    """M4 parallel Herdr execution + conservative resilience supervision.

    Safety properties inherited from M3:
    - never restarts Serena/systemd/the host;
    - never automatically re-sends a prompt after an ambiguous dispatch;
    - keeps a workspace lease while transient target/transport recovery is attempted;
    - automatic initial dispatch remains opt-in via execution_enabled.

    M4 changes:
    - different work items are dispatched/monitored concurrently up to
      execution_parallelism;
    - per-work locks prevent duplicate concurrent dispatches for one work item;
    - optional in-memory fault injection can certify worker isolation/recovery
      without stopping a real pane or Serena.
    """

    def __init__(self, settings: Settings, db: Database, herdr: HerdrManager, scheduler: Any):
        self.settings = settings
        self.db = db
        self.herdr = herdr
        self.scheduler = scheduler
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._work_locks: dict[str, asyncio.Lock] = {}
        self._counter_lock = asyncio.Lock()
        self._active_dispatches = 0
        self._faults: dict[str, dict[str, Any]] = {}
        self.snapshot: dict[str, Any] = {
            "status": "idle",
            "automatic_dispatch": settings.studio.execution_enabled,
            "parallelism": settings.studio.execution_parallelism,
            "resilience_test_mode": settings.studio.resilience_test_mode,
            "last_tick_at": None,
            "last_error": None,
            "ticks": 0,
            "dispatches": 0,
            "batch_dispatches": 0,
            "active_dispatches": 0,
            "peak_parallel_dispatches": 0,
            "recoveries": 0,
            "completed": 0,
            "faults_injected": 0,
        }

    async def start(self) -> None:
        # Resilience certification needs deterministic, observable supervisor
        # transitions.  In test mode we therefore disable the background
        # execution loop and advance it only through explicit /api/execution/run
        # ticks.  Scheduler/worker loops continue normally.
        if self.settings.studio.resilience_test_mode:
            self.snapshot["manual_tick_mode"] = True
            self.snapshot["status"] = "certification_manual"
            return
        self.snapshot["manual_tick_mode"] = False
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-execution-supervisor")
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
        # In resilience_test_mode only an explicit run_once() is allowed to
        # advance execution state.  This prevents a background tick from
        # consuming both the injected fault and its recovery before the
        # certification script can observe the reconnecting state.
        if self.settings.studio.resilience_test_mode:
            return
        self._wake.set()

    def _work_lock(self, work_id: str) -> asyncio.Lock:
        lock = self._work_locks.get(work_id)
        if lock is None:
            lock = asyncio.Lock()
            self._work_locks[work_id] = lock
        return lock

    @asynccontextmanager
    async def _dispatch_slot(self):
        async with self._counter_lock:
            self._active_dispatches += 1
            self.snapshot["active_dispatches"] = self._active_dispatches
            self.snapshot["peak_parallel_dispatches"] = max(
                int(self.snapshot.get("peak_parallel_dispatches") or 0),
                self._active_dispatches,
            )
        try:
            yield
        finally:
            async with self._counter_lock:
                self._active_dispatches = max(0, self._active_dispatches - 1)
                self.snapshot["active_dispatches"] = self._active_dispatches

    async def _loop(self) -> None:
        interval = max(0.5, float(self.settings.studio.execution_interval_seconds))
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stopping.is_set():
                break
            try:
                await self.run_once()
            except Exception as exc:
                self.snapshot["status"] = "degraded"
                self.snapshot["last_error"] = str(exc)
                await self.db.add_event(
                    "execution.supervisor_error",
                    f"Execution supervisor tick failed: {exc}",
                    severity="warning",
                    server_id=self.settings.studio.worker_server_id,
                )

    async def _ensure_fresh_herdr(self) -> dict[str, Any]:
        return await self.herdr.refresh()

    def _pane_for_work(self, work: dict[str, Any]) -> dict[str, Any] | None:
        return self.herdr.find_pane(
            pane_id=work.get("pane") or work.get("requested_pane"),
            workspace=work.get("workspace"),
            agent=work.get("agent") or work.get("requested_agent"),
        )

    async def inject_fault(
        self,
        work_id: str,
        *,
        kind: str,
        ticks: int = 1,
        note: str | None = None,
        stage: str = "any",
    ) -> dict[str, Any]:
        if not self.settings.studio.resilience_test_mode:
            raise PermissionError("resilience_test_mode is disabled")
        if kind not in {"pane_missing", "mcp_transport"}:
            raise ValueError("unsupported resilience fault")
        if stage not in {"any", "post_dispatch"}:
            raise ValueError("unsupported resilience fault stage")
        work = await self.db.get_work(work_id)
        if work.get("state") != "running":
            raise ValueError("fault injection requires running work")
        self._faults[work_id] = {
            "kind": kind,
            "ticks": int(ticks),
            "note": note,
            "stage": stage,
        }
        self.snapshot["faults_injected"] = int(self.snapshot.get("faults_injected") or 0) + 1
        await self.db.add_event(
            "resilience.fault_injected",
            f"{work_id}: simulated {kind} for {ticks} tick(s) at stage={stage}",
            severity="warning",
            server_id=work["server_id"],
            data={
                "work_id": work_id,
                "kind": kind,
                "ticks": ticks,
                "note": note,
                "stage": stage,
            },
        )
        if stage != "post_dispatch" or bool(work.get("dispatched_at")) or int(work.get("dispatch_attempts") or 0) > 0:
            self.kick()
        return {"work_id": work_id, **self._faults[work_id]}

    async def clear_fault(self, work_id: str) -> dict[str, Any]:
        existed = self._faults.pop(work_id, None)
        return {"work_id": work_id, "cleared": bool(existed)}

    def _consume_fault(self, work: dict[str, Any]) -> str | None:
        work_id = work["id"]
        fault = self._faults.get(work_id)
        if not fault:
            return None

        # A post-dispatch fault models loss while supervising work whose prompt
        # was already accepted.  Do not consume it while the work is merely
        # assigned; otherwise the certification would test pre-dispatch
        # recovery and could never prove the no-redispatch invariant.
        if fault.get("stage") == "post_dispatch":
            dispatched = bool(work.get("dispatched_at")) or int(work.get("dispatch_attempts") or 0) > 0
            if not dispatched:
                return None

        kind = str(fault["kind"])
        fault["ticks"] = int(fault.get("ticks") or 1) - 1
        if fault["ticks"] <= 0:
            self._faults.pop(work_id, None)
        return kind

    async def dispatch(
        self,
        work_id: str,
        *,
        explicit: bool = False,
        allow_redispatch: bool = False,
        refresh: bool = True,
    ) -> dict[str, Any]:
        async with self._work_lock(work_id):
            work = await self.db.get_work(work_id)
            if work["state"] != "running":
                raise ValueError(f"work {work_id} is not running")
            if work.get("dispatch_mode") != "herdr":
                raise ValueError(f"work {work_id} dispatch_mode is not herdr")
            instruction = (work.get("instruction") or "").strip()
            if not instruction:
                raise ValueError(f"work {work_id} has no instruction")
            if work.get("dispatch_attempts", 0) > 0 and not allow_redispatch:
                raise ValueError("work has already had a dispatch attempt; redispatch requires explicit acknowledgement")
            if not explicit and not self.settings.studio.execution_enabled:
                raise ValueError("automatic execution dispatch is disabled")

            snapshot = await self._ensure_fresh_herdr() if refresh else self.herdr.snapshot
            if snapshot.get("status") == "down":
                await self.db.update_work_execution(
                    work_id,
                    execution_state="reconnecting",
                    failure_class="mcp_transport",
                )
                await self.db.mark_worker_degraded_for_work(work_id)
                raise RuntimeError(f"Herdr unavailable: {snapshot.get('error')}")

            tool = self.herdr.tool("herdr_prompt_agent")
            if not tool:
                await self.db.update_work_execution(
                    work_id,
                    execution_state="stalled",
                    failure_class="tool_missing",
                )
                raise RuntimeError("herdr_prompt_agent is not available")

            pane = self._pane_for_work(work)
            if not pane:
                await self.db.update_work_execution(
                    work_id,
                    execution_state="reconnecting",
                    failure_class="pane_missing",
                )
                await self.db.mark_worker_degraded_for_work(work_id)
                raise RuntimeError("target pane is not currently discoverable")

            args = build_tool_arguments(
                tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=work.get("agent") or pane.get("agent"),
                prompt=instruction,
            )
            baseline = {
                "pane_id": pane.get("pane_id"),
                "revision": pane.get("revision"),
                "state_change_seq": pane.get("state_change_seq"),
                "agent_status": pane.get("agent_status"),
                "saw_activity": False,
                "dispatch_args": {k: "<instruction>" if v == instruction else v for k, v in args.items()},
            }
            await self.db.update_work_execution(
                work_id,
                execution_state="dispatching",
                pane=pane.get("pane_id"),
                agent=work.get("agent") or pane.get("agent"),
                last_agent_status=str(pane.get("agent_status") or "unknown"),
                failure_class=None,
                execution_patch=baseline,
                increment_dispatch=True,
            )
            try:
                async with self._dispatch_slot():
                    result = await self.herdr.client().call_tool(
                        "herdr_prompt_agent",
                        args,
                        client_name=f"mcp-studio-{work.get('worker_id') or 'worker'}",
                    )
            except Exception as exc:
                failure = classify_failure(exc)
                state = "dispatch_uncertain" if failure == "dispatch_uncertain" else "recovering"
                await self.db.update_work_execution(
                    work_id,
                    execution_state=state,
                    failure_class=failure,
                    execution_patch={"dispatch_error": str(exc)},
                )
                await self.db.mark_worker_degraded_for_work(work_id)
                await self.db.add_event(
                    "execution.dispatch_failed",
                    f"{work_id} dispatch failed ({failure}): {exc}",
                    severity="warning",
                    server_id=work["server_id"],
                    data={"work_id": work_id, "failure_class": failure},
                )
                raise

            decoded = _decode_text_content(result)
            item = await self.db.update_work_execution(
                work_id,
                execution_state="waiting_agent",
                last_agent_status=str(pane.get("agent_status") or "unknown"),
                failure_class=None,
                execution_patch={"dispatch_result": decoded},
                mark_dispatched=True,
            )
            self.snapshot["dispatches"] += 1
            await self.db.add_event(
                "execution.dispatched",
                f"{work_id} dispatched to {pane.get('pane_id')}",
                server_id=work["server_id"],
                data={
                    "work_id": work_id,
                    "worker_id": work.get("worker_id"),
                    "pane": pane.get("pane_id"),
                    "agent": item.get("agent"),
                },
            )
            return item

    async def dispatch_ready(self, work_ids: list[str] | None = None) -> dict[str, Any]:
        """Explicitly dispatch assigned Herdr work in parallel.

        This is primarily useful for certification while execution_enabled=false.
        It still obeys per-work duplicate-dispatch protection.
        """
        snapshot = await self._ensure_fresh_herdr()
        if snapshot.get("status") == "down":
            raise RuntimeError(f"Herdr unavailable: {snapshot.get('error')}")
        running = await self.db.running_work()
        allowed = set(work_ids or [])
        candidates = [
            w for w in running
            if w.get("dispatch_mode") == "herdr"
            and (w.get("execution_state") or "none") in {"none", "assigned"}
            and (not allowed or w["id"] in allowed)
        ]
        sem = asyncio.Semaphore(max(1, int(self.settings.studio.execution_parallelism)))

        async def one(work: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                try:
                    item = await self.dispatch(work["id"], explicit=True, refresh=False)
                    return {"work_id": work["id"], "ok": True, "execution_state": item.get("execution_state")}
                except Exception as exc:
                    return {"work_id": work["id"], "ok": False, "error": str(exc)}

        results = await asyncio.gather(*(one(w) for w in candidates)) if candidates else []
        self.snapshot["batch_dispatches"] = int(self.snapshot.get("batch_dispatches") or 0) + 1
        return {
            "requested": len(candidates),
            "succeeded": sum(1 for r in results if r.get("ok")),
            "failed": sum(1 for r in results if not r.get("ok")),
            "results": results,
            "peak_parallel_dispatches": self.snapshot.get("peak_parallel_dispatches", 0),
        }

    async def _try_read_result(self, work: dict[str, Any], pane: dict[str, Any]) -> Any:
        if not self.settings.studio.execution_read_result:
            return None
        tool = self.herdr.tool("herdr_read_agent")
        if not tool:
            return None
        try:
            args = build_tool_arguments(
                tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=work.get("agent") or pane.get("agent"),
            )
            raw = await self.herdr.client().call_tool(
                "herdr_read_agent", args, client_name="mcp-studio-result-reader"
            )
            return _decode_text_content(raw)
        except Exception as exc:
            return {"read_error": str(exc)}

    async def _complete_from_agent(self, work: dict[str, Any], pane: dict[str, Any]) -> dict[str, Any]:
        output = await self._try_read_result(work, pane)
        result = {
            "source": "herdr",
            "pane": pane.get("pane_id"),
            "agent": pane.get("agent") or work.get("agent"),
            "agent_status": pane.get("agent_status"),
            "revision": pane.get("revision"),
            "output": output,
        }
        item = await self.db.finish_work(work["id"], state="completed", result=result)
        self.snapshot["completed"] += 1
        await self.db.add_event(
            "execution.completed",
            f"{work['id']} completed on {pane.get('pane_id')}",
            server_id=work["server_id"],
            data={"work_id": work["id"], "worker_id": work.get("worker_id"), "pane": pane.get("pane_id")},
        )
        self.scheduler.kick()
        return item

    async def _monitor_waiting(self, work: dict[str, Any]) -> None:
        pane = self._pane_for_work(work)
        if not pane:
            await self.db.update_work_execution(
                work["id"],
                execution_state="reconnecting",
                failure_class="pane_missing",
            )
            await self.db.mark_worker_degraded_for_work(work["id"])
            await self.db.add_event(
                "execution.reconnecting",
                f"{work['id']} lost pane {work.get('pane')}",
                severity="warning",
                server_id=work["server_id"],
                data={"work_id": work["id"], "pane": work.get("pane")},
            )
            return

        execution = dict(work.get("execution") or {})
        baseline_revision = execution.get("revision")
        baseline_seq = execution.get("state_change_seq")
        status = str(pane.get("agent_status") or "unknown").lower()
        revision = pane.get("revision")
        seq = pane.get("state_change_seq")
        changed = False
        if baseline_revision is not None and revision is not None:
            try:
                changed = int(revision) > int(baseline_revision)
            except Exception:
                changed = revision != baseline_revision
        if baseline_seq is not None and seq is not None:
            try:
                changed = changed or int(seq) > int(baseline_seq)
            except Exception:
                changed = changed or seq != baseline_seq
        saw_activity = bool(execution.get("saw_activity")) or changed or status in ACTIVE_AGENT_STATES

        await self.db.update_work_execution(
            work["id"],
            execution_state="waiting_agent",
            pane=pane.get("pane_id"),
            agent=work.get("agent") or pane.get("agent"),
            last_agent_status=status,
            failure_class=None,
            execution_patch={"saw_activity": saw_activity, "last_revision": revision, "last_state_change_seq": seq},
        )
        if saw_activity and status in TERMINAL_AGENT_STATES:
            refreshed = await self.db.get_work(work["id"])
            await self._complete_from_agent(refreshed, pane)
            return

        dispatched_at = work.get("dispatched_at")
        if dispatched_at:
            try:
                age = (_now_dt() - datetime.fromisoformat(dispatched_at)).total_seconds()
            except Exception:
                age = 0
            if age >= self.settings.studio.execution_stall_seconds and not saw_activity:
                await self.db.update_work_execution(
                    work["id"],
                    execution_state="stalled",
                    last_agent_status=status,
                    failure_class="no_agent_activity",
                )
                await self.db.mark_worker_degraded_for_work(work["id"])
                await self.db.add_event(
                    "execution.stalled",
                    f"{work['id']} has no observed agent activity",
                    severity="warning",
                    server_id=work["server_id"],
                    data={"work_id": work["id"], "pane": pane.get("pane_id")},
                )

    async def _recover(self, work: dict[str, Any]) -> None:
        if int(work.get("recovery_count") or 0) >= self.settings.studio.execution_max_recoveries:
            await self.db.update_work_execution(
                work["id"], execution_state="stalled", failure_class="recovery_exhausted"
            )
            await self.db.mark_worker_degraded_for_work(work["id"])
            return
        if self.herdr.snapshot.get("status") == "down":
            return
        pane = self._pane_for_work(work)
        if not pane:
            return
        already_dispatched = bool(work.get("dispatched_at"))
        attempts = int(work.get("dispatch_attempts") or 0)
        if already_dispatched:
            next_state = "waiting_agent"
            message = f"{work['id']} rebound to {pane.get('pane_id')} without redispatch"
            failure_class = None
        elif attempts == 0:
            next_state = "assigned"
            message = f"{work['id']} target recovered before first dispatch"
            failure_class = None
        else:
            next_state = "stalled"
            message = f"{work['id']} connectivity recovered; explicit redispatch is required"
            failure_class = "redispatch_required"

        item = await self.db.update_work_execution(
            work["id"],
            execution_state=next_state,
            pane=pane.get("pane_id"),
            agent=work.get("agent") or pane.get("agent"),
            last_agent_status=str(pane.get("agent_status") or "unknown"),
            failure_class=failure_class,
            execution_patch={"recovered_at": _iso(), "recovered_pane": pane.get("pane_id")},
            increment_recovery=True,
        )
        self.snapshot["recoveries"] += 1
        await self.db.add_event(
            "execution.recovered",
            message,
            severity="warning" if next_state == "stalled" else "info",
            server_id=work["server_id"],
            data={"work_id": work["id"], "pane": pane.get("pane_id"), "recovery_count": item.get("recovery_count"), "execution_state": next_state},
        )

    async def _process_one(self, work: dict[str, Any], upstream_snapshot: dict[str, Any]) -> bool:
        state = work.get("execution_state") or "none"
        fault = self._consume_fault(work)
        if fault:
            await self.db.update_work_execution(
                work["id"],
                execution_state="reconnecting",
                failure_class=f"fault_{fault}",
                execution_patch={"fault_at": _iso(), "fault_kind": fault},
            )
            await self.db.mark_worker_degraded_for_work(work["id"])
            await self.db.add_event(
                "resilience.fault_observed",
                f"{work['id']}: supervisor observed simulated {fault}",
                severity="warning",
                server_id=work["server_id"],
                data={"work_id": work["id"], "kind": fault},
            )
            return True

        if state in {"none", "assigned"}:
            if self.settings.studio.execution_enabled:
                await self.dispatch(work["id"], explicit=False, refresh=False)
                return True
            return False
        if state == "waiting_agent":
            if upstream_snapshot.get("status") == "down":
                await self.db.update_work_execution(
                    work["id"], execution_state="reconnecting", failure_class="mcp_transport"
                )
                await self.db.mark_worker_degraded_for_work(work["id"])
            else:
                await self._monitor_waiting(work)
            return True
        if state in {"reconnecting", "recovering"}:
            await self._recover(work)
            return True
        if state in {"stalled", "dispatch_uncertain"}:
            return False
        return False

    async def run_once(self) -> dict[str, Any]:
        if self._run_lock.locked():
            return dict(self.snapshot)
        async with self._run_lock:
            await self.db.heartbeat_running_work()
            snapshot = await self._ensure_fresh_herdr()
            running = await self.db.running_work()
            herdr_work = [w for w in running if w.get("dispatch_mode") == "herdr"]
            sem = asyncio.Semaphore(max(1, int(self.settings.studio.execution_parallelism)))

            async def one(work: dict[str, Any]) -> bool:
                async with sem:
                    try:
                        return await self._process_one(work, snapshot)
                    except Exception as exc:
                        await self.db.add_event(
                            "execution.item_error",
                            f"{work['id']}: {exc}",
                            severity="warning",
                            server_id=work["server_id"],
                            data={"work_id": work["id"]},
                        )
                        return False

            results = await asyncio.gather(*(one(w) for w in herdr_work)) if herdr_work else []
            processed = sum(1 for x in results if x)
            self.snapshot.update(
                {
                    "status": "healthy" if snapshot.get("status") != "down" else "degraded",
                    "last_tick_at": _iso(),
                    "last_error": snapshot.get("error") if snapshot.get("status") == "down" else None,
                    "ticks": int(self.snapshot.get("ticks") or 0) + 1,
                    "running_herdr_work": len(herdr_work),
                    "processed": processed,
                    "active_faults": len(self._faults),
                }
            )
            return dict(self.snapshot)

    async def retry_dispatch(self, work_id: str, *, acknowledge_duplicate_risk: bool) -> dict[str, Any]:
        work = await self.db.get_work(work_id)
        if work.get("execution_state") == "dispatch_uncertain" and not acknowledge_duplicate_risk:
            raise ValueError("dispatch outcome is uncertain; acknowledge_duplicate_risk=true is required")
        return await self.dispatch(work_id, explicit=True, allow_redispatch=True)

    async def recover_work(self, work_id: str) -> dict[str, Any]:
        await self._ensure_fresh_herdr()
        work = await self.db.get_work(work_id)
        await self._recover(work)
        return await self.db.get_work(work_id)
