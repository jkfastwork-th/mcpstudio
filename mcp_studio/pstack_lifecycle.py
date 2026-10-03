from __future__ import annotations

import time
from typing import Any


PSTACK_LIFECYCLE_SCHEMA_V1 = "hirda-pstack-lifecycle-v1"
PSTACK_LIFECYCLE_SCHEMA = "hirda-pstack-lifecycle-v2"

_LEGACY_WORKFLOW_PHASES: dict[str, tuple[str, ...]] = {
    "feature": ("analyze", "plan", "implement", "review", "verify", "done"),
    "bug-fix": ("analyze", "plan", "implement", "review", "verify", "done"),
    "review": ("analyze", "review", "done"),
}
_ORCHESTRATION_PHASES = ("admitted", "working", "verified", "terminal")


class PstackLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _build_lifecycle(
    *,
    schema: str,
    workflow: str,
    phases: tuple[str, ...],
) -> dict[str, Any]:
    now = time.time()
    return {
        "schema": schema,
        "workflow": workflow,
        "status": "active",
        "current_phase": phases[0],
        "revision": 1,
        "started_at": now,
        "updated_at": now,
        "stages": [
            {
                "phase": phase,
                "status": "active" if index == 0 else "pending",
                "evidence": [],
            }
            for index, phase in enumerate(phases)
        ],
    }


def lifecycle_for_workflow(workflow: str | None) -> dict[str, Any]:
    """Create the HIRDA-owned lifecycle for a new PStack task.

    PStack owns methodology inside Working. HIRDA owns admission, execution
    state, verification, and terminal disposition.
    """
    workflow_key = str(workflow or "feature").strip().casefold()
    lifecycle = _build_lifecycle(
        schema=PSTACK_LIFECYCLE_SCHEMA,
        workflow=workflow_key,
        phases=_ORCHESTRATION_PHASES,
    )
    lifecycle["orchestration_owner"] = "hirda"
    lifecycle["methodology_owner"] = "pstack"
    return lifecycle


def legacy_lifecycle_for_workflow(workflow: str | None) -> dict[str, Any]:
    """Build v1 lifecycle state for legacy task recovery and compatibility."""
    workflow_key = str(workflow or "feature").strip().casefold()
    phases = _LEGACY_WORKFLOW_PHASES.get(
        workflow_key,
        _LEGACY_WORKFLOW_PHASES["feature"],
    )
    return _build_lifecycle(
        schema=PSTACK_LIFECYCLE_SCHEMA_V1,
        workflow=workflow_key,
        phases=phases,
    )


def ensure_lifecycle(
    lifecycle: dict[str, Any] | None,
    *,
    workflow: str | None,
) -> dict[str, Any]:
    if not isinstance(lifecycle, dict):
        return lifecycle_for_workflow(workflow)
    schema = str(lifecycle.get("schema") or "")
    if schema not in {PSTACK_LIFECYCLE_SCHEMA_V1, PSTACK_LIFECYCLE_SCHEMA}:
        return lifecycle_for_workflow(workflow)
    return {
        **lifecycle,
        "stages": [
            {
                **stage,
                "evidence": list(stage.get("evidence") or []),
            }
            for stage in lifecycle.get("stages") or []
            if isinstance(stage, dict)
        ],
    }


def _is_legacy(lifecycle: dict[str, Any]) -> bool:
    return str(lifecycle.get("schema") or "") == PSTACK_LIFECYCLE_SCHEMA_V1


def _stage(lifecycle: dict[str, Any], phase: str) -> dict[str, Any]:
    for stage in lifecycle.get("stages") or []:
        if str(stage.get("phase") or "") == phase:
            return stage
    raise PstackLifecycleError(
        "PSTACK_PHASE_UNKNOWN",
        f"Pstack lifecycle does not contain phase {phase!r}.",
    )


def _passed(lifecycle: dict[str, Any], phase: str) -> bool:
    try:
        return _stage(lifecycle, phase).get("status") == "passed"
    except PstackLifecycleError:
        return False


def workflow_gate(
    lifecycle: dict[str, Any],
    *,
    tool_class: str,
) -> dict[str, Any]:
    current = str(lifecycle.get("current_phase") or "")
    workflow = str(lifecycle.get("workflow") or "feature")
    if tool_class not in {"read", "write", "execute", "destructive"}:
        return {
            "allowed": False,
            "code": "PSTACK_TOOL_CLASS_UNKNOWN",
            "message": (
                "Pstack cannot verify this tool's effect class; classify it before dispatch."
            ),
        }
    if lifecycle.get("status") == "completed":
        if tool_class == "read":
            return {"allowed": True}
        return {
            "allowed": False,
            "code": "PSTACK_WORKFLOW_COMPLETED",
            "message": (
                "The Pstack workflow is already complete; open a new task before "
                "mutating or executing more work."
            ),
        }
    if workflow == "review" and tool_class in {"write", "destructive"}:
        return {
            "allowed": False,
            "code": "PSTACK_REVIEW_WORKFLOW_READ_ONLY",
            "message": "The selected Pstack review workflow is read-only.",
        }

    if _is_legacy(lifecycle):
        if tool_class in {"write", "destructive"}:
            if not _passed(lifecycle, "plan"):
                return {
                    "allowed": False,
                    "code": "PSTACK_PLAN_REQUIRED",
                    "message": (
                        "Complete Analyze and Plan checkpoints before implementation writes."
                    ),
                }
            if current != "implement":
                return {
                    "allowed": False,
                    "code": "PSTACK_IMPLEMENTATION_GATE_CLOSED",
                    "message": (
                        "Implementation writes are only allowed during the implement "
                        f"phase; current phase is {current or 'unknown'}."
                    ),
                }
        return {"allowed": True}

    if lifecycle.get("status") == "blocked" and tool_class != "read":
        return {
            "allowed": False,
            "code": "PSTACK_WORKFLOW_BLOCKED",
            "message": (
                "The HIRDA orchestration lifecycle is blocked; resolve the current "
                "checkpoint before executing or mutating work."
            ),
        }
    if tool_class in {"write", "destructive"} and current != "working":
        return {
            "allowed": False,
            "code": "PSTACK_WORKING_REQUIRED",
            "message": (
                "Mutating work is allowed only while HIRDA orchestration is in "
                f"Working; current phase is {current or 'unknown'}. PStack owns "
                "the methodology steps inside Working."
            ),
        }
    return {"allowed": True}


def record_tool_evidence(
    lifecycle: dict[str, Any],
    *,
    tool: str,
    tool_class: str,
    status: str,
    detail: str,
) -> dict[str, Any]:
    updated = ensure_lifecycle(lifecycle, workflow=str(lifecycle.get("workflow") or "feature"))
    current = str(updated.get("current_phase") or "")
    if not current:
        return updated
    stage = _stage(updated, current)
    evidence = list(stage.get("evidence") or [])
    evidence.append(
        {
            "kind": "tool",
            "tool": str(tool or ""),
            "tool_class": str(tool_class or "unknown"),
            "status": str(status or "unknown"),
            "detail": str(detail or "")[:500],
            "at": time.time(),
        }
    )
    stage["evidence"] = evidence[-50:]
    updated["revision"] = int(updated.get("revision") or 0) + 1
    updated["updated_at"] = time.time()
    return updated


def _require_phase_evidence(
    lifecycle: dict[str, Any],
    phase: str,
    evidence_text: str,
) -> None:
    stage = _stage(lifecycle, phase)
    tool_evidence = list(stage.get("evidence") or [])
    passed_tools = [item for item in tool_evidence if item.get("status") == "passed"]

    if _is_legacy(lifecycle):
        if phase == "analyze" and not passed_tools:
            raise PstackLifecycleError(
                "PSTACK_ANALYZE_EVIDENCE_REQUIRED",
                "Analyze requires at least one successful HIRDA tool observation before it can pass.",
            )
        if phase == "implement":
            writes = [
                item
                for item in passed_tools
                if item.get("tool_class") in {"write", "destructive"}
            ]
            if not writes and not evidence_text.casefold().startswith("n/a:"):
                raise PstackLifecycleError(
                    "PSTACK_IMPLEMENT_EVIDENCE_REQUIRED",
                    "Implement requires successful write evidence, or an explicit 'n/a:' explanation.",
                )
        if phase == "review" and not passed_tools:
            raise PstackLifecycleError(
                "PSTACK_REVIEW_EVIDENCE_REQUIRED",
                "Review requires at least one successful HIRDA inspection or diagnostic observation.",
            )
        if phase == "verify":
            verification = [
                item
                for item in passed_tools
                if item.get("tool_class") == "execute"
                or item.get("tool") == "get_diagnostics_for_file"
            ]
            if not verification:
                raise PstackLifecycleError(
                    "PSTACK_VERIFY_EVIDENCE_REQUIRED",
                    "Verify requires a successful execution/test or diagnostics observation.",
                )
        return

    if phase in {"admitted", "working"} and not passed_tools:
        raise PstackLifecycleError(
            f"PSTACK_{phase.upper()}_EVIDENCE_REQUIRED",
            (
                f"{phase.title()} requires at least one successful HIRDA tool "
                "observation before it can pass."
            ),
        )
    if phase == "verified":
        verification = [
            item
            for item in passed_tools
            if item.get("tool_class") == "execute"
            or item.get("tool") == "get_diagnostics_for_file"
        ]
        if not verification:
            raise PstackLifecycleError(
                "PSTACK_VERIFIED_EVIDENCE_REQUIRED",
                "Verified requires a successful execution/test or diagnostics observation.",
            )


def checkpoint(
    lifecycle: dict[str, Any],
    *,
    phase: str,
    status: str,
    evidence: str,
) -> dict[str, Any]:
    phase_key = str(phase or "").strip().casefold()
    status_key = str(status or "").strip().casefold()
    evidence_text = str(evidence or "").strip()
    if status_key not in {"passed", "failed"}:
        raise PstackLifecycleError(
            "PSTACK_CHECKPOINT_STATUS_INVALID",
            "Checkpoint status must be passed or failed.",
        )
    updated = ensure_lifecycle(
        lifecycle,
        workflow=str(lifecycle.get("workflow") or "feature"),
    )
    current = str(updated.get("current_phase") or "")
    if phase_key != current:
        raise PstackLifecycleError(
            "PSTACK_PHASE_ORDER_VIOLATION",
            f"Cannot checkpoint {phase_key!r}; current Pstack phase is {current!r}.",
        )
    if len(evidence_text) < 3:
        raise PstackLifecycleError(
            "PSTACK_CHECKPOINT_EVIDENCE_REQUIRED",
            "Checkpoint evidence is required.",
        )
    if status_key == "passed":
        _require_phase_evidence(updated, phase_key, evidence_text)

    stage = _stage(updated, phase_key)
    stage["status"] = status_key
    stage["checkpoint_evidence"] = evidence_text
    stage["checkpointed_at"] = time.time()
    updated["revision"] = int(updated.get("revision") or 0) + 1
    updated["updated_at"] = time.time()

    if status_key == "failed":
        if _is_legacy(updated):
            if phase_key in {"review", "verify"}:
                implement = _stage(updated, "implement")
                implement["status"] = "active"
                updated["current_phase"] = "implement"
                updated["status"] = "active"
                updated["rework_from"] = phase_key
                updated["rework_reason"] = evidence_text
                return updated
            updated["status"] = "blocked"
            return updated

        if phase_key in {"verified", "terminal"}:
            working = _stage(updated, "working")
            working["status"] = "active"
            updated["current_phase"] = "working"
            updated["status"] = "active"
            updated["rework_from"] = phase_key
            updated["rework_reason"] = evidence_text
            return updated
        updated["status"] = "blocked"
        return updated

    stages = list(updated.get("stages") or [])
    index = next(
        idx
        for idx, item in enumerate(stages)
        if str(item.get("phase") or "") == phase_key
    )
    if index == len(stages) - 1:
        updated["status"] = "completed"
        updated["completed_at"] = time.time()
        return updated

    next_stage = stages[index + 1]
    next_stage["status"] = "active"
    updated["current_phase"] = next_stage["phase"]
    updated["status"] = "active"
    return updated
