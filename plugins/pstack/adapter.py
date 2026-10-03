from __future__ import annotations

import re
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp_studio.integrations import IntegrationManifest


PSTACK_REVISION = "c02fd4922b25ee005f42042463d741d236c2c35e"
PSTACK_REPOSITORY = "michael-denyer/pstack-claude"

_PLAYBOOK_WORKFLOWS = (
    "authoring-a-skill",
    "autonomous-run",
    "autopilot-full",
    "autopilot-stack",
    "babysit",
    "bug-fix",
    "eval",
    "feature",
    "hillclimb",
    "investigation",
    "multi-phase-plan",
    "opening-a-pr",
    "orchestrate",
    "pause-safely",
    "perf-issue",
    "prototype",
    "refactoring",
    "runtime-forensics",
    "session-pickup",
    "shipping",
    "trace-forensics",
    "visual-parity",
    "worktree-cleanup",
)
_WORKFLOW_SOURCES = {
    workflow: f"plugins/pstack/skills/poteto-mode/playbooks/{workflow}.md"
    for workflow in _PLAYBOOK_WORKFLOWS
}
# Review is an interrogation skill upstream rather than a poteto playbook.
_WORKFLOW_SOURCES["review"] = "plugins/pstack/skills/interrogate/SKILL.md"
_WORKFLOW_NAMES = tuple(sorted(_WORKFLOW_SOURCES))

_AUTO_ROUTE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("pause-safely", r"\b(?:pause safely|pause this|park this|safe stop|stop safely|handoff checkpoint)\b"),
    ("session-pickup", r"\b(?:session pickup|resume(?: this| the)? work|continue from|pick up where|prior session|previous session)\b"),
    ("worktree-cleanup", r"\b(?:worktree cleanup|clean up (?:the )?worktree|remove stale worktree)\b"),
    ("visual-parity", r"\b(?:visual parity|pixel[- ]perfect|match (?:the )?screenshot|screenshot parity)\b"),
    ("perf-issue", r"\b(?:performance|latency|slow(?:down)?|throughput|perf issue|bottleneck)\b"),
    ("runtime-forensics", r"\b(?:runtime forensics|process forensics|runtime state|live process|service logs?)\b"),
    ("trace-forensics", r"\b(?:trace forensics|event trace|correlation id|distributed trace|event ledger)\b"),
    ("prototype", r"\b(?:prototype|proof of concept|poc|spike)\b"),
    ("refactoring", r"\b(?:refactor|refactoring|restructure|technical debt)\b"),
    ("investigation", r"\b(?:investigate|investigation|root cause analysis|explain how|understand why)\b"),
    ("eval", r"\b(?:eval|evaluation|benchmark|scorecard)\b"),
    ("orchestrate", r"\b(?:orchestrate|orchestration|multi[- ]agent|parallel agents?)\b"),
    ("opening-a-pr", r"\b(?:open|create|prepare) (?:a )?(?:pull request|pr)\b"),
    ("shipping", r"\b(?:ship|shipping|release|deploy to production)\b"),
    ("authoring-a-skill", r"\b(?:author|create|write|build) (?:a )?(?:new )?skill\b"),
    ("multi-phase-plan", r"\b(?:multi[- ]phase plan|phased plan|implementation roadmap)\b"),
    ("hillclimb", r"\b(?:hillclimb|hill climb|iterative improvement)\b"),
    ("babysit", r"\b(?:babysit|watch until|monitor until)\b"),
    ("autonomous-run", r"\b(?:autonomous run|run autonomously)\b"),
)

_ROLE_PROFILES = {
    "coordinator": ("coding", "deep"),
    "code": ("coding", "deep"),
    "investigator": ("research", "deep"),
    "reviewer": ("coding", "deep"),
    "judgment": ("reasoning_high", "deep"),
    "prose": ("reflection", "auto"),
}


class PstackAdapterError(RuntimeError):
    pass


def _descriptor(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


_TOOL_CATALOG = {
    "route_task": _descriptor(
        "route_task",
        "Choose a pinned Pstack workflow and build a HIRDA-owned coordinator request.",
        {
            "goal": {"type": "string", "minLength": 1, "maxLength": 120000},
            "workflow": {
                "type": ["string", "null"],
                "enum": [*_WORKFLOW_NAMES, None],
                "default": None,
            },
            "workflow_hint": {
                "type": ["string", "null"],
                "enum": ["engineering_task", None],
                "default": None,
            },
        },
        ["goal"],
    ),
    "load_workflow": _descriptor(
        "load_workflow",
        "Load the exact pinned Pstack methodology text for one supported workflow.",
        {
            "workflow": {
                "type": "string",
                "enum": list(_WORKFLOW_NAMES),
            }
        },
        ["workflow"],
    ),
    "build_role_request": _descriptor(
        "build_role_request",
        "Translate a Pstack role task into a runtime-neutral HIRDA cognitive request.",
        {
            "role": {
                "type": "string",
                "enum": sorted(_ROLE_PROFILES),
            },
            "task": {"type": "string", "minLength": 1, "maxLength": 120000},
            "workflow": {
                "type": ["string", "null"],
                "enum": [*_WORKFLOW_NAMES, None],
                "default": None,
            },
        },
        ["role", "task"],
    ),
}


class PstackIntegration:
    def __init__(self, *, repository_root: Path | None = None) -> None:
        root = repository_root or Path(__file__).resolve().parents[2]
        self.repository_root = Path(root).resolve(strict=False)
        self.pstack_root = self.repository_root / "third_party" / "pstack-claude"
        self._activity_events: deque[dict[str, Any]] = deque(maxlen=250)
        self._activity_traces: dict[str, dict[str, Any]] = {}
        self._activity_sequence = 0
        self._active_trace_id: str | None = None
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "pstack",
                "name": "Pstack Engineering Workflow",
                "capabilities": [
                    "engineering_workflow",
                    "workflow_routing",
                    "role_request_translation",
                ],
                "runtime": {"type": "local-python"},
                "tools": list(_TOOL_CATALOG),
                "permissions": {name: "read" for name in _TOOL_CATALOG},
                "health": {"type": "adapter_probe"},
                "routing": {
                    "mode": "methodology-layer",
                    "preferred_lanes": ["claude", "codex", "hermes"],
                },
                "metadata": {
                    "runtime_authority": "hirda",
                    "model_selection": "deferred_to_hirda",
                    "upstream_repository": PSTACK_REPOSITORY,
                    "upstream_revision": PSTACK_REVISION,
                },
            }
        )

    @staticmethod
    def _activity_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_trace(
        self,
        *,
        tool: str,
        goal: str,
        context: dict[str, Any],
        workflow: str | None = None,
        role: str | None = None,
    ) -> str:
        trace_id = str(context.get("pstack_trace_id") or "").strip()
        if not trace_id:
            trace_id = f"PSTACK-TRACE-{uuid4().hex[:12].upper()}"
        identity = {
            key: str(context.get(key) or "").strip()
            for key in ("task_id", "capsule_id", "managed_session_id", "turn_id")
            if context.get(key)
        }
        trace = self._activity_traces.get(trace_id)
        if trace is None:
            trace = {
                "trace_id": trace_id,
                "tool": tool,
                "goal": str(goal or "").strip()[:1200],
                "workflow": workflow,
                "role": role,
                "status": "running",
                "current_step": None,
                "started_at": self._activity_timestamp(),
                "updated_at": None,
                "identity": identity,
            }
            self._activity_traces[trace_id] = trace
            while len(self._activity_traces) > 32:
                oldest = next(iter(self._activity_traces))
                if oldest == trace_id and len(self._activity_traces) == 1:
                    break
                self._activity_traces.pop(oldest, None)
        else:
            if workflow:
                trace["workflow"] = workflow
            if role:
                trace["role"] = role
            if identity:
                trace["identity"] = {**trace.get("identity", {}), **identity}
        self._active_trace_id = trace_id
        return trace_id

    def _emit_activity(
        self,
        trace_id: str,
        *,
        phase: str,
        title: str,
        detail: str,
        status: str,
        workflow: str | None = None,
        role: str | None = None,
        tool: str | None = None,
        trace_status: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "pstack",
    ) -> dict[str, Any]:
        self._activity_sequence += 1
        timestamp = self._activity_timestamp()
        trace = self._activity_traces.get(trace_id)
        if trace is not None:
            if workflow:
                trace["workflow"] = workflow
            if role:
                trace["role"] = role
            if tool:
                trace["tool"] = tool
            trace["current_step"] = {
                "phase": phase,
                "title": title,
                "status": status,
                "actor": actor,
            }
            trace["updated_at"] = timestamp
            if trace_status:
                trace["status"] = trace_status
        event = {
            "schema": "hirda-pstack-activity-event-v1",
            "sequence": self._activity_sequence,
            "trace_id": trace_id,
            "timestamp": timestamp,
            "actor": str(actor or "pstack").strip().casefold() or "pstack",
            "phase": phase,
            "title": title,
            "detail": detail,
            "status": status,
            "workflow": workflow or (trace or {}).get("workflow"),
            "role": role or (trace or {}).get("role"),
            "tool": tool or (trace or {}).get("tool"),
            "runtime_authority": "hirda",
            "metadata": dict(metadata or {}),
        }
        self._activity_events.append(event)
        return event

    def record_external_event(
        self,
        trace_id: str,
        *,
        actor: str,
        phase: str,
        title: str,
        detail: str,
        status: str,
        trace_status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_key = str(trace_id or "").strip()
        if not trace_key or trace_key not in self._activity_traces:
            raise PstackAdapterError("pstack_trace_unknown")
        return self._emit_activity(
            trace_key,
            actor=actor,
            phase=phase,
            title=title,
            detail=detail,
            status=status,
            trace_status=trace_status,
            metadata=metadata,
        )

    def activity_snapshot(
        self,
        *,
        since: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        cursor = max(0, int(since or 0))
        bounded_limit = max(1, min(int(limit or 100), 250))
        events = [
            dict(event)
            for event in self._activity_events
            if int(event.get("sequence") or 0) > cursor
        ][-bounded_limit:]
        active = self._activity_traces.get(self._active_trace_id or "")
        traces = list(self._activity_traces.values())[-20:]
        return {
            "schema": "hirda-pstack-activity-v1",
            "available": True,
            "runtime_authority": "hirda",
            "model_selection": "deferred_to_hirda",
            "latest_sequence": self._activity_sequence,
            "event_count": len(self._activity_events),
            "active_trace": dict(active) if active else None,
            "traces": [dict(item) for item in reversed(traces)],
            "events": events,
        }

    def _revision(self) -> str | None:
        if not self.pstack_root.is_dir():
            return None
        try:
            result = subprocess.run(
                ["git", "-C", str(self.pstack_root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    def _workflow_path(self, workflow: str) -> Path:
        key = str(workflow or "").strip().casefold()
        relative = _WORKFLOW_SOURCES.get(key)
        if relative is None:
            raise PstackAdapterError("pstack_workflow_invalid")
        path = (self.pstack_root / relative).resolve(strict=False)
        if self.pstack_root.resolve(strict=False) not in path.parents:
            raise PstackAdapterError("pstack_workflow_path_escape")
        return path

    def _load_workflow(self, workflow: str) -> dict[str, Any]:
        path = self._workflow_path(workflow)
        if not path.is_file():
            raise PstackAdapterError("pstack_workflow_source_missing")
        text = path.read_text(encoding="utf-8")
        return {
            "workflow": workflow,
            "source": {
                "repository": PSTACK_REPOSITORY,
                "revision": PSTACK_REVISION,
                "path": str(path.relative_to(self.pstack_root)),
            },
            "methodology": text,
        }

    @staticmethod
    def _select_workflow(
        goal: str,
        requested: str | None = None,
        workflow_hint: str | None = None,
    ) -> str:
        explicit = str(requested or "").strip().casefold()
        if explicit:
            if explicit not in _WORKFLOW_SOURCES:
                raise PstackAdapterError("pstack_workflow_invalid")
            return explicit

        lowered = str(goal or "").casefold()

        review_intent = re.search(
            r"\binterrogate\b|\b(?:review|audit)\s+(?:(?:this|the|my|our|these)\s+)?"
            r"(?:diff|patch|code|changes?|implementation|pull request|pr|design|architecture)\b"
            r"|\b(?:pull request|pr)\s+review\b",
            lowered,
        )
        if review_intent:
            return "review"

        # Specific methodology intents take precedence over generic verbs such
        # as "build" or "create". This lets "build a prototype" remain a
        # prototype while ambiguous build/add requests still fall back to feature.
        for workflow, pattern in _AUTO_ROUTE_PATTERNS:
            if re.search(pattern, lowered):
                return workflow

        if re.search(
            r"\b(?:add|build|create|implement|introduce|expand|extend)\b",
            lowered,
        ):
            return "feature"

        if re.search(
            r"\b(?:fix|repair|resolve)\b.{0,50}\b(?:bug|crash|error|defect|regression|failure|broken)\b"
            r"|\b(?:bug|crash|error|defect|regression|failure|broken)\b.{0,50}\b(?:fix|repair|resolve)\b",
            lowered,
        ):
            return "bug-fix"

        if str(workflow_hint or "").strip().casefold() == "engineering_task":
            return "feature"
        return "feature"

    @staticmethod
    def _role_profile(role: str) -> tuple[str, str]:
        key = str(role or "").strip().casefold()
        try:
            return _ROLE_PROFILES[key]
        except KeyError:
            raise PstackAdapterError("pstack_role_invalid") from None

    def _build_role_request(
        self,
        *,
        role: str,
        task: str,
        workflow: str | None,
        methodology: str | None = None,
    ) -> dict[str, Any]:
        role_key = str(role or "").strip().casefold()
        task_text = str(task or "").strip()
        if not task_text:
            raise PstackAdapterError("pstack_task_required")
        capability, depth = self._role_profile(role_key)
        workflow_key = (
            self._select_workflow(task_text, workflow)
            if workflow is not None
            else None
        )
        prompt_parts = [
            f"PSTACK ROLE: {role_key}",
            "HIRDA owns model and runtime selection. Do not pin or request a model.",
        ]
        if workflow_key:
            prompt_parts.append(f"PSTACK WORKFLOW: {workflow_key}")
        if methodology:
            prompt_parts.extend(["PSTACK METHODOLOGY:", methodology])
        prompt_parts.extend(["TASK:", task_text])
        request = {
            "schema": "hirda-cognitive-request-v1",
            "request_id": f"PSTACK-{uuid4().hex[:12].upper()}",
            "prompt": "\n\n".join(prompt_parts),
            "capability": capability,
            "preferred_depth": depth,
            "allow_fallback": True,
            "context": {
                "workflow_provider": "pstack",
                "workflow": workflow_key,
                "role": role_key,
                "upstream_repository": PSTACK_REPOSITORY,
                "upstream_revision": PSTACK_REVISION,
                "runtime_authority": "hirda",
            },
        }
        return {
            "schema": "hirda-pstack-role-request-v1",
            "role": role_key,
            "workflow": workflow_key,
            "runtime_authority": "hirda",
            "model_selection": "deferred",
            "hirda_request": request,
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        return [dict(_TOOL_CATALOG[name]) for name in self.manifest.tools]

    async def validate(self) -> dict[str, Any]:
        revision = self._revision()
        missing = [
            workflow
            for workflow in _WORKFLOW_SOURCES
            if not self._workflow_path(workflow).is_file()
        ]
        ok = revision == PSTACK_REVISION and not missing
        return {
            "ok": ok,
            "repository": PSTACK_REPOSITORY,
            "expected_revision": PSTACK_REVISION,
            "observed_revision": revision,
            "missing_workflows": missing,
            "supported_workflows": list(_WORKFLOW_NAMES),
            "playbook_count": len(_PLAYBOOK_WORKFLOWS),
            "runtime_authority": "hirda",
        }

    async def certify(self) -> dict[str, Any]:
        samples = {
            "bug-fix": "Resolve the crash regression in session reconnect.",
            "feature": "Expand the workflow adapter with a registry.",
            "review": "Review this diff for correctness and maintainability.",
            "investigation": "Investigate why task ownership drifts across sessions.",
            "perf-issue": "Diagnose the latency bottleneck in task dispatch.",
            "refactoring": "Refactor the scheduler ranking module without behavior changes.",
            "prototype": "Build a prototype for a new routing strategy.",
            "session-pickup": "Resume this work from the previous session.",
            "pause-safely": "Pause this task safely and leave a handoff checkpoint.",
            "visual-parity": "Match the screenshot with pixel-perfect visual parity.",
        }
        routes = {}
        for workflow, goal in samples.items():
            selected = self._select_workflow(goal)
            request = self._build_role_request(
                role="coordinator",
                task=goal,
                workflow=selected,
            )
            routes[workflow] = {
                "selected": selected,
                "runtime_neutral": "preferred_runtime"
                not in request["hirda_request"],
            }

        explicit_loadable = all(
            self._workflow_path(workflow).is_file()
            for workflow in _WORKFLOW_NAMES
        )
        return {
            "ok": (
                explicit_loadable
                and all(
                    item["selected"] == workflow and item["runtime_neutral"]
                    for workflow, item in routes.items()
                )
            ),
            "routes": routes,
            "supported_workflows": list(_WORKFLOW_NAMES),
            "playbook_count": len(_PLAYBOOK_WORKFLOWS),
            "explicit_loadable": explicit_loadable,
        }

    async def status(self) -> dict[str, Any]:
        validation = await self.validate()
        activity = self.activity_snapshot(limit=1)
        return {
            **validation,
            "schema": "hirda-pstack-status-v1",
            "supported_workflows": sorted(_WORKFLOW_SOURCES),
            "supported_roles": sorted(_ROLE_PROFILES),
            "observability": {
                "schema": activity["schema"],
                "event_count": activity["event_count"],
                "latest_sequence": activity["latest_sequence"],
                "active_trace": activity["active_trace"],
            },
        }

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        trace_id: str | None = None
        try:
            if name == "load_workflow":
                workflow = str(arguments.get("workflow") or "").strip().casefold()
                trace_id = self._new_trace(
                    tool=name,
                    goal=f"Load Pstack workflow {workflow or 'unknown'}",
                    context=context,
                    workflow=workflow or None,
                )
                self._emit_activity(
                    trace_id,
                    phase="received",
                    title="Workflow requested",
                    detail=f"Loading pinned Pstack methodology for {workflow or 'unknown'}.",
                    status="running",
                    workflow=workflow or None,
                    tool=name,
                )
                loaded = self._load_workflow(workflow)
                self._emit_activity(
                    trace_id,
                    phase="methodology_loaded",
                    title="Methodology loaded",
                    detail="Pinned upstream workflow text is ready for use.",
                    status="passed",
                    workflow=workflow,
                    tool=name,
                    trace_status="completed",
                    metadata={"source": loaded["source"]},
                )
                return {**loaded, "trace_id": trace_id}

            if name == "build_role_request":
                workflow = arguments.get("workflow")
                workflow_key = (
                    str(workflow).strip().casefold()
                    if workflow is not None
                    else None
                )
                role = str(arguments.get("role") or "").strip().casefold()
                task = str(arguments.get("task") or "").strip()
                trace_id = self._new_trace(
                    tool=name,
                    goal=task,
                    context=context,
                    workflow=workflow_key,
                    role=role or None,
                )
                self._emit_activity(
                    trace_id,
                    phase="received",
                    title="Role task received",
                    detail=f"Translating Pstack role {role or 'unknown'} into a HIRDA request.",
                    status="running",
                    workflow=workflow_key,
                    role=role or None,
                    tool=name,
                )
                result = self._build_role_request(
                    role=role,
                    task=task,
                    workflow=workflow_key,
                )
                result["trace_id"] = trace_id
                result["hirda_request"]["context"]["pstack_trace_id"] = trace_id
                self._emit_activity(
                    trace_id,
                    phase="hirda_request_ready",
                    title="HIRDA request ready",
                    detail=(
                        f"Role {result['role']} mapped to capability "
                        f"{result['hirda_request']['capability']}; runtime selection remains with HIRDA."
                    ),
                    status="passed",
                    workflow=result.get("workflow"),
                    role=result["role"],
                    tool=name,
                    trace_status="ready_for_hirda",
                    metadata={
                        "request_id": result["hirda_request"]["request_id"],
                        "capability": result["hirda_request"]["capability"],
                        "preferred_depth": result["hirda_request"]["preferred_depth"],
                    },
                )
                return result

            if name == "route_task":
                goal = str(arguments.get("goal") or "").strip()
                trace_id = self._new_trace(
                    tool=name,
                    goal=goal,
                    context=context,
                )
                self._emit_activity(
                    trace_id,
                    phase="received",
                    title="Task received",
                    detail="Pstack received an engineering task for methodology selection.",
                    status="running",
                    tool=name,
                )
                if not goal:
                    raise PstackAdapterError("pstack_goal_required")
                workflow = self._select_workflow(
                    goal,
                    arguments.get("workflow"),
                    arguments.get("workflow_hint"),
                )
                self._emit_activity(
                    trace_id,
                    phase="workflow_selected",
                    title="Workflow selected",
                    detail=f"Selected Pstack workflow: {workflow}.",
                    status="passed",
                    workflow=workflow,
                    tool=name,
                )
                loaded = self._load_workflow(workflow)
                self._emit_activity(
                    trace_id,
                    phase="methodology_loaded",
                    title="Methodology loaded",
                    detail="Loaded the pinned upstream methodology without selecting a runtime.",
                    status="passed",
                    workflow=workflow,
                    tool=name,
                    metadata={"source": loaded["source"]},
                )
                coordinator = self._build_role_request(
                    role="coordinator",
                    task=goal,
                    workflow=workflow,
                    methodology=loaded["methodology"],
                )
                coordinator["trace_id"] = trace_id
                coordinator["hirda_request"]["context"]["pstack_trace_id"] = trace_id
                result = {
                    "schema": "hirda-pstack-route-v1",
                    "trace_id": trace_id,
                    "workflow": workflow,
                    "source": loaded["source"],
                    "methodology": loaded["methodology"],
                    "runtime_authority": "hirda",
                    "model_selection": "deferred",
                    "coordinator": coordinator,
                }
                self._emit_activity(
                    trace_id,
                    phase="handoff_ready",
                    title="Ready for HIRDA",
                    detail=(
                        "Coordinator request is ready. Pstack stops at the methodology boundary; "
                        "HIRDA owns runtime, model, machine, permission, and execution decisions."
                    ),
                    status="ready",
                    workflow=workflow,
                    role="coordinator",
                    tool=name,
                    trace_status="ready_for_hirda",
                    metadata={
                        "request_id": coordinator["hirda_request"]["request_id"],
                        "capability": coordinator["hirda_request"]["capability"],
                    },
                )
                return result

            raise PstackAdapterError("pstack_tool_unknown")
        except Exception as exc:
            if trace_id is not None:
                self._emit_activity(
                    trace_id,
                    phase="failed",
                    title="Pstack step failed",
                    detail=f"{type(exc).__name__}: {exc}",
                    status="failed",
                    tool=name,
                    trace_status="failed",
                )
            raise
