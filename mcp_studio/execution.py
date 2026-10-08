from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, cast

import httpx

from .db import Database, PStackPreDispatchBlocked
from .herdr import HerdrManager, _decode_text_content
from .settings import Settings
from .execution_environments import close_execution_environment


TERMINAL_AGENT_STATES = {"idle", "done"}
ACTIVE_AGENT_STATES = {"busy", "running", "working", "thinking", "executing", "active"}



_RESULT_MARKER = re.compile(
    r"(?im)^\s*HIRDA_TASK_RESULT:\s*(success|blocked|failed)\s*$"
)
_MODEL_PATTERNS = (
    re.compile(r"\b(cx/[A-Za-z0-9._-]+(?:\[[^\]\s]+\])?)", re.I),
    re.compile(r"\b(GPT-[A-Za-z0-9._-]+(?:\[[^\]\s]+\])?)", re.I),
    re.compile(r"\b(claude-[A-Za-z0-9._-]+)\b", re.I),
)
_PENDING_PROMPTS = (
    ("onboarding", re.compile(r"Teach auto mode about your environment\?", re.I)),
    ("auth", re.compile(r"No access token found|Login required|re-authenticate", re.I)),
    ("trust", re.compile(r"Trust this folder\?", re.I)),
    ("approval", re.compile(r"Approve(?: this)?(?: tool| action)?\?", re.I)),
    ("confirmation", re.compile(r"Enter to confirm\s*[·•]\s*Esc to cancel", re.I)),
)
_TOOL_FAILURE = re.compile(
    r"repository tools are unavailable|tools are unavailable|session model is temporarily unavailable",
    re.I,
)


def _decoded_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("result", "text", "output"):
            item = value.get(key)
            if isinstance(item, str):
                return item
        return "\n".join(
            text
            for text in (_decoded_text(item) for item in value.values())
            if text
        )
    if isinstance(value, (list, tuple)):
        return "\n".join(
            text for text in (_decoded_text(item) for item in value) if text
        )
    return ""


def _parse_runtime_readiness(
    runtime_id: str,
    decoded: Any,
    *,
    pane: dict[str, Any] | None = None,
    require_auto_edit: bool = False,
    expected_model: str | None = None,
    expected_permission_mode: str | None = None,
) -> dict[str, Any]:
    text = _decoded_text(decoded)
    model = None
    for pattern in _MODEL_PATTERNS:
        match = pattern.search(text)
        if match:
            model = match.group(1)
            break

    effort = None
    for pattern in (
        re.compile(r"with\s+(extra high|high|medium|low)\s+effort", re.I),
        re.compile(r"Reasoning effort:\s*(extra high|high|medium|low)", re.I),
        re.compile(r"GPT-[A-Za-z0-9._-]+(?:\[[^\]\s]+\])?\s+(extra high|high|medium|low)\s*[·•]", re.I),
    ):
        match = pattern.search(text)
        if match:
            effort = match.group(1).casefold()
            break

    mode = None
    permission_mode = None
    auto_mode_enabled = None
    mode_match = re.search(r"\b(auto|manual) mode on\b", text, re.I)
    accept_edits_match = re.search(r"\baccept edits on\b", text, re.I)
    if mode_match:
        mode = mode_match.group(1).casefold()
        permission_mode = mode
        auto_mode_enabled = mode == "auto"
    elif accept_edits_match:
        mode = "acceptEdits"
        permission_mode = "acceptEdits"
        auto_mode_enabled = False

    auto_edit_enabled = None
    auto_edit_source = None
    explicit_auto_edit = re.search(
        r"\bauto edit\s*(?:=|:|is)?\s*(on|off|enabled|disabled)\b",
        text,
        re.I,
    )
    if explicit_auto_edit:
        auto_edit_enabled = explicit_auto_edit.group(1).casefold() in {"on", "enabled"}
        auto_edit_source = "explicit"
    elif permission_mode == "acceptEdits" and runtime_id == "claude":
        auto_edit_enabled = True
        auto_edit_source = "claude_accept_edits"
    elif auto_mode_enabled is not None and runtime_id == "claude":
        auto_edit_enabled = auto_mode_enabled
        auto_edit_source = "claude_auto_mode"

    pending_prompt = None
    for kind, pattern in _PENDING_PROMPTS:
        if pattern.search(text):
            pending_prompt = kind
            break

    tools_ready: bool | None = None
    if _TOOL_FAILURE.search(text):
        tools_ready = False
    elif text:
        tools_ready = True

    pane = pane or {}
    pane_interactive = pane.get("interactive_ready")
    interactive_ready = (
        bool(pane_interactive)
        if pane_interactive is not None
        else True
    )
    if pending_prompt:
        interactive_ready = False

    expected_key = str(expected_model or "").strip().casefold()
    observed_key = str(model or "").strip().casefold()
    model_matches = not expected_key or observed_key == expected_key
    expected_permission_key = str(
        expected_permission_mode or ""
    ).strip().casefold()
    observed_permission_key = str(
        permission_mode or ""
    ).strip().casefold()
    permission_mode_matches = (
        not expected_permission_key
        or observed_permission_key == expected_permission_key
    )

    reasons: list[str] = []
    if not model:
        reasons.append("model_unobserved")
    if not model_matches:
        reasons.append("model_mismatch")
    if not permission_mode_matches:
        reasons.append("permission_mode_mismatch")
    if pending_prompt:
        reasons.append(f"pending_prompt:{pending_prompt}")
    if not interactive_ready:
        reasons.append("interactive_not_ready")
    if tools_ready is False:
        reasons.append("tools_not_ready")
    if require_auto_edit and auto_edit_enabled is not True:
        reasons.append("auto_edit_not_ready")

    return {
        "schema": "hirda-runtime-readiness-v1",
        "runtime": str(runtime_id or "").strip().casefold() or None,
        "model": model,
        "expected_model": expected_model,
        "model_matches": model_matches,
        "reasoning_effort": effort,
        "mode": mode,
        "permission_mode": permission_mode,
        "expected_permission_mode": expected_permission_mode,
        "permission_mode_matches": permission_mode_matches,
        "auto_mode_enabled": auto_mode_enabled,
        "auto_edit_enabled": auto_edit_enabled,
        "auto_edit_source": auto_edit_source,
        "interactive_ready": interactive_ready,
        "pending_prompt": pending_prompt,
        "tools_ready": tools_ready,
        "require_auto_edit": bool(require_auto_edit),
        "ready": not reasons,
        "reasons": reasons,
    }


def _semantic_completion(decoded: Any) -> dict[str, Any]:
    text = _decoded_text(decoded)
    marker = _RESULT_MARKER.search(text)
    status = marker.group(1).casefold() if marker else None
    pending_prompt = None
    for kind, pattern in _PENDING_PROMPTS:
        if pattern.search(text):
            pending_prompt = kind
            break
    tool_failure = bool(_TOOL_FAILURE.search(text))
    if pending_prompt:
        return {
            "status": "blocked",
            "reason": f"pending_prompt:{pending_prompt}",
            "marker": status,
        }
    if tool_failure:
        return {
            "status": "blocked",
            "reason": "tools_not_ready",
            "marker": status,
        }
    if status in {"success", "blocked", "failed"}:
        return {
            "status": status,
            "reason": None if status == "success" else f"agent_reported_{status}",
            "marker": status,
        }
    return {
        "status": "blocked",
        "reason": "completion_marker_missing",
        "marker": None,
    }


_RESULT_CONTRACT = """
HIRDA EXECUTION RESULT CONTRACT:
- Before becoming idle or done, end your final response with exactly one machine-readable line.
- Use HIRDA_TASK_RESULT: success only when the requested task is actually complete.
- Use HIRDA_TASK_RESULT: blocked when an approval, authentication, onboarding prompt, unavailable tool/model, or other dependency prevents completion.
- Use HIRDA_TASK_RESULT: failed when execution failed and cannot safely continue.
- Never report success merely because you reached an idle/done state.
""".strip()

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
        self._recovery_poll_after: dict[str, float] = {}
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

    @staticmethod
    def _pstack_execution_metadata(work: dict[str, Any]) -> dict[str, Any]:
        metadata = work.get("metadata")
        if not isinstance(metadata, dict):
            return {}
        if not str(metadata.get("pstack_task_id") or "").strip():
            return {}
        return metadata


    @staticmethod
    def _decoded_dispatch_failure(decoded: Any) -> tuple[str, str, str] | None:
        """Return (failure_class, execution_state, message) for rejected HERDR calls."""

        def find_error(value: Any) -> tuple[str, str] | None:
            if isinstance(value, dict):
                # Prefer nested machine-readable errors (for example
                # stderr.error.code=agent_not_found) over a generic wrapper.
                for key in ("stderr", "details", "data", "result"):
                    if key in value:
                        nested = find_error(value.get(key))
                        if nested is not None:
                            return nested
                error = value.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or error.get("error") or "").strip()
                    message = str(
                        error.get("message")
                        or value.get("message")
                        or code
                    ).strip()
                    if code:
                        return code, message
                elif error:
                    code = str(error).strip()
                    message = str(value.get("message") or code).strip()
                    return code, message
                for child in value.values():
                    nested = find_error(child)
                    if nested is not None:
                        return nested
            elif isinstance(value, list):
                for child in value:
                    nested = find_error(child)
                    if nested is not None:
                        return nested
            return None

        found = find_error(decoded)
        if found is None:
            return None
        code, message = found
        normalized = code.casefold()
        if normalized in {"agent_not_found", "pane_not_found"}:
            return "pane_missing", "reconnecting", message
        return "herdr_command_failed", "recovering", message

    async def _patch_pstack_from_work(
        self,
        work: dict[str, Any],
        *,
        status: str,
        execution_state: str,
    ) -> None:
        metadata = self._pstack_execution_metadata(work)
        task_id = str(metadata.get("pstack_task_id") or "").strip()
        trace_id = str(metadata.get("pstack_trace_id") or "").strip()
        if not task_id:
            return

        patch: dict[str, Any] = {
            "work_id": work.get("id"),
            "status": status,
            "execution_state": execution_state,
        }
        status_key = str(status or "").strip().casefold()
        execution_key = str(execution_state or "").strip().casefold()
        if status_key in {"completed", "expired", "cancelled", "failed"} or execution_key in {
            "completed",
            "cancelled",
            "failed",
        }:
            tab = metadata.get("pstack_herdr_tab")
            tab = dict(tab) if isinstance(tab, dict) else {}
            if (
                tab
                and str(tab.get("owner") or "").casefold() == "pstack"
                and not bool(tab.get("persistent"))
                and str(tab.get("status") or "").casefold()
                not in {"closed", "unavailable"}
            ):
                try:
                    closed_tab = await self.herdr.close_task_tab(tab)
                except Exception as exc:
                    closed_tab = {
                        **tab,
                        "status": "close_failed",
                        "close_error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                    if trace_id:
                        await self.db.add_event(
                            "pstack.herdr_tab.close_failed",
                            "PStack HERDR task tab could not be closed after terminal execution.",
                            severity="warning",
                            data={
                                "task_id": task_id,
                                "trace_id": trace_id,
                                "work_id": work.get("id"),
                                "tab_id": tab.get("tab_id"),
                                "error": closed_tab["close_error"],
                            },
                            stream_id=f"pstack:{trace_id}",
                            correlation_id=trace_id,
                        )
                else:
                    if trace_id:
                        await self.db.add_event(
                            "pstack.herdr_tab.closed",
                            "PStack HERDR task tab closed after terminal execution.",
                            data={
                                "task_id": task_id,
                                "trace_id": trace_id,
                                "work_id": work.get("id"),
                                "tab_id": closed_tab.get("tab_id"),
                                "tab_label": closed_tab.get("label"),
                            },
                            stream_id=f"pstack:{trace_id}",
                            correlation_id=trace_id,
                        )
                patch["herdr_tab"] = closed_tab
                environment = metadata.get("pstack_execution_environment")
                environment = dict(environment) if isinstance(environment, dict) else {}
                if str(closed_tab.get("status") or "").casefold() == "closed":
                    environment = close_execution_environment(environment, closed_tab)
                elif environment and str(closed_tab.get("status") or "").casefold() == "close_failed":
                    environment = {
                        **environment,
                        "status": "close_failed",
                        "error": closed_tab.get("close_error"),
                    }
                if environment:
                    patch["execution_environment"] = environment

        await self.db.patch_pstack_task(task_id, patch)
        if trace_id:
            await self.db.add_event(
                "pstack.execution.state",
                f"PStack task {task_id} execution state: {status}",
                data={
                    "task_id": task_id,
                    "trace_id": trace_id,
                    "work_id": work.get("id"),
                    "actor": "hirda",
                    "phase": f"execution_{status}",
                    "title": f"Execution {status}",
                    "detail": (
                        f"Scheduler work {work.get('id')} is {status} "
                        f"({execution_state})."
                    ),
                    "status": status,
                    "trace_status": status,
                    "metadata": {
                        "execution_state": execution_state,
                    },
                },
                stream_id=f"pstack:{trace_id}",
                correlation_id=trace_id,
            )

    async def _ensure_runtime_agent(
        self,
        work: dict[str, Any],
        pane: dict[str, Any],
    ) -> dict[str, Any]:
        """Ensure the HIRDA-selected runtime is live in the task-scoped pane."""
        runtime_id = str(
            work.get("agent")
            or work.get("requested_agent")
            or ""
        ).strip().casefold()
        if not runtime_id:
            return pane

        observed = str(pane.get("agent") or "").strip().casefold()
        if observed == runtime_id:
            return pane
        if observed and observed != runtime_id:
            raise RuntimeError(
                "task pane is already owned by a different runtime: "
                f"{observed} != {runtime_id}"
            )

        starter = getattr(self.herdr, "start_agent_in_pane", None)
        if not callable(starter):
            raise RuntimeError("herdr runtime bootstrap is unavailable")

        metadata = self._pstack_execution_metadata(work)
        task_id = str(metadata.get("pstack_task_id") or "").strip()
        requirements = metadata.get("runtime_requirements")
        requirements = requirements if isinstance(requirements, dict) else {}
        agent_name = (
            f"pstack-{task_id.casefold()}"
            if task_id
            else f"work-{str(work.get('id') or '').removeprefix('work-')[:12]}"
        )

        agent_args: list[str] = []
        launch_contract: dict[str, Any] = {
            "runtime": runtime_id,
            "authority": "hirda",
        }
        if runtime_id == "claude" and task_id:
            permission_mode = str(
                requirements.get("permission_mode")
                or "acceptEdits"
            ).strip()
            if permission_mode:
                agent_args.extend(["--permission-mode", permission_mode])
                launch_contract["permission_mode"] = permission_mode
            expected_model = str(requirements.get("model") or "").strip()
            if expected_model:
                agent_args.extend(["--model", expected_model])
                launch_contract["model"] = expected_model
            effort = str(
                requirements.get("reasoning_effort")
                or requirements.get("effort")
                or ""
            ).strip()
            if effort:
                agent_args.extend(["--effort", effort])
                launch_contract["reasoning_effort"] = effort

        starter_fn = cast(Callable[..., Awaitable[dict[str, Any]]], starter)
        started = await starter_fn(
            name=agent_name[:64],
            kind=runtime_id,
            pane_id=str(pane.get("pane_id") or ""),
            timeout_ms=30000,
            agent_args=agent_args,
        )
        refreshed = self._pane_for_work(work)
        if not refreshed:
            raise RuntimeError("task pane disappeared after runtime bootstrap")
        detected = str(refreshed.get("agent") or "").strip().casefold()
        if detected != runtime_id:
            raise RuntimeError(
                "HIRDA-selected runtime did not become ready in task pane: "
                f"expected {runtime_id}, observed {detected or 'none'}"
            )
        await self.db.update_work_execution(
            work["id"],
            execution_patch={
                "runtime_launch_contract": launch_contract,
                "runtime_launch_observed": {
                    "agent_session": started.get("agent_session"),
                    "agent_status": started.get("agent_status"),
                    "agent_args": started.get("agent_args", agent_args),
                },
            },
        )
        await self.db.add_event(
            "execution.agent_started",
            f"{work['id']} started {runtime_id} in {refreshed.get('pane_id')}",
            server_id=work["server_id"],
            data={
                "work_id": work["id"],
                "runtime": runtime_id,
                "pane": refreshed.get("pane_id"),
                "authority": "hirda",
                "launch_contract": launch_contract,
            },
        )
        return refreshed


    async def _probe_runtime_readiness(
        self,
        work: dict[str, Any],
        pane: dict[str, Any],
    ) -> dict[str, Any]:
        runtime_id = str(
            work.get("agent")
            or work.get("requested_agent")
            or pane.get("agent")
            or ""
        ).strip().casefold()
        metadata = self._pstack_execution_metadata(work)
        requirements = metadata.get("runtime_requirements")
        requirements = requirements if isinstance(requirements, dict) else {}
        runtime_selection = metadata.get("runtime_selection")
        runtime_selection = (
            runtime_selection if isinstance(runtime_selection, dict) else {}
        )
        expected_model = (
            str(
                requirements.get("model")
                or runtime_selection.get("model")
                or ""
            ).strip()
            or None
        )
        explicit_auto_edit = requirements.get("auto_edit")
        require_auto_edit = (
            bool(explicit_auto_edit)
            if explicit_auto_edit is not None
            else bool(metadata.get("pstack_task_id") and runtime_id == "claude")
        )
        expected_permission_mode = (
            str(requirements.get("permission_mode") or "").strip()
            or (
                "acceptEdits"
                if metadata.get("pstack_task_id") and runtime_id == "claude"
                else None
            )
        )

        tool = self.herdr.tool("herdr_read_agent")
        decoded: Any = None
        if not tool:
            readiness = _parse_runtime_readiness(
                runtime_id,
                decoded,
                pane=pane,
                require_auto_edit=require_auto_edit,
                expected_model=expected_model,
                expected_permission_mode=expected_permission_mode,
            )
            readiness["reasons"].append("readiness_probe_unavailable")
            readiness["ready"] = False
            return readiness

        last: dict[str, Any] | None = None
        for attempt in range(6):
            args = build_tool_arguments(
                tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=work.get("agent") or pane.get("agent"),
            )
            try:
                raw = await self.herdr.client().call_tool(
                    "herdr_read_agent",
                    args,
                    client_name="mcp-studio-runtime-readiness",
                )
                decoded = _decode_text_content(raw)
            except Exception as exc:
                decoded = {"read_error": str(exc)}
            last = _parse_runtime_readiness(
                runtime_id,
                decoded,
                pane=pane,
                require_auto_edit=require_auto_edit,
                expected_model=expected_model,
                expected_permission_mode=expected_permission_mode,
            )
            last["attempt"] = attempt + 1
            if last["ready"]:
                break
            hard_reasons = {
                reason
                for reason in last["reasons"]
                if reason != "model_unobserved"
            }
            if hard_reasons:
                break
            await asyncio.sleep(0.5)

        readiness = last or _parse_runtime_readiness(
            runtime_id,
            decoded,
            pane=pane,
            require_auto_edit=require_auto_edit,
            expected_model=expected_model,
            expected_permission_mode=expected_permission_mode,
        )
        await self.db.update_work_execution(
            work["id"],
            execution_patch={"runtime_readiness": readiness},
        )
        await self.db.add_event(
            "execution.runtime_readiness",
            (
                f"{work['id']} runtime readiness "
                f"{'passed' if readiness.get('ready') else 'blocked'}"
            ),
            severity="info" if readiness.get("ready") else "warning",
            server_id=work["server_id"],
            data={
                "work_id": work["id"],
                "pane": pane.get("pane_id"),
                "runtime": runtime_id,
                "model": readiness.get("model"),
                "reasoning_effort": readiness.get("reasoning_effort"),
                "mode": readiness.get("mode"),
                "permission_mode": readiness.get("permission_mode"),
                "auto_edit_enabled": readiness.get("auto_edit_enabled"),
                "pending_prompt": readiness.get("pending_prompt"),
                "ready": readiness.get("ready"),
                "reasons": readiness.get("reasons"),
            },
        )
        return readiness

    async def _reject_stale_pstack_work(
        self, work: dict[str, Any], gate: dict[str, Any],
    ) -> None:
        """Never send a prompt to a PStack task without active authority."""
        work_id = work["id"]
        current = await self.db.get_work(work_id)
        was_dispatched = bool(
            int(current.get("dispatch_attempts") or 0)
            or current.get("dispatched_at")
        )
        if current.get("state") == "running" and not was_dispatched:
            # No remote prompt was sent, so it is safe to return the logical
            # worker lease. This is not evidence that the PStack task passed.
            try:
                await self.db.finish_work(
                    work_id, state="cancelled",
                    error=f"Pre-dispatch PStack guard: {gate['reason']}",
                )
            except ValueError:
                pass
        await self.db.add_event(
            "pstack.predispatch.blocked",
            f"{work_id} withheld from HERDR prompt: {gate['reason']}",
            severity="warning", server_id=work["server_id"],
            data={"work_id": work_id, "task_id": gate.get("task_id"),
                  "reason": gate["reason"], "prior_dispatch":was_dispatched},
        )
        await self.db.add_audit(
            "pstack.predispatch.blocked",
            actor="hirda/execution", target_type="work",
            target_id=work_id,
            data={"task_id": gate.get("task_id"), "reason":gate["reason"],
                  "prior_dispatch":was_dispatched},
        )

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

            checker = getattr(self.db, "pstack_pre_dispatch_gate", None)
            if callable(checker):
                admission = await checker(work_id)
                if not admission["allowed"]:
                    await self._reject_stale_pstack_work(work, admission)
                    raise PStackPreDispatchBlocked(
                        admission["task_id"], admission["reason"]
                    )

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
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state="stalled",
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
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state="reconnecting",
                )
                raise RuntimeError("target pane is not currently discoverable")

            try:
                pane = await self._ensure_runtime_agent(work, pane)
            except Exception as exc:
                await self.db.update_work_execution(
                    work_id,
                    execution_state="recovering",
                    failure_class="agent_start_failed",
                    execution_patch={"agent_start_error": str(exc)[:1000]},
                )
                await self.db.mark_worker_degraded_for_work(work_id)
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state="recovering",
                )
                await self.db.add_event(
                    "execution.agent_start_failed",
                    f"{work_id} could not bootstrap the selected runtime: {exc}",
                    severity="warning",
                    server_id=work["server_id"],
                    data={
                        "work_id": work_id,
                        "runtime": work.get("agent") or work.get("requested_agent"),
                        "pane": pane.get("pane_id"),
                    },
                )
                raise

            readiness = await self._probe_runtime_readiness(work, pane)
            if not readiness.get("ready"):
                await self.db.update_work_execution(
                    work_id,
                    execution_state="stalled",
                    failure_class="runtime_not_ready",
                    execution_patch={"runtime_readiness": readiness},
                )
                await self.db.mark_worker_degraded_for_work(work_id)
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state="stalled",
                )
                await self.db.add_event(
                    "execution.runtime_not_ready",
                    f"{work_id} runtime readiness gate blocked dispatch.",
                    severity="warning",
                    server_id=work["server_id"],
                    data={
                        "work_id": work_id,
                        "runtime": readiness.get("runtime"),
                        "model": readiness.get("model"),
                        "reasons": readiness.get("reasons"),
                        "pending_prompt": readiness.get("pending_prompt"),
                        "permission_mode": readiness.get("permission_mode"),
                    },
                )
                raise RuntimeError(
                    "runtime readiness gate failed: "
                    + ",".join(readiness.get("reasons") or ["unknown"])
                )

            dispatch_instruction = f"{instruction}\n\n{_RESULT_CONTRACT}"
            args = build_tool_arguments(
                tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=work.get("agent") or pane.get("agent"),
                prompt=dispatch_instruction,
            )
            baseline = {
                "pane_id": pane.get("pane_id"),
                "revision": pane.get("revision"),
                "state_change_seq": pane.get("state_change_seq"),
                "agent_status": pane.get("agent_status"),
                "saw_activity": False,
                "dispatch_args": {k: "<instruction>" if v == dispatch_instruction else v for k, v in args.items()},
            }
            try:
                async with self._dispatch_slot():
                    # Re-check immediately before dispatch, after readiness
                    # checks that may have taken time or launched an agent.
                    if callable(checker):
                        admission = await checker(work_id)
                        if not admission["allowed"]:
                            await self._reject_stale_pstack_work(work, admission)
                            raise PStackPreDispatchBlocked(
                                admission["task_id"], admission["reason"]
                            )
                    try:
                        # SQLite checks canonical PStack again in this same
                        # atomic dispatch-attempt transaction.
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
                    except PStackPreDispatchBlocked as exc:
                        await self._reject_stale_pstack_work(
                            work, {"task_id": exc.task_id, "reason": exc.reason}
                        )
                        raise
                    result = await self.herdr.client().call_tool(
                        "herdr_prompt_agent",
                        args,
                        client_name=f"mcp-studio-{work.get('worker_id') or 'worker'}",
                    )
            except PStackPreDispatchBlocked:
                # No prompt was sent. Do not misclassify policy denial as an
                # unknown remote dispatch and do not attempt recovery.
                raise
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
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state=state,
                )
                raise

            decoded = _decode_text_content(result)
            rejected = self._decoded_dispatch_failure(decoded)
            if rejected is not None:
                failure_class, execution_state, message = rejected
                item = await self.db.update_work_execution(
                    work_id,
                    execution_state=execution_state,
                    last_agent_status=str(pane.get("agent_status") or "unknown"),
                    failure_class=failure_class,
                    execution_patch={
                        "dispatch_result": decoded,
                        "dispatch_rejected": True,
                        "safe_redispatch": failure_class == "pane_missing",
                    },
                )
                await self.db.mark_worker_degraded_for_work(work_id)
                await self._patch_pstack_from_work(
                    item,
                    status="blocked",
                    execution_state=execution_state,
                )
                await self.db.add_event(
                    "execution.dispatch_failed",
                    f"{work_id} dispatch rejected ({failure_class}): {message}",
                    severity="warning",
                    server_id=work["server_id"],
                    data={
                        "work_id": work_id,
                        "failure_class": failure_class,
                        "pane": pane.get("pane_id"),
                        "decoded": decoded,
                    },
                )
                raise RuntimeError(
                    f"HERDR rejected dispatch ({failure_class}): {message}"
                )

            item = await self.db.update_work_execution(
                work_id,
                execution_state="waiting_agent",
                last_agent_status=str(pane.get("agent_status") or "unknown"),
                failure_class=None,
                execution_patch={"dispatch_result": decoded},
                mark_dispatched=True,
            )
            self.snapshot["dispatches"] += 1
            await self._patch_pstack_from_work(
                item,
                status="running",
                execution_state="waiting_agent",
            )
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

    async def _complete_from_agent(
        self,
        work: dict[str, Any],
        pane: dict[str, Any],
    ) -> dict[str, Any]:
        output = await self._try_read_result(work, pane)
        semantic = _semantic_completion(output)
        execution = dict(work.get("execution") or {})
        result = {
            "source": "herdr",
            "pane": pane.get("pane_id"),
            "agent": pane.get("agent") or work.get("agent"),
            "agent_status": pane.get("agent_status"),
            "revision": pane.get("revision"),
            "runtime_readiness": execution.get("runtime_readiness"),
            "semantic_completion": semantic,
            "output": output,
        }

        if semantic.get("status") == "success":
            item = await self.db.finish_work(
                work["id"],
                state="completed",
                result=result,
            )
            await self._patch_pstack_from_work(
                item,
                status="verification_required",
                execution_state="completed",
            )
            self.snapshot["completed"] += 1
            await self.db.add_event(
                "execution.completed",
                f"{work['id']} completed on {pane.get('pane_id')}",
                server_id=work["server_id"],
                data={
                    "work_id": work["id"],
                    "worker_id": work.get("worker_id"),
                    "pane": pane.get("pane_id"),
                    "semantic_completion": semantic,
                },
            )
            self.scheduler.kick()
            return item

        reason = str(semantic.get("reason") or "semantic_completion_failed")
        item = await self.db.finish_work(
            work["id"],
            state="failed",
            result=result,
            error=reason,
        )
        await self._patch_pstack_from_work(
            item,
            status="blocked",
            execution_state="failed",
        )
        await self.db.add_event(
            "execution.semantic_completion_failed",
            f"{work['id']} did not satisfy semantic completion: {reason}",
            severity="warning",
            server_id=work["server_id"],
            data={
                "work_id": work["id"],
                "worker_id": work.get("worker_id"),
                "pane": pane.get("pane_id"),
                "semantic_completion": semantic,
            },
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
            await self._patch_pstack_from_work(
                work,
                status="blocked",
                execution_state="reconnecting",
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
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state="stalled",
                )

    async def _recover(self, work: dict[str, Any]) -> None:
        if int(work.get("recovery_count") or 0) >= self.settings.studio.execution_max_recoveries:
            await self.db.update_work_execution(
                work["id"], execution_state="stalled", failure_class="recovery_exhausted"
            )
            await self.db.mark_worker_degraded_for_work(work["id"])
            await self._patch_pstack_from_work(
                work,
                status="blocked",
                execution_state="stalled",
            )
            return
        if self.herdr.snapshot.get("status") == "down":
            return
        pane = self._pane_for_work(work)
        if not pane:
            return
        already_dispatched = bool(work.get("dispatched_at"))
        attempts = int(work.get("dispatch_attempts") or 0)
        execution = dict(work.get("execution") or {})
        safe_redispatch = execution.get("safe_redispatch") is True
        if already_dispatched:
            next_state = "waiting_agent"
            message = f"{work['id']} rebound to {pane.get('pane_id')} without redispatch"
            failure_class = None
        elif attempts == 0:
            next_state = "assigned"
            message = f"{work['id']} target recovered before first dispatch"
            failure_class = None
        elif safe_redispatch:
            next_state = "assigned"
            message = (
                f"{work['id']} target recovered after a confirmed rejected dispatch; "
                "safe redispatch is allowed"
            )
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
        await self._patch_pstack_from_work(
            item,
            status=(
                "blocked"
                if next_state == "stalled"
                else "running"
                if next_state == "waiting_agent"
                else "waiting"
            ),
            execution_state=next_state,
        )

    def _poll_interval_for_work(self, work: dict[str, Any]) -> float | None:
        """Return the minimum supervisor polling interval for actionable Herdr work."""
        state = str(work.get("execution_state") or "none")
        if state in {"stalled", "dispatch_uncertain"}:
            return None
        if state in {"none", "assigned"}:
            metadata = self._pstack_execution_metadata(work)
            if not (
                self.settings.studio.execution_enabled
                or metadata.get("pstack_auto_dispatch") is True
            ):
                return None
            return 0.0
        if state == "waiting_agent":
            return 0.0
        if state in {"reconnecting", "recovering"}:
            return max(
                10.0,
                float(getattr(self.settings.studio, "herdr_poll_interval_seconds", 30.0)),
            )
        return None

    def _work_poll_eligible(self, work: dict[str, Any]) -> bool:
        interval = self._poll_interval_for_work(work)
        work_id = str(work.get("id") or "")
        state = str(work.get("execution_state") or "none")
        if interval is None:
            self._recovery_poll_after.pop(work_id, None)
            return False
        if state not in {"reconnecting", "recovering"}:
            self._recovery_poll_after.pop(work_id, None)
            return True

        now = time.monotonic()
        next_poll = self._recovery_poll_after.get(work_id, 0.0)
        if now < next_poll:
            return False
        self._recovery_poll_after[work_id] = now + interval
        return True

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
            await self._patch_pstack_from_work(
                work,
                status="blocked",
                execution_state="reconnecting",
            )
            return True

        if state in {"none", "assigned"}:
            metadata = self._pstack_execution_metadata(work)
            pstack_auto_dispatch = metadata.get("pstack_auto_dispatch") is True
            if self.settings.studio.execution_enabled or pstack_auto_dispatch:
                execution = dict(work.get("execution") or {})
                await self.dispatch(
                    work["id"],
                    explicit=(
                        pstack_auto_dispatch
                        and not self.settings.studio.execution_enabled
                    ),
                    allow_redispatch=(
                        int(work.get("dispatch_attempts") or 0) > 0
                        and execution.get("safe_redispatch") is True
                    ),
                    refresh=False,
                )
                return True
            return False
        if state == "waiting_agent":
            if upstream_snapshot.get("status") == "down":
                await self.db.update_work_execution(
                    work["id"], execution_state="reconnecting", failure_class="mcp_transport"
                )
                await self.db.mark_worker_degraded_for_work(work["id"])
                await self._patch_pstack_from_work(
                    work,
                    status="blocked",
                    execution_state="reconnecting",
                )
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
            running = await self.db.running_work()
            herdr_work = [w for w in running if w.get("dispatch_mode") == "herdr"]
            running_ids = {str(w.get("id") or "") for w in herdr_work}
            for work_id in tuple(self._recovery_poll_after):
                if work_id not in running_ids:
                    self._recovery_poll_after.pop(work_id, None)
            polling_work = [w for w in herdr_work if self._work_poll_eligible(w)]
            refresh_skipped = not polling_work
            snapshot = (
                dict(self.herdr.snapshot)
                if refresh_skipped
                else await self._ensure_fresh_herdr()
            )
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

            results = await asyncio.gather(*(one(w) for w in polling_work)) if polling_work else []
            processed = sum(1 for x in results if x)
            self.snapshot.update(
                {
                    "status": "healthy" if snapshot.get("status") != "down" else "degraded",
                    "last_tick_at": _iso(),
                    "last_error": snapshot.get("error") if snapshot.get("status") == "down" else None,
                    "ticks": int(self.snapshot.get("ticks") or 0) + 1,
                    "running_herdr_work": len(herdr_work),
                    "polled_herdr_work": len(polling_work),
                    "herdr_refresh_skipped": refresh_skipped,
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
