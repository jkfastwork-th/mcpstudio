from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from uuid import uuid4

import httpx

from .execution import ToolSchemaError, build_tool_arguments
from .herdr import HerdrManager, _decode_text_content
from .hermes_groups import HermesGroupClient, HermesGroupError
from .agent_permissions import resolve_agent_permissions


COGNITIVE_REQUEST_SCHEMA = "hirda-cognitive-request-v1"
COGNITIVE_RESULT_SCHEMA = "hirda-cognitive-result-v1"
COGNITIVE_JUDGMENT_SCHEMA = "hirda-jev-cognitive-judgment-v1"
TASK_SEMANTIC_PROFILE_SCHEMA = "hirda-task-semantic-profile-v1"
SEMANTIC_RUNTIME_RANKING_SCHEMA = "hirda-semantic-runtime-ranking-v1"
SUPERVISOR_POLICY_SCHEMA = "hirda-supervisor-policy-v1"
HERMES_TEAM_DELEGATION_SCHEMA = "hirda-hermes-team-delegation-v1"
RUNTIME_BACKOFF_SCHEMA = "hirda-runtime-backoff-v1"
_RUNTIME_BACKOFF_ACTOR = "hirda/runtime-backoff"
_RUNTIME_BACKOFF_REASON_PREFIX = "runtime_backoff"

_CAPABILITY_TASK_TYPES = {
    "social_dialogue": "conversation",
    "reasoning_high": "general_reasoning",
    "reflection": "reflection",
    "fast_utility": "utility",
    "coding": "software_engineering",
    "research": "research",
}
_CAPABILITY_SEMANTIC_CAPABILITIES = {
    "social_dialogue": ("social_reasoning", "persona_continuity"),
    "reasoning_high": ("reasoning", "multi_step_reasoning"),
    "reflection": ("reflection",),
    "fast_utility": ("fast_utility",),
    "coding": ("coding", "software_engineering"),
    "research": ("research", "evidence_synthesis"),
}
_CAPABILITY_LATENCY_PRIORITIES = {
    "social_dialogue": "high",
    "reasoning_high": "quality",
    "reflection": "balanced",
    "fast_utility": "high",
    "coding": "quality",
    "research": "quality",
}
_TASK_TYPES = {
    "conversation",
    "software_engineering",
    "research",
    "planning",
    "tool_operation",
    "general_reasoning",
    "reflection",
    "utility",
}
_LATENCY_PRIORITIES = {"high", "balanced", "quality"}
_JEV_TASK_TYPE_MIN_CONFIDENCE = 0.50
_JEV_TOOL_OPERATION_MIN_CONFIDENCE = 0.25
_JEV_TOOL_OPERATION_MIN_NEED = 0.55
_JEV_NON_DECLARED_CODING_WEIGHT = 0.35

_CAPABILITIES = {
    "social_dialogue",
    "reasoning_high",
    "reflection",
    "fast_utility",
    "coding",
    "research",
}
_DEPTHS = {"auto", "fast", "deep"}
_SYSTEM_PRINCIPAL_PANE_PREFIXES: dict[tuple[str, str], tuple[str, ...]] = {
    ("hermes", "hirda-skill-distiller"): ("hirda-skill-hermes",),
}


class CognitiveRouterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CognitiveRequest:
    schema: str
    request_id: str
    prompt: str
    capability: str = "reflection"
    preferred_depth: str = "auto"
    context: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    allow_fallback: bool = True
    preferred_runtime: str | None = None
    target_pane_id: str | None = None
    fallback_target_pane_id: str | None = None
    principal_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class JevCognitiveJudgment:
    schema: str
    enabled: bool
    evaluated: bool
    mode: str
    request_id: str
    suggested_lane: str | None
    confidence: float
    needs_tool: float | None
    needs_specialist: float | None
    model: str | None
    error: str | None
    task_type: str | None = None
    coding: float | None = None
    latency_priority: str | None = None
    advisory_only: bool = True
    routing_authority: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CognitiveResult:
    schema: str
    request_id: str
    status: str
    output: str | None
    lane: str
    runtime: str | None
    provider: str | None
    model: str | None
    degraded: bool
    fallback_used: bool
    attempts: tuple[dict[str, Any], ...]
    jev: dict[str, Any]
    task_semantic_profile: dict[str, Any] = field(default_factory=dict)
    semantic_routing: dict[str, Any] = field(default_factory=dict)
    supervisor_policy: dict[str, Any] = field(default_factory=dict)
    infrastructure_routing: dict[str, Any] = field(default_factory=dict)
    team_delegation: dict[str, Any] = field(default_factory=dict)
    identity_continuity: bool = True
    semantic_authority_changed: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attempts"] = list(self.attempts)
        return data


def normalize_cognitive_request(payload: CognitiveRequest | dict[str, Any]) -> CognitiveRequest:
    if isinstance(payload, CognitiveRequest):
        return payload
    if not isinstance(payload, dict):
        raise CognitiveRouterError("cognitive_request_object_required")

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise CognitiveRouterError("cognitive_prompt_required")
    if len(prompt) > 120_000:
        raise CognitiveRouterError("cognitive_prompt_too_large")

    capability = str(payload.get("capability") or "reflection").strip().casefold()
    if capability not in _CAPABILITIES:
        raise CognitiveRouterError("cognitive_capability_invalid")

    preferred_depth = str(payload.get("preferred_depth") or "auto").strip().casefold()
    if preferred_depth not in _DEPTHS:
        raise CognitiveRouterError("cognitive_depth_invalid")

    timeout_raw = payload.get("timeout_seconds", 30.0)
    try:
        timeout_seconds = max(1.0, min(300.0, float(timeout_raw)))
    except (TypeError, ValueError) as exc:
        raise CognitiveRouterError("cognitive_timeout_invalid") from exc

    request_id = str(payload.get("request_id") or f"COG-{uuid4().hex[:12].upper()}").strip()
    context = payload.get("context")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise CognitiveRouterError("cognitive_context_invalid")

    preferred_runtime = str(payload.get("preferred_runtime") or "").strip().casefold() or None
    target_pane_id = str(payload.get("target_pane_id") or "").strip() or None
    fallback_target_pane_id = str(payload.get("fallback_target_pane_id") or "").strip() or None
    allow_fallback = bool(payload.get("allow_fallback", True))

    if fallback_target_pane_id and not target_pane_id:
        raise CognitiveRouterError("cognitive_fallback_pane_requires_target")
    if fallback_target_pane_id and not allow_fallback:
        raise CognitiveRouterError("cognitive_fallback_pane_requires_fallback")
    if fallback_target_pane_id and fallback_target_pane_id == target_pane_id:
        raise CognitiveRouterError("cognitive_fallback_pane_matches_target")

    agent_id = str(payload.get("agent_id") or "").strip() or None
    principal_id = (
        str(
            payload.get("principal_id")
            or context.get("principal_id")
            or agent_id
            or ""
        ).strip()
        or None
    )

    return CognitiveRequest(
        schema=COGNITIVE_REQUEST_SCHEMA,
        request_id=request_id,
        prompt=prompt,
        capability=capability,
        preferred_depth=preferred_depth,
        context=dict(context),
        timeout_seconds=timeout_seconds,
        allow_fallback=allow_fallback,
        preferred_runtime=preferred_runtime,
        target_pane_id=target_pane_id,
        fallback_target_pane_id=fallback_target_pane_id,
        principal_id=principal_id,
        agent_id=agent_id,
        conversation_id=str(payload.get("conversation_id") or "").strip() or None,
    )


def _answer_map(body: dict[str, Any]) -> dict[str, Any]:
    answers = body.get("answers")
    return answers if isinstance(answers, dict) else {}


def _probability(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, numeric))


def _task_semantic_profile(
    request: CognitiveRequest,
    jev: JevCognitiveJudgment,
    lane: str,
) -> dict[str, Any]:
    declared_task_type = _CAPABILITY_TASK_TYPES.get(
        request.capability,
        "reflection",
    )
    task_type = declared_task_type
    source = "declared_capability"

    jev_confidence = (
        _probability(jev.confidence)
        if jev.evaluated and jev.confidence is not None
        else 0.0
    )
    tool_need = (
        _probability(jev.needs_tool)
        if jev.evaluated and jev.needs_tool is not None
        else None
    )

    if (
        request.capability == "reflection"
        and jev.evaluated
        and jev.task_type in _TASK_TYPES
        and jev_confidence >= _JEV_TASK_TYPE_MIN_CONFIDENCE
    ):
        task_type = str(jev.task_type)
        source = "jev_advisory"

    capabilities = list(
        _CAPABILITY_SEMANTIC_CAPABILITIES.get(
            request.capability,
            (request.capability,),
        )
    )
    if source == "jev_advisory" and task_type not in capabilities:
        capabilities.append(task_type)

    # A reflection request with a strong tool-use signal is still anchored to
    # its declared semantic capability, but receives an explicit tool
    # operation modifier. This keeps JEV advisory rather than authoritative.
    if (
        request.capability == "reflection"
        and tool_need is not None
        and tool_need >= _JEV_TOOL_OPERATION_MIN_NEED
        and jev_confidence >= _JEV_TOOL_OPERATION_MIN_CONFIDENCE
        and "tool_operation" not in capabilities
    ):
        capabilities.append("tool_operation")

    if request.capability == "coding":
        coding = 1.0
    elif jev.evaluated and jev.coding is not None:
        coding = (
            _probability(jev.coding)
            * jev_confidence
            * _JEV_NON_DECLARED_CODING_WEIGHT
        )
    else:
        coding = 0.0

    latency_priority = _CAPABILITY_LATENCY_PRIORITIES.get(
        request.capability,
        "balanced",
    )
    if (
        jev.evaluated
        and jev.latency_priority in _LATENCY_PRIORITIES
        and jev_confidence >= _JEV_TASK_TYPE_MIN_CONFIDENCE
    ):
        latency_priority = str(jev.latency_priority)

    return {
        "schema": TASK_SEMANTIC_PROFILE_SCHEMA,
        "source": source,
        "task_type": task_type,
        "declared_capability": request.capability,
        "capabilities": capabilities,
        "coding": coding,
        "tool_need": tool_need,
        "reasoning_depth": lane,
        "latency_priority": latency_priority,
        "jev_task_type": jev.task_type if jev.evaluated else None,
        "jev_confidence": jev.confidence if jev.evaluated else None,
        "advisory_only": True,
        "routing_authority": False,
    }


def _text_fragments(value: Any) -> list[str]:
    """Collect textual leaves from MCP/Herdr envelopes without assuming one schema."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        fragments: list[str] = []
        for item in value.values():
            fragments.extend(_text_fragments(item))
        return fragments
    if isinstance(value, (list, tuple)):
        fragments = []
        for item in value:
            fragments.extend(_text_fragments(item))
        return fragments
    return []


def _extract_marked_result(value: Any, result_start: str, result_end: str) -> str:
    """Extract one bounded cognitive result from nested MCP/terminal output."""
    fragments = _text_fragments(value)
    candidates = list(reversed(fragments))
    if fragments:
        candidates.append("\n".join(fragments))

    def marker_span(lines: list[str], index: int, marker: str) -> int | None:
        first = lines[index].strip()
        for prefix in ("● ", "• ", "◉ "):
            if first.startswith(prefix):
                first = first[len(prefix):].strip()
                break
        if first == marker:
            return 1

        # Herdr pane history is terminal-width bounded, so a long marker can be
        # soft-wrapped even when the runtime emitted it as one logical line.
        # Reconstruct only the marker itself (max 3 physical lines); never join
        # result payload lines.
        combined = first
        for span in (2, 3):
            if index + span - 1 >= len(lines):
                break
            combined += lines[index + span - 1].strip()
            if combined == marker:
                return span
            if not marker.startswith(combined):
                break
        return None

    saw_start = False
    saw_end = False
    for text in candidates:
        variants = [text]
        if "\\n" in text:
            variants.append(text.replace("\\n", "\n"))
        for candidate in variants:
            lines = candidate.splitlines()
            starts: list[tuple[int, int]] = []
            for index in range(len(lines)):
                span = marker_span(lines, index, result_start)
                if span is not None:
                    starts.append((index, span))
            if not starts:
                continue
            saw_start = True
            for start_index, start_span in reversed(starts):
                end_match: tuple[int, int] | None = None
                for index in range(start_index + start_span, len(lines)):
                    span = marker_span(lines, index, result_end)
                    if span is not None:
                        end_match = (index, span)
                        break
                if end_match is None:
                    continue
                saw_end = True
                end_index, _ = end_match
                isolated = "\n".join(lines[start_index + start_span:end_index]).strip()
                if isolated:
                    return isolated

    if saw_start and not saw_end:
        raise CognitiveRouterError("cognitive_result_end_marker_missing")
    if saw_start:
        raise CognitiveRouterError("empty_marked_cognitive_result")
    raise CognitiveRouterError("cognitive_result_markers_missing")


def _runtime_terminal_error(value: Any, request_id: str) -> str | None:
    """Classify terminal-side failures correlated to the current request.

    Pane history may contain stale errors from older requests, so only inspect the
    segment that follows the latest occurrence of this request id and stop before
    any newer HIRDA cognitive request.
    """
    text = "\n".join(_text_fragments(value))
    if not text:
        return None
    needle = f"request_id: {request_id}"
    start = text.rfind(needle)
    if start < 0:
        return None
    segment = text[start + len(needle):]
    next_request = segment.find("HIRDA COGNITIVE REQUEST")
    if next_request >= 0:
        segment = segment[:next_request]
    lowered = segment.casefold()

    context_errors = (
        "input exceeds the context window",
        "maximum context length",
        "context length exceeded",
        "prompt is too long",
    )
    if any(marker in lowered for marker in context_errors):
        return "runtime_context_exhausted"

    rate_limit_errors = (
        "rate limited",
        "rate limit",
        "too many requests",
        "http 429",
        "retrying in ",
    )
    if any(marker in lowered for marker in rate_limit_errors):
        return "runtime_rate_limited"

    quota_errors = (
        "out of funds",
        "does not include paid service access",
        "upgrade or add credits",
        "insufficient credits",
        "quota exceeded",
        "usage limit has been reached",
        "usage limit reached",
        "hit your usage limit",
    )
    if any(marker in lowered for marker in quota_errors):
        return "runtime_quota_exhausted"

    auth_errors = (
        "http 401",
        "api key is invalid",
        "rejected your sign-in",
        "authentication failed",
        "unauthorized",
    )
    if any(marker in lowered for marker in auth_errors):
        return "runtime_auth_failed"

    return None


async def evaluate_cognitive_request(studio: Any, request: CognitiveRequest) -> JevCognitiveJudgment:
    enabled = bool(getattr(studio, "jev_enabled", False))
    mode = str(getattr(studio, "jev_mode", "shadow") or "shadow").strip().lower()
    if mode not in {"shadow", "teacher", "enforce"}:
        mode = "shadow"
    model = str(getattr(studio, "jev_model", "jev-latest") or "jev-latest").strip()

    def fallback(error: str | None = None) -> JevCognitiveJudgment:
        return JevCognitiveJudgment(
            schema=COGNITIVE_JUDGMENT_SCHEMA,
            enabled=enabled,
            evaluated=False,
            mode=mode,
            request_id=request.request_id,
            suggested_lane=None,
            confidence=0.0,
            needs_tool=None,
            needs_specialist=None,
            model=model if enabled else None,
            error=error,
        )

    if not enabled:
        return fallback()

    import os

    key_env = str(getattr(studio, "jev_api_key_env", "TYPESAFE_API_KEY") or "TYPESAFE_API_KEY")
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        return fallback("missing_api_key")

    max_state_chars = max(512, int(getattr(studio, "jev_max_state_chars", 12000) or 12000))
    context_json = json.dumps(request.context, ensure_ascii=False, default=str, separators=(",", ":"))
    bounded_context: Any
    if len(context_json) <= max_state_chars:
        bounded_context = request.context
    else:
        bounded_context = {
            "truncated": True,
            "preview": context_json[:max_state_chars],
        }

    state = {
        "system": "HIRDA cognitive routing advisor",
        "request": {
            "request_id": request.request_id,
            "capability": request.capability,
            "preferred_depth": request.preferred_depth,
            "prompt": request.prompt[: min(8000, max_state_chars)],
            "context": bounded_context,
        },
        "authority": {
            "jev_is_advisory_only": True,
            "jev_has_no_routing_or_action_authority": True,
            "hirda_deterministic_policy_remains_authoritative": True,
        },
    }
    questions = {
        "task_type": {
            "type": "choice",
            "instructions": (
                "Classify the semantic task shape. This is advisory evidence only; "
                "HIRDA retains routing authority and the declared capability remains authoritative."
            ),
            "criteria": {
                "conversation": "Social dialogue, interpersonal response, or persona-continuous conversation.",
                "software_engineering": "Software design, coding, debugging, repository editing, or testing.",
                "research": "Evidence gathering, source comparison, synthesis, or investigation.",
                "planning": "Multi-step planning, decomposition, scheduling, or strategy formation.",
                "tool_operation": "The main need is bounded external tool or system operation.",
                "general_reasoning": "Complex reasoning that does not fit a more specific category.",
                "reflection": "Introspection, interpretation, or reflective synthesis.",
                "utility": "Routine transformation, formatting, extraction, or lightweight assistance.",
            },
        },
        "coding": {
            "type": "noul",
            "instructions": "How strongly does this task require software-engineering or coding capability?",
        },
        "latency_priority": {
            "type": "choice",
            "instructions": "Choose the task latency preference, not a provider or runtime.",
            "criteria": {
                "high": "Low latency materially improves the interaction and short reasoning is acceptable.",
                "balanced": "Balance response time and reasoning quality.",
                "quality": "Reasoning quality should dominate latency for this task.",
            },
        },
        "compute_lane": {
            "type": "choice",
            "instructions": (
                "Choose the minimum compute depth needed for this request. Use fast for routine, "
                "clear, low-consequence work. Use deep for ambiguity, multi-step reasoning, "
                "conflicting evidence, consequential social nuance, planning, coding, or research."
            ),
            "criteria": {
                "fast": "Routine or clear task where short reasoning is sufficient.",
                "deep": "Complex, ambiguous, consequential, specialist, or multi-step reasoning.",
            },
        },
        "needs_tool": {
            "type": "noul",
            "instructions": "Would a sound answer require bounded tool or external state access beyond the supplied context?",
        },
        "needs_specialist": {
            "type": "noul",
            "instructions": "Would a specialist runtime materially improve correctness for this request?",
        },
    }
    base_url = str(
        getattr(studio, "jev_api_url", "https://api.typesafe.ai/v1/systemone") or ""
    ).strip()
    timeout = max(0.1, float(getattr(studio, "jev_timeout_seconds", 2.0) or 2.0))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                base_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "state": state, "questions": questions},
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        return fallback(type(exc).__name__)

    if not isinstance(body, dict):
        return fallback("jev_invalid_response")
    answers = _answer_map(body)
    lane_answer = answers.get("compute_lane")
    if not isinstance(lane_answer, dict):
        return fallback("jev_invalid_response")
    lane = str(lane_answer.get("choice") or "").strip().casefold()
    if lane not in {"fast", "deep"}:
        return fallback("jev_invalid_lane")

    def noul(name: str) -> float | None:
        value = answers.get(name)
        if not isinstance(value, dict) or "noul" not in value:
            return None
        return _probability(value.get("noul"), 0.5)

    def choice(name: str, allowed: set[str]) -> str | None:
        value = answers.get(name)
        if not isinstance(value, dict):
            return None
        selected = str(value.get("choice") or "").strip().casefold()
        return selected if selected in allowed else None

    return JevCognitiveJudgment(
        schema=COGNITIVE_JUDGMENT_SCHEMA,
        enabled=True,
        evaluated=True,
        mode=mode,
        request_id=request.request_id,
        suggested_lane=lane,
        confidence=_probability(lane_answer.get("confidence")),
        needs_tool=noul("needs_tool"),
        needs_specialist=noul("needs_specialist"),
        model=str(body.get("model") or model),
        error=None,
        task_type=choice("task_type", _TASK_TYPES),
        coding=noul("coding"),
        latency_priority=choice("latency_priority", _LATENCY_PRIORITIES),
    )


class CognitiveRouter:
    """HIRDA-owned cognitive routing across live agent runtimes.

    Semantic authority remains external to HIRDA. HIRDA selects a compute
    lane/runtime and can fail over between compatible runtimes. JEV is teacher evidence only.
    """

    def __init__(
        self,
        studio: Any,
        herdr: HerdrManager,
        runtimes: Any,
        capsules: Any | None = None,
        capability_router: Any | None = None,
        hermes_groups: HermesGroupClient | None = None,
    ) -> None:
        self.studio = studio
        self.herdr = herdr
        self.runtimes = runtimes
        self.capsules = capsules
        self.capability_router = capability_router
        self.hermes_groups = hermes_groups or HermesGroupClient(studio)
        self._runtime_recovery_streaks: dict[str, int] = {}
        self._runtime_recovery_task: asyncio.Task[None] | None = None
        # A Herdr pane is a single interactive agent stream. Parallel prompts to
        # the same pane can overwrite request correlation and interleave output,
        # so cognitive execution must be serialized per pane.
        self._pane_execution_locks: dict[str, asyncio.Lock] = {}

    async def start_runtime_recovery(self) -> None:
        if self.capsules is None or not bool(
            getattr(self.studio, "cognitive_runtime_auto_recovery_enabled", True)
        ):
            return
        if self._runtime_recovery_task is not None and not self._runtime_recovery_task.done():
            return
        self._runtime_recovery_task = asyncio.create_task(
            self._runtime_recovery_loop(),
            name="hirda-runtime-recovery",
        )

    async def stop_runtime_recovery(self) -> None:
        task = self._runtime_recovery_task
        self._runtime_recovery_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _runtime_recovery_loop(self) -> None:
        interval = max(
            5.0,
            float(getattr(self.studio, "cognitive_runtime_recovery_interval_seconds", 15.0) or 15.0),
        )
        while True:
            try:
                await self.herdr.refresh()
                runtime_snapshot = self.runtimes.snapshot(force=False)
                lane_states = await self.capsules.lane_states()
                await self._reconcile_runtime_lane_health(runtime_snapshot, lane_states)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db = getattr(self.capsules, "db", None)
                if db is not None and hasattr(db, "add_event"):
                    try:
                        await db.add_event(
                            "cognitive.runtime_recovery_error",
                            "Runtime recovery health loop failed.",
                            severity="warning",
                            data={"error": type(exc).__name__, "detail": str(exc)[:240]},
                        )
                    except Exception:
                        pass
            await asyncio.sleep(interval)


    def _node_routing_mode(self) -> str:
        mode = str(
            getattr(self.studio, "cognitive_node_routing_mode", "off")
            or "off"
        ).strip().lower()
        return mode if mode in {"off", "shadow", "enforce"} else "off"

    def _advertised_capability(self, request: CognitiveRequest) -> str | None:
        raw = getattr(self.studio, "cognitive_capability_routes", {})
        if not isinstance(raw, dict):
            return None
        value = raw.get(request.capability)
        capability = str(value or "").strip().lower()
        return capability or None

    def _runtime_binding(self, runtime_id: str) -> dict[str, str] | None:
        raw = getattr(self.studio, "cognitive_runtime_bindings", {})
        if not isinstance(raw, dict):
            return None

        selected: Any = None
        for key, value in raw.items():
            if str(key or "").strip().casefold() == runtime_id.casefold():
                selected = value
                break
        if not isinstance(selected, dict):
            return None

        machine_id = str(selected.get("machine_id") or "").strip()
        backend_id = str(selected.get("backend_id") or "").strip().lower()
        model_id = str(selected.get("model_id") or "").strip()
        if not machine_id:
            return None
        if model_id and not backend_id:
            return None
        return {
            "machine_id": machine_id,
            "backend_id": backend_id,
            "model_id": model_id,
        }

    def _apply_infrastructure_routing(
        self,
        request: CognitiveRequest,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        mode = self._node_routing_mode()
        report: dict[str, Any] = {
            "schema": "hirda-cognitive-infrastructure-routing-v1",
            "mode": mode,
            "enabled": mode != "off",
            "advertised_capability": None,
            "permission": "execute",
            "candidate_count_before": len(candidates),
            "candidate_count_after": len(candidates),
            "decisions": [],
            "eligible_runtimes": [],
            "filtered_runtimes": [],
            "enforced": mode == "enforce",
            "routing_authority": (
                "infrastructure_eligibility_only"
                if mode != "off"
                else "disabled"
            ),
            "semantic_authority_changed": False,
        }
        if mode == "off":
            return candidates, report

        advertised_capability = self._advertised_capability(request)
        report["advertised_capability"] = advertised_capability
        filtered: list[dict[str, Any]] = []

        for row in candidates:
            candidate = dict(row)
            runtime_id = str(candidate.get("id") or "").strip().casefold()
            binding = self._runtime_binding(runtime_id)
            decision: dict[str, Any] = {
                "runtime": runtime_id,
                "binding": dict(binding) if binding is not None else None,
                "eligible": False,
                "selected": None,
                "reason": None,
            }

            if advertised_capability is None:
                decision["reason"] = "cognitive_capability_route_unmapped"
            elif binding is None:
                decision["reason"] = "cognitive_runtime_binding_missing"
            elif self.capability_router is None:
                decision["reason"] = "capability_router_unavailable"
            else:
                try:
                    resolved = self.capability_router.resolve_capability_route(
                        advertised_capability,
                        permission="execute",
                        machine_id=binding["machine_id"],
                        backend_id=binding["backend_id"] or None,
                        model_id=binding["model_id"] or None,
                    )
                except Exception as exc:
                    decision["reason"] = (
                        f"capability_route_error:{type(exc).__name__}"
                    )
                else:
                    selected = resolved.get("selected")
                    if isinstance(selected, dict):
                        decision["eligible"] = True
                        decision["selected"] = dict(selected)
                        decision["reason"] = None
                        candidate["_infrastructure_route"] = dict(selected)
                    else:
                        decision["reason"] = str(
                            resolved.get("no_route_reason")
                            or "no_eligible_route"
                        )
                        decision["route_rejections"] = list(
                            resolved.get("rejected") or []
                        )

            report["decisions"].append(decision)
            if mode == "shadow" or bool(decision["eligible"]):
                filtered.append(candidate)

        report["candidate_count_after"] = len(filtered)
        report["enforced"] = mode == "enforce"
        report["eligible_runtimes"] = [
            item["runtime"]
            for item in report["decisions"]
            if item["eligible"]
        ]
        report["filtered_runtimes"] = (
            [
                item["runtime"]
                for item in report["decisions"]
                if not item["eligible"]
            ]
            if mode == "enforce"
            else []
        )
        report["routing_authority"] = "infrastructure_eligibility_only"
        report["semantic_authority_changed"] = False
        return filtered, report

    @staticmethod
    def _deterministic_lane(request: CognitiveRequest) -> str:
        if request.preferred_depth in {"fast", "deep"}:
            return request.preferred_depth
        if request.capability in {"reasoning_high", "coding", "research"}:
            return "deep"
        return "fast"

    @staticmethod
    def _runtime_healthy(row: dict[str, Any]) -> bool:
        if not bool(row.get("installed") or row.get("observed")):
            return False
        if str(row.get("status") or "").casefold() in {"down", "error", "unavailable"}:
            return False
        auth = row.get("auth_health")
        if isinstance(auth, dict) and str(auth.get("status") or "").casefold() in {
            "invalid",
            "expired",
            "missing",
            "error",
        }:
            return False
        limit = row.get("limit_health")
        if isinstance(limit, dict) and str(limit.get("status") or "").casefold() == "exhausted":
            return False
        return True

    @staticmethod
    def _runtime_health_reason(row: dict[str, Any]) -> str:
        if not bool(row.get("installed") or row.get("observed")):
            return "runtime_unavailable"
        status = str(row.get("status") or "").strip().casefold()
        if status in {"down", "error", "unavailable"}:
            return f"runtime_status_{status}"
        auth = row.get("auth_health")
        if isinstance(auth, dict):
            auth_status = str(auth.get("status") or "").strip().casefold()
            if auth_status in {"invalid", "expired", "missing", "error"}:
                return f"auth_{auth_status}"
        limit = row.get("limit_health")
        if isinstance(limit, dict):
            limit_status = str(limit.get("status") or "").strip().casefold()
            if limit_status == "exhausted":
                return "limit_exhausted"
        return "healthy"


    @staticmethod
    def _parse_runtime_backoff_reason(reason: Any) -> dict[str, Any]:
        text = str(reason or "").strip()
        if not text.startswith(_RUNTIME_BACKOFF_REASON_PREFIX + ";"):
            return {}
        parsed: dict[str, Any] = {}
        for item in text.split(";")[1:]:
            key, sep, value = item.partition("=")
            if not sep or not key:
                continue
            parsed[key.strip()] = value.strip()
        for key in ("until", "seconds"):
            raw = parsed.get(key)
            if raw is None:
                continue
            try:
                parsed[key] = float(raw)
            except (TypeError, ValueError):
                parsed.pop(key, None)
        raw_failures = parsed.get("failures")
        if raw_failures is not None:
            try:
                parsed["failures"] = max(0, int(raw_failures))
            except (TypeError, ValueError):
                parsed.pop("failures", None)
        return parsed

    def _runtime_backoff_base_seconds(self, error_code: str) -> float | None:
        if not bool(
            getattr(
                self.studio,
                "cognitive_runtime_backoff_enabled",
                True,
            )
        ):
            return None
        if error_code == "runtime_rate_limited":
            return max(
                1.0,
                float(
                    getattr(
                        self.studio,
                        "cognitive_runtime_rate_limit_backoff_seconds",
                        600.0,
                    )
                    or 600.0
                ),
            )
        if error_code == "runtime_quota_exhausted":
            return max(
                1.0,
                float(
                    getattr(
                        self.studio,
                        "cognitive_runtime_quota_backoff_seconds",
                        3600.0,
                    )
                    or 3600.0
                ),
            )
        return None

    async def _record_runtime_backoff(
        self,
        runtime_id: str,
        error_code: str,
    ) -> dict[str, Any] | None:
        base_seconds = self._runtime_backoff_base_seconds(error_code)
        if base_seconds is None or self.capsules is None:
            return None
        runtime_id = str(runtime_id or "").strip().casefold()
        if not runtime_id:
            return None

        lanes = await self.capsules.lane_states()
        current = dict(lanes.get(runtime_id) or {})
        previous = (
            self._parse_runtime_backoff_reason(current.get("reason"))
            if str(current.get("actor") or "") == _RUNTIME_BACKOFF_ACTOR
            else {}
        )
        failures = int(previous.get("failures") or 0) + 1
        max_seconds = max(
            base_seconds,
            float(
                getattr(
                    self.studio,
                    "cognitive_runtime_backoff_max_seconds",
                    21600.0,
                )
                or 21600.0
            ),
        )
        multiplier = float(2 ** min(max(0, failures - 1), 6))
        seconds = min(max_seconds, base_seconds * multiplier)
        until_epoch = time.time() + seconds
        reason = (
            f"{_RUNTIME_BACKOFF_REASON_PREFIX};"
            f"error={error_code};"
            f"until={until_epoch:.3f};"
            f"failures={failures};"
            f"seconds={seconds:.3f}"
        )
        result = await self.capsules.set_lane_state(
            runtime_id,
            "emergency",
            reason=reason,
            actor=_RUNTIME_BACKOFF_ACTOR,
            auto_handoff=True,
        )
        return {
            "schema": RUNTIME_BACKOFF_SCHEMA,
            "runtime": runtime_id,
            "active": True,
            "error": error_code,
            "failures": failures,
            "seconds": round(seconds, 3),
            "until_epoch": round(until_epoch, 3),
            "remaining_seconds": round(seconds, 3),
            "lane": dict(result.get("lane") or {}),
        }

    async def _clear_runtime_backoff(self, runtime_id: str) -> None:
        if self.capsules is None:
            return
        runtime_id = str(runtime_id or "").strip().casefold()
        if not runtime_id:
            return
        lanes = await self.capsules.lane_states()
        current = dict(lanes.get(runtime_id) or {})
        if str(current.get("actor") or "") != _RUNTIME_BACKOFF_ACTOR:
            return
        await self.capsules.set_lane_state(
            runtime_id,
            "normal",
            reason="runtime_backoff_cleared_after_success",
            actor=_RUNTIME_BACKOFF_ACTOR,
            auto_handoff=False,
        )

    async def _reconcile_runtime_backoffs(
        self,
        lane_states: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if self.capsules is None:
            return lane_states
        now = time.time()
        current_states = dict(lane_states)
        for runtime_id, lane_value in list(current_states.items()):
            lane = dict(lane_value or {})
            if str(lane.get("actor") or "") != _RUNTIME_BACKOFF_ACTOR:
                continue
            parsed = self._parse_runtime_backoff_reason(lane.get("reason"))
            until_epoch = parsed.get("until")
            if (
                str(lane.get("state") or "").strip().casefold() == "normal"
                or not isinstance(until_epoch, (int, float))
                or until_epoch > now
            ):
                continue
            reason = (
                f"{_RUNTIME_BACKOFF_REASON_PREFIX};"
                f"error={parsed.get('error') or 'runtime_limit'};"
                f"until={float(until_epoch):.3f};"
                f"failures={int(parsed.get('failures') or 1)};"
                f"seconds={float(parsed.get('seconds') or 0.0):.3f};"
                "probe=1"
            )
            result = await self.capsules.set_lane_state(
                runtime_id,
                "normal",
                reason=reason,
                actor=_RUNTIME_BACKOFF_ACTOR,
                auto_handoff=False,
            )
            current_states[runtime_id] = dict(result.get("lane") or lane)
        return current_states

    async def runtime_backoff_status(self) -> dict[str, Any]:
        if self.capsules is None:
            return {
                "schema": RUNTIME_BACKOFF_SCHEMA,
                "enabled": False,
                "active_count": 0,
                "runtimes": {},
            }
        lanes = await self.capsules.lane_states()
        now = time.time()
        runtimes: dict[str, Any] = {}
        active_count = 0
        for runtime_id, lane_value in lanes.items():
            lane = dict(lane_value or {})
            if str(lane.get("actor") or "") != _RUNTIME_BACKOFF_ACTOR:
                continue
            parsed = self._parse_runtime_backoff_reason(lane.get("reason"))
            until_epoch = parsed.get("until")
            active = bool(
                str(lane.get("state") or "").strip().casefold() != "normal"
                and isinstance(until_epoch, (int, float))
                and float(until_epoch) > now
            )
            if active:
                active_count += 1
            runtimes[str(runtime_id)] = {
                "runtime": str(runtime_id),
                "active": active,
                "probe_due": bool(
                    str(lane.get("state") or "").strip().casefold() == "normal"
                    and str(lane.get("reason") or "").find("probe=1") >= 0
                ),
                "error": parsed.get("error"),
                "failures": int(parsed.get("failures") or 0),
                "seconds": parsed.get("seconds"),
                "until_epoch": until_epoch,
                "remaining_seconds": (
                    round(max(0.0, float(until_epoch) - now), 3)
                    if isinstance(until_epoch, (int, float))
                    else None
                ),
                "lane_state": lane.get("state"),
                "reason": lane.get("reason"),
                "updated_at": lane.get("updated_at"),
            }
        return {
            "schema": RUNTIME_BACKOFF_SCHEMA,
            "enabled": bool(
                getattr(
                    self.studio,
                    "cognitive_runtime_backoff_enabled",
                    True,
                )
            ),
            "active_count": active_count,
            "runtimes": runtimes,
        }

    @staticmethod
    def decorate_runtime_snapshot_with_backoff(
        snapshot: dict[str, Any],
        backoff_status: dict[str, Any],
    ) -> dict[str, Any]:
        """Overlay execution-observed backoff onto runtime health for UI/status."""
        decorated = dict(snapshot or {})
        backoffs = (
            dict(backoff_status.get("runtimes") or {})
            if isinstance(backoff_status, dict)
            else {}
        )
        rows: list[dict[str, Any]] = []
        constrained = 0
        for raw in snapshot.get("runtimes", []) if isinstance(snapshot, dict) else []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            runtime_id = str(row.get("id") or "").strip().casefold()
            backoff = dict(backoffs.get(runtime_id) or {})
            if backoff:
                row["runtime_backoff"] = backoff
            if backoff.get("active"):
                limit = dict(row.get("limit_health") or {})
                error_code = str(backoff.get("error") or "")
                limit["status"] = (
                    "exhausted"
                    if error_code == "runtime_quota_exhausted"
                    else "limited"
                )
                limit["source"] = "runtime_backoff"
                limit["detail"] = (
                    "HIRDA observed a provider limit during live execution; "
                    "this runtime is temporarily excluded from new cognitive work."
                )
                limit["reset_at"] = backoff.get("until_epoch")
                row["limit_health"] = limit
            limit = row.get("limit_health")
            limit_status = (
                str(limit.get("status") or "").strip().casefold()
                if isinstance(limit, dict)
                else ""
            )
            if limit_status in {"limited", "exhausted"}:
                constrained += 1
            rows.append(row)
        decorated["runtimes"] = rows
        summary = dict(decorated.get("summary") or {})
        summary["limit_constrained"] = constrained
        decorated["summary"] = summary
        return decorated

    async def _reconcile_runtime_lane_health(
        self,
        runtime_snapshot: dict[str, Any],
        lane_states: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if self.capsules is None or not bool(
            getattr(self.studio, "cognitive_runtime_auto_recovery_enabled", True)
        ):
            return lane_states

        threshold = max(
            1,
            int(getattr(self.studio, "cognitive_runtime_recovery_successes", 3) or 3),
        )
        rows = {
            str(row.get("id") or "").strip().casefold(): dict(row)
            for row in runtime_snapshot.get("runtimes", [])
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        }
        current_states = dict(lane_states)

        for runtime_id, row in rows.items():
            lane = dict(current_states.get(runtime_id) or {})
            state = str(lane.get("state") or "normal").strip().casefold()
            actor = str(lane.get("actor") or "").strip().casefold()
            known_runtime = bool(row.get("installed") or row.get("observed"))
            healthy = self._runtime_healthy(row)

            if not known_runtime:
                self._runtime_recovery_streaks[runtime_id] = 0
                continue

            if not healthy:
                self._runtime_recovery_streaks[runtime_id] = 0
                if state == "normal":
                    reason = self._runtime_health_reason(row)
                    result = await self.capsules.set_lane_state(
                        runtime_id,
                        "emergency",
                        reason=f"runtime_health:{reason}",
                        actor="hirda/runtime-health",
                        auto_handoff=True,
                    )
                    current_states[runtime_id] = dict(result.get("lane") or lane)
                continue

            if actor != "hirda/runtime-health" or state == "normal":
                self._runtime_recovery_streaks[runtime_id] = 0
                continue

            streak = self._runtime_recovery_streaks.get(runtime_id, 0) + 1
            self._runtime_recovery_streaks[runtime_id] = streak
            if streak < threshold:
                continue

            result = await self.capsules.set_lane_state(
                runtime_id,
                "normal",
                reason=f"runtime_recovered_after_{streak}_healthy_checks",
                actor="hirda/runtime-health",
                auto_handoff=False,
            )
            current_states[runtime_id] = dict(result.get("lane") or lane)
            self._runtime_recovery_streaks[runtime_id] = 0

        return current_states

    @staticmethod
    def _pane_reserved(pane: dict[str, Any]) -> bool:
        """Keep dedicated certification and rollover panes out of ordinary routing."""
        prefixes = (
            "hirda-certification",
            "hirda-live-certification",
            "hirda-auto-rollover",
        )
        name = str(pane.get("name") or "").strip().casefold()
        label = str(pane.get("label") or "").strip().casefold()
        return name.startswith(prefixes) or label.startswith(prefixes)

    def _pane_routing_mode(self) -> str:
        mode = str(
            getattr(self.studio, "cognitive_pane_routing_mode", "off")
            or "off"
        ).strip().casefold()
        return mode if mode in {"off", "shadow", "enforce"} else "off"

    def _runtime_pane_selectors(self, runtime_id: str) -> dict[str, tuple[str, ...]]:
        raw_bindings = getattr(self.studio, "cognitive_runtime_bindings", {})
        selected: Any = None
        if isinstance(raw_bindings, dict):
            for key, value in raw_bindings.items():
                if str(key or "").strip().casefold() == runtime_id.casefold():
                    selected = value
                    break
        if not isinstance(selected, dict):
            selected = {}

        def values(key: str) -> tuple[str, ...]:
            raw = selected.get(key, [])
            if not isinstance(raw, list):
                return ()
            return tuple(
                str(value).strip()
                for value in raw
                if str(value or "").strip()
            )

        principal_prefixes: list[str] = []
        raw_principals = selected.get("principal_pane_name_prefixes")
        if isinstance(raw_principals, dict):
            for raw_values in raw_principals.values():
                items = [raw_values] if isinstance(raw_values, str) else raw_values
                if not isinstance(items, list):
                    continue
                principal_prefixes.extend(
                    str(value).strip().casefold()
                    for value in items
                    if str(value or "").strip()
                )

        pane_name_prefixes = [
            value.casefold()
            for value in values("pane_name_prefixes")
        ]
        pane_name_prefixes.extend(principal_prefixes)
        pane_name_prefixes.extend(
            prefix
            for (system_runtime, _), prefixes in _SYSTEM_PRINCIPAL_PANE_PREFIXES.items()
            if system_runtime == runtime_id.casefold()
            for prefix in prefixes
        )

        return {
            "pane_ids": values("pane_ids"),
            "pane_name_prefixes": tuple(dict.fromkeys(pane_name_prefixes)),
            "pane_label_prefixes": tuple(
                value.casefold() for value in values("pane_label_prefixes")
            ),
        }



    def _runtime_bootstrap_contract(self, runtime_id: str) -> dict[str, str]:
        """Return HIRDA-owned attribution for an isolated runtime launch."""
        runtime_key = str(runtime_id or "").strip().casefold()
        raw_bindings = getattr(self.studio, "cognitive_runtime_bindings", {})
        selected: Any = None
        if isinstance(raw_bindings, dict):
            for key, value in raw_bindings.items():
                if str(key or "").strip().casefold() == runtime_key:
                    selected = value
                    break
        if not isinstance(selected, dict) or runtime_key != "hermes":
            return {}

        provider = str(selected.get("launch_provider") or "").strip()
        model = str(selected.get("launch_model") or "").strip()
        reasoning = str(
            selected.get("launch_reasoning") or ""
        ).strip().casefold()
        if bool(provider) != bool(model):
            raise CognitiveRouterError(
                "cognitive_runtime_bootstrap_launch_contract_incomplete"
            )
        return {
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
        }

    def _runtime_bootstrap_agent_args(self, runtime_id: str) -> list[str]:
        """Return HIRDA-owned CLI launch constraints for isolated runtimes."""
        contract = self._runtime_bootstrap_contract(runtime_id)
        provider = contract.get("provider", "")
        model = contract.get("model", "")
        reasoning = contract.get("reasoning", "")

        args: list[str] = []
        if provider and model:
            args.extend(["--provider", provider, "--model", model])
        if reasoning:
            args.extend(["--reasoning", reasoning])
        permissions = resolve_agent_permissions(
            runtime_id,
            permission_policy="auto_accept",
            agent_args=args,
        )
        return list(permissions["agent_args"])


    def _request_principal_id(
        self,
        request: CognitiveRequest | None,
    ) -> str | None:
        if request is None:
            return None
        context = dict(request.context or {})
        return (
            str(
                request.principal_id
                or request.agent_id
                or context.get("principal_id")
                or context.get("owner_agent_id")
                or context.get("persona_id")
                or ""
            ).strip()
            or None
        )

    def _principal_pane_name_prefixes(
        self,
        runtime_id: str,
        principal_id: str | None,
    ) -> tuple[str, ...]:
        if not principal_id:
            return ()

        runtime_key = str(runtime_id or "").strip().casefold()
        principal_key = str(principal_id or "").strip().casefold()

        raw_bindings = getattr(self.studio, "cognitive_runtime_bindings", {})
        selected: Any = None
        if isinstance(raw_bindings, dict):
            for key, value in raw_bindings.items():
                if str(key or "").strip().casefold() == runtime_key:
                    selected = value
                    break
        if isinstance(selected, dict):
            raw_map = selected.get("principal_pane_name_prefixes")
            if isinstance(raw_map, dict):
                for key, raw_values in raw_map.items():
                    if str(key or "").strip().casefold() != principal_key:
                        continue
                    if isinstance(raw_values, str):
                        raw_values = [raw_values]
                    if not isinstance(raw_values, list):
                        return ()
                    configured = tuple(
                        str(value).strip().casefold()
                        for value in raw_values
                        if str(value or "").strip()
                    )
                    if configured:
                        return configured

        return _SYSTEM_PRINCIPAL_PANE_PREFIXES.get(
            (runtime_key, principal_key),
            (),
        )

    def _pane_claimed_by_any_principal(
        self,
        pane: dict[str, Any],
        runtime_id: str,
    ) -> bool:
        raw_bindings = getattr(self.studio, "cognitive_runtime_bindings", {})
        selected: Any = None
        if isinstance(raw_bindings, dict):
            for key, value in raw_bindings.items():
                if str(key or "").strip().casefold() == runtime_id.casefold():
                    selected = value
                    break
        if not isinstance(selected, dict):
            return False
        raw_map = selected.get("principal_pane_name_prefixes")
        if not isinstance(raw_map, dict):
            return False
        return any(
            self._pane_owned_by_principal(
                pane,
                runtime_id,
                str(principal_id or "").strip(),
            )
            for principal_id in raw_map
            if str(principal_id or "").strip()
        )

    def _pane_owned_by_principal(
        self,
        pane: dict[str, Any],
        runtime_id: str,
        principal_id: str | None,
    ) -> bool:
        if not principal_id:
            return True
        prefixes = self._principal_pane_name_prefixes(
            runtime_id,
            principal_id,
        )
        if not prefixes:
            return False
        names = tuple(
            str(pane.get(key) or "").strip().casefold()
            for key in ("agent_name", "name", "label")
            if str(pane.get(key) or "").strip()
        )
        return any(
            name.startswith(prefix)
            for name in names
            for prefix in prefixes
        )

    def _pane_owned_by_runtime(
        self,
        pane: dict[str, Any],
        runtime_id: str,
    ) -> bool:
        if str(pane.get("agent") or "").strip().casefold() != runtime_id.casefold():
            return False
        selectors = self._runtime_pane_selectors(runtime_id)
        pane_id = str(pane.get("pane_id") or "").strip()
        names = tuple(
            str(pane.get(key) or "").strip().casefold()
            for key in ("agent_name", "name", "label")
            if str(pane.get(key) or "").strip()
        )
        if pane_id and pane_id in selectors["pane_ids"]:
            return True
        if names and any(
            name.startswith(prefix)
            for name in names
            for prefix in selectors["pane_name_prefixes"]
        ):
            return True
        label = str(pane.get("label") or "").strip().casefold()
        if label and any(
            label.startswith(prefix)
            for prefix in selectors["pane_label_prefixes"]
        ):
            return True
        return False


    def _pane_snapshot_rows(self) -> list[dict[str, Any]]:
        snapshot = getattr(self.herdr, "snapshot", {})
        pane_list = getattr(self.herdr, "pane_list", None)
        if callable(pane_list):
            raw_rows = pane_list(snapshot)
            if not isinstance(raw_rows, list):
                return []
            return [
                dict(row)
                for row in raw_rows
                if isinstance(row, dict)
            ]

        panes_block = snapshot.get("panes") if isinstance(snapshot, dict) else {}
        raw_rows = (
            panes_block.get("panes", [])
            if isinstance(panes_block, dict)
            else panes_block
            if isinstance(panes_block, list)
            else []
        )
        if not isinstance(raw_rows, list):
            return []
        return [
            dict(row)
            for row in raw_rows
            if isinstance(row, dict)
        ]

    def pane_routing_status(self) -> dict[str, Any]:
        panes = self._pane_snapshot_rows()
        raw_bindings = getattr(self.studio, "cognitive_runtime_bindings", {})
        runtime_ids = sorted(
            str(key or "").strip().casefold()
            for key in raw_bindings
            if str(key or "").strip()
        ) if isinstance(raw_bindings, dict) else []
        runtimes: dict[str, Any] = {}
        for runtime_id in runtime_ids:
            matched = [
                dict(pane)
                for pane in panes
                if isinstance(pane, dict)
                and self._pane_owned_by_runtime(pane, runtime_id)
            ]
            runtimes[runtime_id] = {
                "selectors": {
                    key: list(value)
                    for key, value in self._runtime_pane_selectors(runtime_id).items()
                },
                "owned_pane_count": len(matched),
                "available_owned_pane_count": sum(
                    1
                    for pane in matched
                    if str(pane.get("agent_status") or "").casefold()
                    in {"idle", "done"}
                ),
                "owned_panes": [
                    {
                        "pane_id": pane.get("pane_id"),
                        "agent_name": pane.get("agent_name"),
                        "name": pane.get("name"),
                        "label": pane.get("label"),
                        "status": pane.get("agent_status"),
                    }
                    for pane in matched
                ],
            }
        return {
            "schema": "hirda-cognitive-pane-routing-v1",
            "mode": self._pane_routing_mode(),
            "enforced": self._pane_routing_mode() == "enforce",
            "runtimes": runtimes,
            "ready_runtime_count": sum(
                1
                for value in runtimes.values()
                if value["available_owned_pane_count"] > 0
            ),
        }

    def _find_runtime_pane(
        self,
        runtime_id: str,
        request: CognitiveRequest | None = None,
    ) -> dict[str, Any] | None:
        panes = self._pane_snapshot_rows()
        candidates = [
            dict(pane)
            for pane in panes
            if isinstance(pane, dict)
            and str(pane.get("agent") or "").casefold() == runtime_id
            and not self._pane_reserved(pane)
        ]
        mode = self._pane_routing_mode()
        if mode == "enforce":
            candidates = [
                pane
                for pane in candidates
                if self._pane_owned_by_runtime(pane, runtime_id)
            ]

        principal_id = self._request_principal_id(request)
        if principal_id:
            principal_candidates = [
                pane
                for pane in candidates
                if self._pane_owned_by_principal(
                    pane,
                    runtime_id,
                    principal_id,
                )
            ]
            if principal_candidates:
                candidates = principal_candidates
            elif mode == "enforce":
                return None
        elif mode == "enforce":
            candidates = [
                pane
                for pane in candidates
                if not self._pane_claimed_by_any_principal(
                    pane,
                    runtime_id,
                )
            ]

        if not candidates:
            return None
        rank = {"idle": 0, "done": 1, "unknown": 2}
        return sorted(
            candidates,
            key=lambda pane: (
                rank.get(str(pane.get("agent_status") or "").casefold(), 3),
                1 if pane.get("focused") else 0,
                str(pane.get("pane_id") or ""),
            ),
        )[0]

    async def _wait_for_owned_pane_available(
        self,
        runtime_id: str,
        pane_id: str,
        *,
        timeout_seconds: float,
        principal_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Wait for an owned cognitive pane to settle within the caller budget.

        Dedicated cognitive panes are queueable resources. A transient
        working state may outlive the request that produced the answer
        (for example while the harness finishes auxiliary/title work), so
        enforce mode waits for the owned pane instead of falling through to a
        different shared lane.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        current = self.herdr.find_pane(pane_id=pane_id)
        while current is not None:
            status = str(current.get("agent_status") or "unknown").casefold()
            if status in {"idle", "done"}:
                return current
            if not self._pane_owned_by_runtime(current, runtime_id):
                return current
            if (
                principal_id
                and not self._pane_owned_by_principal(
                    current,
                    runtime_id,
                    principal_id,
                )
            ):
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            await asyncio.sleep(min(0.5, remaining))
            await self.herdr.refresh()
            current = self.herdr.find_pane(pane_id=pane_id)
        return None

    def _runtime_capability_profile(self, row: dict[str, Any]) -> dict[str, Any]:
        runtime_id = str(row.get("id") or "").strip().casefold()
        base = row.get("capability_profile")
        profile = dict(base) if isinstance(base, dict) else {}
        profile["skills"] = dict(profile.get("skills") or {})

        overrides = getattr(
            self.studio,
            "cognitive_runtime_capability_overrides",
            {},
        )
        override = (
            overrides.get(runtime_id)
            if isinstance(overrides, dict)
            else None
        )
        if isinstance(override, dict):
            override_skills = override.get("skills")
            for key, value in override.items():
                if key != "skills":
                    profile[key] = value
            if isinstance(override_skills, dict):
                profile["skills"].update(override_skills)
            profile["source"] = (
                "config_override"
                if not base
                else "builtin_runtime_profile+config_override"
            )

        profile.setdefault("schema", "hirda-runtime-capability-profile-v1")
        profile.setdefault("source", "unprofiled_runtime")
        return profile

    @staticmethod
    def _semantic_score(value: Any, default: float = 0.5) -> float:
        return _probability(value, default)

    @staticmethod
    def _runtime_quota_score(row: dict[str, Any]) -> float:
        limit = row.get("limit_health")
        status = (
            str(limit.get("status") or "").strip().casefold()
            if isinstance(limit, dict)
            else ""
        )
        if status == "limited":
            return 0.40
        if status == "exhausted":
            return 0.0
        if status in {"healthy", "available", "ok", "unlimited"}:
            return 1.0
        if status == "interactive_only":
            return 0.80
        return 0.70

    @staticmethod
    def _semantic_ranking_weights(task_profile: dict[str, Any]) -> dict[str, float]:
        declared = str(
            task_profile.get("declared_capability") or ""
        ).strip().casefold()
        if declared == "social_dialogue":
            return {
                "capability_fit": 0.64,
                "quality": 0.15,
                "latency": 0.08,
                "cost": 0.05,
                "quota": 0.05,
                "tool_fit": 0.03,
            }

        capabilities = {
            str(item).strip().casefold()
            for item in task_profile.get("capabilities", [])
            if str(item).strip()
        }
        if "tool_operation" in capabilities:
            return {
                "capability_fit": 0.51,
                "quality": 0.08,
                "latency": 0.08,
                "cost": 0.06,
                "quota": 0.07,
                "tool_fit": 0.20,
            }

        priority = str(
            task_profile.get("latency_priority") or "balanced"
        ).strip().casefold()
        if priority == "high":
            return {
                "capability_fit": 0.56,
                "quality": 0.05,
                "latency": 0.18,
                "cost": 0.10,
                "quota": 0.06,
                "tool_fit": 0.05,
            }
        if priority == "quality":
            return {
                "capability_fit": 0.62,
                "quality": 0.16,
                "latency": 0.04,
                "cost": 0.05,
                "quota": 0.07,
                "tool_fit": 0.06,
            }
        return {
            "capability_fit": 0.60,
            "quality": 0.10,
            "latency": 0.10,
            "cost": 0.07,
            "quota": 0.07,
            "tool_fit": 0.06,
        }

    def _score_semantic_candidate(
        self,
        row: dict[str, Any],
        task_profile: dict[str, Any],
    ) -> dict[str, Any]:
        runtime_id = str(row.get("id") or "").strip().casefold()
        runtime_profile = self._runtime_capability_profile(row)
        skills = runtime_profile.get("skills")
        skills = skills if isinstance(skills, dict) else {}

        requested = [
            str(item).strip().casefold()
            for item in task_profile.get("capabilities", [])
            if str(item).strip()
        ]
        weighted_skill_scores: list[tuple[float, float]] = [
            (
                self._semantic_score(skills.get(capability), 0.5),
                1.0,
            )
            for capability in requested
        ]

        coding_need = self._semantic_score(
            task_profile.get("coding"),
            0.0,
        )
        if coding_need > 0.0:
            weighted_skill_scores.append(
                (
                    self._semantic_score(skills.get("coding"), 0.5),
                    max(0.25, 2.0 * coding_need),
                )
            )

        if weighted_skill_scores:
            skill_weight = sum(weight for _, weight in weighted_skill_scores)
            capability_fit = sum(
                score * weight for score, weight in weighted_skill_scores
            ) / skill_weight
        else:
            capability_fit = 0.5

        tool_need_raw = task_profile.get("tool_need")
        tool_need = (
            self._semantic_score(tool_need_raw, 0.5)
            if tool_need_raw is not None
            else 0.5
        )
        tool_use = self._semantic_score(
            runtime_profile.get("tool_use"),
            self._semantic_score(skills.get("tool_operation"), 0.5),
        )
        tool_fit = ((1.0 - tool_need) * 1.0) + (tool_need * tool_use)

        free = bool(row.get("free"))
        components = {
            "capability_fit": capability_fit,
            "quality": self._semantic_score(runtime_profile.get("quality"), 0.5),
            "latency": self._semantic_score(runtime_profile.get("latency"), 0.5),
            "cost": (
                1.0
                if free
                else self._semantic_score(
                    runtime_profile.get("cost_efficiency"),
                    0.5,
                )
            ),
            "quota": self._runtime_quota_score(row),
            "tool_fit": tool_fit,
        }
        weights = self._semantic_ranking_weights(task_profile)
        score = sum(
            components[name] * weight
            for name, weight in weights.items()
        )

        return {
            "runtime": runtime_id,
            "score": round(score, 4),
            "profile_source": runtime_profile.get("source"),
            "components": {
                key: round(value, 4)
                for key, value in components.items()
            },
            "weights": dict(weights),
            "free": free,
            "status": row.get("status"),
        }

    def _semantic_runtime_ranking(
        self,
        request: CognitiveRequest,
        task_profile: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        mode = str(
            getattr(
                self.studio,
                "cognitive_semantic_routing_mode",
                "shadow",
            )
            or "shadow"
        ).strip().casefold()
        current_order = [
            str(row.get("id") or "").strip().casefold()
            for row in candidates
            if str(row.get("id") or "").strip()
        ]
        current_selected = current_order[0] if current_order else None

        if mode == "off":
            return {
                "schema": SEMANTIC_RUNTIME_RANKING_SCHEMA,
                "mode": "off",
                "applied": False,
                "authority": "disabled",
                "explicit_constraint": None,
                "current_order": current_order,
                "ranked_order": current_order,
                "current_selected": current_selected,
                "semantic_preference": current_selected,
                "would_select": current_selected,
                "effective_selected": current_selected,
                "changed": False,
                "top_margin": None,
                "min_margin": None,
                "guard_passed": False,
                "guard_reasons": ["semantic_routing_disabled"],
                "rankings": [],
            }

        current_position = {
            runtime_id: index for index, runtime_id in enumerate(current_order)
        }
        rankings = [
            self._score_semantic_candidate(row, task_profile)
            for row in candidates
        ]
        rankings.sort(
            key=lambda item: (
                -float(item.get("score") or 0.0),
                current_position.get(
                    str(item.get("runtime") or ""),
                    10_000,
                ),
            )
        )
        ranked_order = [
            str(item.get("runtime") or "")
            for item in rankings
            if str(item.get("runtime") or "")
        ]
        semantic_preference = ranked_order[0] if ranked_order else None

        top_margin: float | None = None
        if len(rankings) >= 2:
            top_margin = max(
                0.0,
                float(rankings[0].get("score") or 0.0)
                - float(rankings[1].get("score") or 0.0),
            )
        elif len(rankings) == 1:
            top_margin = 1.0

        explicit_constraint = None
        if request.target_pane_id:
            explicit_constraint = "target_pane_id"
        elif request.preferred_runtime:
            explicit_constraint = "preferred_runtime"

        would_select = (
            current_selected
            if explicit_constraint and current_selected
            else semantic_preference
        )
        changed = bool(
            would_select
            and current_selected
            and would_select != current_selected
        )

        min_margin = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(
                        self.studio,
                        "cognitive_semantic_canary_min_margin",
                        0.03,
                    )
                    or 0.0
                ),
            ),
        )
        guard_reasons: list[str] = []
        guard_passed = False
        applied = False
        effective_selected = current_selected

        if explicit_constraint:
            guard_reasons.append(f"explicit_{explicit_constraint}")
        elif not semantic_preference or not current_selected:
            guard_reasons.append("semantic_candidate_unavailable")
        elif not changed:
            guard_reasons.append("semantic_matches_current")
            guard_passed = True
        elif mode == "canary":
            if top_margin is None or top_margin < min_margin:
                guard_reasons.append("semantic_margin_below_threshold")
            else:
                guard_passed = True
                applied = True
                effective_selected = semantic_preference
        else:
            # Shadow mode reports the semantic preference but never changes
            # execution authority.
            guard_reasons.append("shadow_observation_only")

        return {
            "schema": SEMANTIC_RUNTIME_RANKING_SCHEMA,
            "mode": mode,
            "applied": applied,
            "authority": (
                "hirda_canary"
                if applied
                else "external"
            ),
            "explicit_constraint": explicit_constraint,
            "current_order": current_order,
            "ranked_order": ranked_order,
            "current_selected": current_selected,
            "semantic_preference": semantic_preference,
            "would_select": would_select,
            "effective_selected": effective_selected,
            "changed": changed,
            "top_margin": (
                round(top_margin, 4)
                if top_margin is not None
                else None
            ),
            "min_margin": (
                round(min_margin, 4)
                if mode == "canary"
                else None
            ),
            "guard_passed": guard_passed,
            "guard_reasons": guard_reasons,
            "rankings": rankings,
        }


    def _supervisor_policy(
        self,
        request: CognitiveRequest,
        task_profile: dict[str, Any],
        semantic_routing: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Choose strategic supervision without taking semantic specialist authority."""
        mode = str(
            getattr(
                self.studio,
                "cognitive_supervisor_policy_mode",
                "shadow",
            )
            or "shadow"
        ).strip().casefold()
        preferred_supervisor = str(
            getattr(
                self.studio,
                "cognitive_supervisor_runtime",
                "claude",
            )
            or "claude"
        ).strip().casefold()
        semantic_execution_runtime = str(
            semantic_routing.get("effective_selected") or ""
        ).strip().casefold() or None
        specialist_runtime = str(
            semantic_routing.get("semantic_preference")
            or semantic_execution_runtime
            or ""
        ).strip().casefold() or None
        candidate_ids = {
            str(row.get("id") or "").strip().casefold()
            for row in candidates
            if str(row.get("id") or "").strip()
        }
        explicit_runtime = str(
            request.preferred_runtime or ""
        ).strip().casefold() or None

        reasons: list[str] = []
        eligible = False
        if mode == "off":
            reasons.append("supervisor_policy_disabled")
        elif request.target_pane_id:
            reasons.append("explicit_target_pane_id")
        elif explicit_runtime and explicit_runtime != preferred_supervisor:
            reasons.append("explicit_preferred_runtime")
        elif task_profile.get("task_type") != "software_engineering":
            reasons.append("task_not_software_engineering")
        elif task_profile.get("reasoning_depth") != "deep":
            reasons.append("task_not_deep")
        elif preferred_supervisor not in candidate_ids:
            reasons.append("supervisor_runtime_unavailable")
        else:
            eligible = True
            reasons.append(
                "supervisor_policy_enforced"
                if mode == "enforce"
                else "shadow_observation_only"
            )

        applied = bool(eligible and mode == "enforce")
        effective_supervisor = (
            preferred_supervisor
            if applied
            else semantic_execution_runtime
        )
        return {
            "schema": SUPERVISOR_POLICY_SCHEMA,
            "mode": mode,
            "applied": applied,
            "authority": (
                "hirda_supervisor_policy"
                if applied
                else "external"
            ),
            "eligible": eligible,
            "preferred_supervisor": preferred_supervisor,
            "effective_supervisor": effective_supervisor,
            "semantic_execution_runtime": semantic_execution_runtime,
            "specialist_runtime": specialist_runtime,
            "explicit_runtime": explicit_runtime,
            "guard_passed": eligible,
            "guard_reasons": reasons,
            "semantic_authority_changed": False,
            "execution_order_changed": False,
        }

    def _hermes_team_delegation(
        self,
        request: CognitiveRequest,
        task_profile: dict[str, Any],
        supervisor_policy: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Guard controlled Claude -> Hermes -> Bots delegation."""
        mode = str(
            getattr(
                self.studio,
                "cognitive_hermes_team_delegation_mode",
                "shadow",
            )
            or "shadow"
        ).strip().casefold()
        min_skill = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(
                        self.studio,
                        "cognitive_hermes_team_min_skill",
                        0.80,
                    )
                    or 0.0
                ),
            ),
        )
        supervisor_runtime = str(
            supervisor_policy.get("effective_supervisor") or ""
        ).strip().casefold() or None
        hermes_row = next(
            (
                row
                for row in candidates
                if str(row.get("id") or "").strip().casefold() == "hermes"
            ),
            None,
        )
        hermes_profile = (
            self._runtime_capability_profile(hermes_row)
            if isinstance(hermes_row, dict)
            else {}
        )
        hermes_skills = dict(hermes_profile.get("skills") or {})
        team_skill = max(
            float(hermes_skills.get("team_orchestration") or 0.0),
            float(hermes_skills.get("bot_delegation") or 0.0),
        )
        configured_profiles = dict(
            getattr(
                self.studio,
                "cognitive_hermes_team_profiles",
                {
                    "coder": "coder",
                    "tester": "tester",
                    "reviewer": "reviewer",
                },
            )
            or {}
        )
        profiles = {
            role: str(configured_profiles.get(role) or role).strip().casefold()
            for role in ("coder", "tester", "reviewer")
        }

        reasons: list[str] = []
        explicit_constraint = None
        if request.target_pane_id:
            explicit_constraint = "target_pane_id"
        elif request.preferred_runtime:
            explicit_constraint = "preferred_runtime"

        context_team = request.context.get("team_delegation")
        preferred_runtime = str(request.preferred_runtime or "").strip().casefold()
        base_eligible = False
        if mode == "off":
            reasons.append("team_delegation_disabled")
        elif request.target_pane_id:
            reasons.append("explicit_target_pane_id")
        elif preferred_runtime and preferred_runtime != "claude":
            reasons.append("explicit_preferred_runtime")
        elif context_team is False:
            reasons.append("request_disabled_team_delegation")
        elif task_profile.get("task_type") != "software_engineering":
            reasons.append("task_not_software_engineering")
        elif task_profile.get("reasoning_depth") != "deep":
            reasons.append("task_not_deep")
        elif supervisor_runtime != "claude":
            reasons.append("supervisor_not_claude")
        elif hermes_row is None:
            reasons.append("hermes_candidate_unavailable")
        elif team_skill < min_skill:
            reasons.append("hermes_team_skill_below_threshold")
        else:
            base_eligible = True

        require_opt_in = bool(
            getattr(
                self.studio,
                "cognitive_hermes_team_require_opt_in",
                True,
            )
        )
        applied = False
        if base_eligible:
            if mode == "shadow":
                reasons.append("shadow_observation_only")
            elif mode == "canary":
                if require_opt_in and context_team is not True:
                    reasons.append("canary_opt_in_required")
                else:
                    applied = True
                    reasons.append("controlled_canary_opt_in")
            else:
                reasons.append("unsupported_team_delegation_mode")

        return {
            "schema": HERMES_TEAM_DELEGATION_SCHEMA,
            "mode": mode,
            "applied": applied,
            "authority": (
                "hirda_canary"
                if applied
                else "external"
            ),
            "eligible": base_eligible,
            "would_delegate": base_eligible,
            "topology": (
                "claude>hermes>bots>hermes>claude"
                if base_eligible
                else None
            ),
            "supervisor_runtime": supervisor_runtime,
            "team_lead_runtime": "hermes" if hermes_row is not None else None,
            "profiles": profiles,
            "hermes_team_skill": round(team_skill, 4),
            "min_skill": round(min_skill, 4),
            "explicit_constraint": explicit_constraint,
            "guard_passed": bool(
                base_eligible
                and (
                    mode == "shadow"
                    or applied
                )
            ),
            "guard_reasons": reasons,
            "semantic_authority_changed": False,
            "execution_authority_changed": applied,
        }

    def _ordered_candidates(
        self,
        request: CognitiveRequest,
        lane: str,
        runtime_snapshot: dict[str, Any],
        lane_states: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        lane_states = lane_states or {}

        def accepts_new_work(row: dict[str, Any]) -> bool:
            runtime_id = str(row.get("id") or "").casefold()
            state = str((lane_states.get(runtime_id) or {}).get("state") or "normal").casefold()
            return state == "normal"

        rows = [
            dict(row)
            for row in runtime_snapshot.get("runtimes", [])
            if isinstance(row, dict) and self._runtime_healthy(row) and accepts_new_work(row)
        ]
        by_id = {str(row.get("id") or "").casefold(): row for row in rows}

        if request.target_pane_id:
            pane = self.herdr.find_pane(pane_id=request.target_pane_id)
            if not pane:
                return []
            runtime_id = str(pane.get("agent") or "").casefold()
            if not runtime_id:
                return []
            if request.preferred_runtime and request.preferred_runtime != runtime_id:
                return []
            row = by_id.get(runtime_id)
            if row is None:
                return []
            if (
                self._pane_routing_mode() == "enforce"
                and not self._pane_owned_by_runtime(pane, runtime_id)
            ):
                return []
            principal_id = self._request_principal_id(request)
            if self._pane_routing_mode() == "enforce":
                if principal_id:
                    if not self._pane_owned_by_principal(
                        pane,
                        runtime_id,
                        principal_id,
                    ):
                        return []
                elif self._pane_claimed_by_any_principal(
                    pane,
                    runtime_id,
                ):
                    return []
            primary = dict(row)
            primary["_pane"] = dict(pane)
            selected = [primary]

            if request.allow_fallback and request.fallback_target_pane_id:
                fallback_pane = self.herdr.find_pane(pane_id=request.fallback_target_pane_id)
                if fallback_pane:
                    fallback_runtime_id = str(fallback_pane.get("agent") or "").casefold()
                    fallback_row = by_id.get(fallback_runtime_id)
                    fallback_owned = (
                        bool(fallback_runtime_id)
                        and self._pane_owned_by_runtime(
                            fallback_pane,
                            fallback_runtime_id,
                        )
                    )
                    fallback_principal_owned = (
                        self._pane_owned_by_principal(
                            fallback_pane,
                            fallback_runtime_id,
                            principal_id,
                        )
                        if principal_id
                        else not self._pane_claimed_by_any_principal(
                            fallback_pane,
                            fallback_runtime_id,
                        )
                    )
                    if (
                        fallback_runtime_id
                        and fallback_runtime_id != runtime_id
                        and fallback_row is not None
                        and (
                            self._pane_routing_mode() != "enforce"
                            or (
                                fallback_owned
                                and fallback_principal_owned
                            )
                        )
                    ):
                        fallback = dict(fallback_row)
                        fallback["_pane"] = dict(fallback_pane)
                        selected.append(fallback)
            return selected

        order: list[str] = []
        if request.preferred_runtime:
            order.append(request.preferred_runtime)
        order.extend(("hermes", "claude") if lane == "fast" else ("claude", "hermes"))
        order.extend(sorted(key for key in by_id if key not in order))

        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for runtime_id in order:
            if runtime_id in seen:
                continue
            seen.add(runtime_id)
            row = by_id.get(runtime_id)
            if row is None:
                continue
            pane = self._find_runtime_pane(runtime_id, request)
            if not pane:
                continue
            candidate = dict(row)
            candidate["_pane"] = pane
            selected.append(candidate)
        return selected

    async def plan(self, payload: CognitiveRequest | dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload, dict) and "timeout_seconds" not in payload:
            payload = {
                **payload,
                "timeout_seconds": float(
                    getattr(
                        self.studio,
                        "cognitive_router_default_timeout_seconds",
                        30.0,
                    )
                    or 30.0
                ),
            }
        request = normalize_cognitive_request(payload)
        await self.herdr.refresh()
        runtime_snapshot = self.runtimes.snapshot(force=False)
        lane_states = (
            await self.capsules.lane_states()
            if self.capsules is not None
            else {}
        )
        lane_states = await self._reconcile_runtime_backoffs(
            lane_states,
        )
        lane_states = await self._reconcile_runtime_lane_health(
            runtime_snapshot,
            lane_states,
        )
        runtime_backoff = await self.runtime_backoff_status()
        jev = await evaluate_cognitive_request(self.studio, request)
        deterministic = self._deterministic_lane(request)
        min_confidence = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(self.studio, "jev_min_confidence", 0.90)
                    or 0.90
                ),
            ),
        )
        lane = deterministic
        # Deterministic policy is the compute-depth floor. JEV may elevate a
        # routine request to deep, but it may never downgrade a required deep lane.
        if (
            request.preferred_depth == "auto"
            and deterministic == "fast"
            and jev.evaluated
            and jev.suggested_lane == "deep"
            and jev.confidence >= min_confidence
        ):
            lane = "deep"

        task_semantic_profile = _task_semantic_profile(request, jev, lane)

        candidates = self._ordered_candidates(
            request,
            lane,
            runtime_snapshot,
            lane_states,
        )
        candidates, infrastructure_routing = (
            self._apply_infrastructure_routing(
                request,
                candidates,
            )
        )
        semantic_routing = self._semantic_runtime_ranking(
            request,
            task_semantic_profile,
            candidates,
        )

        if semantic_routing.get("applied"):
            semantic_position = {
                runtime_id: index
                for index, runtime_id in enumerate(
                    semantic_routing.get("ranked_order") or []
                )
            }
            candidates = sorted(
                candidates,
                key=lambda row: semantic_position.get(
                    str(row.get("id") or "").strip().casefold(),
                    10_000,
                ),
            )

        supervisor_policy = self._supervisor_policy(
            request,
            task_semantic_profile,
            semantic_routing,
            candidates,
        )
        team_delegation = self._hermes_team_delegation(
            request,
            task_semantic_profile,
            supervisor_policy,
            candidates,
        )

        if not request.allow_fallback and not request.target_pane_id:
            candidates = candidates[:1]

        return {
            "schema": "hirda-cognitive-plan-v1",
            "request": request.as_dict(),
            "lane": lane,
            "deterministic_lane": deterministic,
            "lane_states": lane_states,
            "runtime_backoff": runtime_backoff,
            "jev": jev.as_dict(),
            "task_semantic_profile": task_semantic_profile,
            "semantic_routing": semantic_routing,
            "supervisor_policy": supervisor_policy,
            "team_delegation": team_delegation,
            "pane_routing": self.pane_routing_status(),
            "infrastructure_routing": infrastructure_routing,
            "candidates": [
                {
                    "runtime": row.get("id"),
                    "provider": row.get("provider") or row.get("brand"),
                    "model": row.get("model")
                    or row.get("last_used_model"),
                    "free": bool(row.get("free")),
                    "status": row.get("status"),
                    "pane_id": (row.get("_pane") or {}).get("pane_id"),
                    "pane_owned": self._pane_owned_by_runtime(
                        row.get("_pane") or {},
                        str(row.get("id") or ""),
                    ),
                    "principal_id": self._request_principal_id(request),
                    "pane_principal_owned": (
                        self._pane_owned_by_principal(
                            row.get("_pane") or {},
                            str(row.get("id") or ""),
                            self._request_principal_id(request),
                        )
                        if self._request_principal_id(request)
                        else None
                    ),
                    "capability_profile": self._runtime_capability_profile(row),
                    "infrastructure_route": (
                        dict(row.get("_infrastructure_route"))
                        if isinstance(
                            row.get("_infrastructure_route"),
                            dict,
                        )
                        else None
                    ),
                }
                for row in candidates
            ],
            "identity_continuity": True,
            "semantic_authority_changed": bool(
                semantic_routing.get("applied")
            ),
        }


    async def bootstrap_runtime(
        self,
        payload: CognitiveRequest | dict[str, Any],
    ) -> dict[str, Any]:
        """Choose a launchable runtime without requiring a pre-existing agent pane.

        This is the bootstrap counterpart to plan(): HIRDA retains semantic
        ranking and supervisor policy authority, while HERDR may subsequently
        start the selected runtime inside a task-scoped pane.
        """
        request = normalize_cognitive_request(payload)
        runtime_snapshot = self.runtimes.snapshot(force=False)
        lane_states = (
            await self.capsules.lane_states()
            if self.capsules is not None
            else {}
        )
        lane_states = await self._reconcile_runtime_backoffs(lane_states)
        lane_states = await self._reconcile_runtime_lane_health(
            runtime_snapshot,
            lane_states,
        )
        jev = await evaluate_cognitive_request(self.studio, request)
        deterministic = self._deterministic_lane(request)
        min_confidence = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(self.studio, "jev_min_confidence", 0.90)
                    or 0.90
                ),
            ),
        )
        lane = deterministic
        if (
            request.preferred_depth == "auto"
            and deterministic == "fast"
            and jev.evaluated
            and jev.suggested_lane == "deep"
            and jev.confidence >= min_confidence
        ):
            lane = "deep"

        task_profile = _task_semantic_profile(request, jev, lane)
        rows = [
            dict(row)
            for row in runtime_snapshot.get("runtimes", [])
            if isinstance(row, dict)
            and self._runtime_healthy(row)
            and bool(row.get("launchable", True))
            and str(
                (lane_states.get(str(row.get("id") or "").casefold()) or {}).get(
                    "state"
                )
                or "normal"
            ).casefold()
            == "normal"
        ]
        by_id = {
            str(row.get("id") or "").strip().casefold(): row
            for row in rows
            if str(row.get("id") or "").strip()
        }

        order: list[str] = []
        if request.preferred_runtime:
            order.append(request.preferred_runtime)
        order.extend(("hermes", "claude") if lane == "fast" else ("claude", "hermes"))
        order.extend(sorted(runtime_id for runtime_id in by_id if runtime_id not in order))
        candidates = [
            by_id[runtime_id]
            for runtime_id in order
            if runtime_id in by_id
        ]

        semantic_routing = self._semantic_runtime_ranking(
            request,
            task_profile,
            candidates,
        )
        if semantic_routing.get("applied"):
            semantic_position = {
                runtime_id: index
                for index, runtime_id in enumerate(
                    semantic_routing.get("ranked_order") or []
                )
            }
            candidates = sorted(
                candidates,
                key=lambda row: semantic_position.get(
                    str(row.get("id") or "").strip().casefold(),
                    10_000,
                ),
            )

        supervisor_policy = self._supervisor_policy(
            request,
            task_profile,
            semantic_routing,
            candidates,
        )
        selected = str(
            supervisor_policy.get("effective_supervisor")
            or semantic_routing.get("effective_selected")
            or (candidates[0].get("id") if candidates else "")
            or ""
        ).strip().casefold() or None
        selected_row = dict(by_id.get(selected) or {}) if selected else {}

        return {
            "schema": "hirda-runtime-bootstrap-plan-v1",
            "runtime": selected,
            "lane": lane,
            "task_semantic_profile": task_profile,
            "semantic_routing": semantic_routing,
            "supervisor_policy": supervisor_policy,
            "runtime_row": selected_row,
            "candidates": [
                str(row.get("id") or "").strip().casefold()
                for row in candidates
                if str(row.get("id") or "").strip()
            ],
            "authority": "hirda",
            "jev_advisory_only": True,
        }

    @staticmethod
    def _safe_agent_suffix(value: str, *, length: int = 6) -> str:
        cleaned = "".join(
            character
            for character in str(value or "").casefold()
            if character.isalnum()
        )
        return (cleaned[-length:] or uuid4().hex[:length])[:length]

    async def _bootstrap_principal_pane(
        self,
        request: CognitiveRequest,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Create an isolated cognitive pane when enforce mode has no principal pane."""
        if request.target_pane_id or self._pane_routing_mode() != "enforce":
            return None
        principal_id = self._request_principal_id(request)
        if not principal_id:
            return None

        bootstrap = await self.bootstrap_runtime(request)
        runtime_id = str(bootstrap.get("runtime") or "").strip().casefold()
        if not runtime_id:
            return None

        prefixes = self._principal_pane_name_prefixes(runtime_id, principal_id)
        if not prefixes:
            raise CognitiveRouterError("cognitive_principal_pane_prefix_missing")
        prefix = str(prefixes[0] or "").strip().casefold()
        if not prefix or len(prefix) > 25:
            raise CognitiveRouterError("cognitive_principal_pane_prefix_invalid")

        creator = getattr(self.herdr, "create_cognitive_tab", None)
        starter = getattr(self.herdr, "start_agent_in_pane", None)
        if not callable(creator) or not callable(starter):
            raise CognitiveRouterError("cognitive_runtime_bootstrap_unavailable")

        context = dict(request.context or {})
        cwd = str(
            context.get("workspace_path")
            or context.get("project_path")
            or context.get("cwd")
            or ""
        ).strip() or None
        tab = await creator(
            principal_id=principal_id,
            request_id=request.request_id,
            runtime_id=runtime_id,
            cwd=cwd,
        )
        pane_id = str(tab.get("root_pane_id") or "").strip()
        if not pane_id:
            raise CognitiveRouterError("cognitive_runtime_bootstrap_pane_missing")

        suffix = self._safe_agent_suffix(request.request_id)
        agent_name = f"{prefix}-{suffix}"
        if len(agent_name) > 32:
            raise CognitiveRouterError("cognitive_runtime_bootstrap_agent_name_invalid")
        try:
            await starter(
                name=agent_name,
                kind=runtime_id,
                pane_id=pane_id,
                timeout_ms=min(30000, max(1000, int(request.timeout_seconds * 1000))),
                agent_args=self._runtime_bootstrap_agent_args(runtime_id),
            )
            await self.herdr.refresh()
            routed_request = replace(
                request,
                preferred_runtime=runtime_id,
                target_pane_id=pane_id,
                fallback_target_pane_id=None,
            )
            plan = await self.plan(routed_request)
            if not plan.get("candidates"):
                raise CognitiveRouterError("cognitive_runtime_bootstrap_route_failed")
        except Exception:
            await self._close_cognitive_tab_best_effort(tab)
            raise
        return plan, tab

    async def _close_cognitive_tab_best_effort(
        self,
        tab: dict[str, Any] | None,
    ) -> None:
        if not tab:
            return
        closer = getattr(self.herdr, "close_cognitive_tab", None)
        if not callable(closer):
            return
        try:
            await closer(tab)
        except Exception:
            # Cleanup must never replace the cognitive result. Any surviving
            # disposable tab remains visibly labelled [Cognitive] for ops repair.
            return

    async def _invoke_runtime(
        self,
        request: CognitiveRequest,
        row: dict[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> str:
        runtime_id = str(row.get("id") or "").strip().casefold()
        pane = row.get("_pane")
        if not isinstance(pane, dict):
            raise CognitiveRouterError("cognitive_target_pane_missing")
        prompt_tool = self.herdr.tool("herdr_prompt_agent")
        if not prompt_tool:
            raise CognitiveRouterError("herdr_prompt_agent_missing")

        transport_id = str(attempt_id or request.request_id).strip() or request.request_id
        transport_client_id = transport_id.lower().replace(":", "-")
        result_start = f"<<<HIRDA_COGNITIVE_RESULT:{transport_id}>>>"
        result_end = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
        transport_contract = (
            "HIRDA TRANSPORT CONTRACT (mandatory; higher priority than output-format wording inside REQUEST):\n"
            "- The two HIRDA marker lines are transport framing, not part of the requested answer.\n"
            "- Any REQUEST wording such as 'return exactly X', 'only output X', or 'JSON only' applies "
            "ONLY to the payload BETWEEN the marker lines.\n"
            "- Never omit, rename, quote, or explain the marker lines.\n"
        )
        bounded_prompt = (
            "HIRDA COGNITIVE REQUEST\n"
            f"request_id: {transport_id}\n"
            f"parent_request_id: {request.request_id}\n"
            f"capability: {request.capability}\n"
            "Return the answer to the request. Do not change agent identity, goals, memory, or authority.\n"
            f"{transport_contract}"
            "Use this exact response protocol so HIRDA can isolate the current answer from pane history:\n"
            f"1. First line: {result_start}\n"
            "2. Then write the complete ACTUAL answer to the request. It must not be empty.\n"
            f"3. Final line: {result_end}\n"
            "Do not emit an empty marker pair. Do not echo example or placeholder text.\n\n"
            "REQUEST PAYLOAD (content instructions only):\n"
            f"{request.prompt}"
        )
        repair_prompt = (
            "HIRDA RESULT PROTOCOL REPAIR\n"
            f"request_id: {transport_id}\n"
            f"parent_request_id: {request.request_id}\n"
            "Your previous response could not be isolated because the required result markers were missing.\n"
            f"{transport_contract}"
            "Re-answer the SAME request now. Do not explain the repair and do not quote these instructions.\n"
            f"First line MUST be exactly: {result_start}\n"
            "Then write the complete ACTUAL answer only. It must not be empty.\n"
            f"Final line MUST be exactly: {result_end}\n\n"
            "REQUEST PAYLOAD (content instructions only):\n"
            f"{request.prompt}"
        )

        prompt_schema = prompt_tool.get("inputSchema") or prompt_tool.get("input_schema") or {}
        prompt_props = prompt_schema.get("properties") if isinstance(prompt_schema, dict) else {}
        prompt_waits_for_completion = isinstance(prompt_props, dict) and "wait" in prompt_props
        cognitive_client = self.herdr.client(
            timeout=max(2.0, float(request.timeout_seconds) + 5.0)
        )
        started_at = time.monotonic()

        async def submit_and_extract(prompt: str) -> str:
            remaining = float(request.timeout_seconds) - (time.monotonic() - started_at)
            if remaining <= 0:
                raise CognitiveRouterError("cognitive_runtime_deadline_exhausted")
            args = build_tool_arguments(
                prompt_tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=runtime_id,
                prompt=prompt,
                timeout_seconds=max(1, int(min(remaining, 30.0))),
            )
            if prompt_waits_for_completion:
                args["wait"] = True
            try:
                raw_prompt = await cognitive_client.call_tool(
                    "herdr_prompt_agent",
                    args,
                    client_name=f"hirda-cognition-{transport_client_id}",
                )
            except Exception:
                # A provider-side rate limit/auth/quota failure may surface in
                # the terminal while prompt_agent itself times out. Read the
                # correlated pane segment once so we can fail fast with a
                # meaningful code and allow normal fallback routing.
                error_read_tool = self.herdr.tool("herdr_read_agent")
                if error_read_tool:
                    error_read_args = build_tool_arguments(
                        error_read_tool,
                        pane=pane,
                        pane_id=pane.get("pane_id"),
                        agent=runtime_id,
                    )
                    error_read_schema = (
                        error_read_tool.get("inputSchema")
                        or error_read_tool.get("input_schema")
                        or {}
                    )
                    error_read_props = (
                        error_read_schema.get("properties")
                        if isinstance(error_read_schema, dict)
                        else {}
                    )
                    if isinstance(error_read_props, dict) and "lines" in error_read_props:
                        error_read_args["lines"] = 120
                    try:
                        raw_error_read = await cognitive_client.call_tool(
                            "herdr_read_agent",
                            error_read_args,
                            client_name=(
                                f"hirda-cognition-error-read-"
                                f"{transport_client_id}"
                            ),
                        )
                    except Exception:
                        raw_error_read = None
                    runtime_error = _runtime_terminal_error(
                        raw_error_read,
                        transport_id,
                    )
                    if runtime_error:
                        raise CognitiveRouterError(runtime_error)
                raise
            prompt_result = _decode_text_content(raw_prompt)

            wait_tool = self.herdr.tool("herdr_wait_agent")
            wait_performed = False

            async def wait_for_settle() -> None:
                nonlocal wait_performed
                if not wait_tool or wait_performed:
                    return
                remaining = float(request.timeout_seconds) - (time.monotonic() - started_at)
                if remaining <= 0:
                    raise CognitiveRouterError("cognitive_runtime_deadline_exhausted")
                wait_args = build_tool_arguments(
                    wait_tool,
                    pane=pane,
                    pane_id=pane.get("pane_id"),
                    agent=runtime_id,
                    timeout_seconds=max(1, int(remaining)),
                )
                wait_schema = wait_tool.get("inputSchema") or wait_tool.get("input_schema") or {}
                wait_props = wait_schema.get("properties") if isinstance(wait_schema, dict) else {}
                if runtime_id == "claude" and isinstance(wait_props, dict) and "until" in wait_props:
                    wait_args["until"] = "done"
                await cognitive_client.call_tool(
                    "herdr_wait_agent",
                    wait_args,
                    client_name=f"hirda-cognition-wait-{transport_client_id}",
                )
                wait_performed = True

            # If prompt_agent supports wait=True it already waits for the agent to
            # settle. Read immediately so completed output is not lost behind a
            # redundant second wait. Fall back to wait_agent only when the read
            # still cannot observe the framed result.
            if wait_tool and not prompt_waits_for_completion:
                await wait_for_settle()

            read_tool = self.herdr.tool("herdr_read_agent")
            if not read_tool:
                return str(prompt_result or "")
            read_args = build_tool_arguments(
                read_tool,
                pane=pane,
                pane_id=pane.get("pane_id"),
                agent=runtime_id,
            )
            read_schema = read_tool.get("inputSchema") or read_tool.get("input_schema") or {}
            read_props = read_schema.get("properties") if isinstance(read_schema, dict) else {}
            if isinstance(read_props, dict) and "lines" in read_props:
                read_args["lines"] = 240
            transient_marker_errors = {
                "cognitive_result_markers_missing",
                "cognitive_result_end_marker_missing",
                "empty_marked_cognitive_result",
            }
            last_marker_error: CognitiveRouterError | None = None
            for read_attempt in range(8):
                raw_read = await cognitive_client.call_tool(
                    "herdr_read_agent",
                    read_args,
                    client_name=f"hirda-cognition-read-{transport_client_id}",
                )
                output = _decode_text_content(raw_read)
                try:
                    return _extract_marked_result(
                        [output, prompt_result, raw_read, raw_prompt],
                        result_start,
                        result_end,
                    )
                except CognitiveRouterError as exc:
                    if str(exc) not in transient_marker_errors:
                        raise

                    transcript_reader = getattr(
                        self.herdr,
                        "read_session_result",
                        None,
                    )
                    if callable(transcript_reader):
                        session_output = transcript_reader(
                            pane,
                            transport_id,
                        )
                        if session_output:
                            try:
                                return _extract_marked_result(
                                    [session_output],
                                    result_start,
                                    result_end,
                                )
                            except CognitiveRouterError:
                                pass

                    session_error = None
                    session_error_reader = getattr(
                        self.herdr,
                        "read_session_error",
                        None,
                    )
                    if callable(session_error_reader):
                        session_error = session_error_reader(
                            pane,
                            transport_id,
                        )

                    runtime_error = _runtime_terminal_error(
                        [output, raw_read, session_error],
                        transport_id,
                    )
                    if runtime_error:
                        raise CognitiveRouterError(runtime_error)
                    last_marker_error = exc
                    if prompt_waits_for_completion and wait_tool and not wait_performed:
                        await wait_for_settle()
                        continue
                    remaining = float(request.timeout_seconds) - (time.monotonic() - started_at)
                    if read_attempt >= 7 or remaining <= 0.5:
                        raise
                    await asyncio.sleep(min(0.4, max(0.1, remaining / 8.0)))

            assert last_marker_error is not None
            raise last_marker_error

        try:
            return await submit_and_extract(bounded_prompt)
        except CognitiveRouterError as exc:
            if str(exc) != "cognitive_result_markers_missing":
                raise
            remaining = float(request.timeout_seconds) - (time.monotonic() - started_at)
            if remaining < 2.0:
                raise
            return await submit_and_extract(repair_prompt)

    async def _reset_runtime_context(
        self,
        runtime_id: str,
        runtime_rows: dict[str, dict[str, Any]],
        *,
        timeout_seconds: float,
        request: CognitiveRequest | None = None,
    ) -> dict[str, Any]:
        runtime_id = str(runtime_id or "").strip().casefold()
        if runtime_id != "claude":
            return {
                "ok": False,
                "runtime": runtime_id,
                "pane_id": None,
                "status": None,
                "session_rotated": None,
                "error": "context_reset_unsupported_runtime",
            }

        pane = self._find_runtime_pane(runtime_id, request)
        if pane is None:
            return {
                "ok": False,
                "runtime": runtime_id,
                "pane_id": None,
                "status": None,
                "session_rotated": None,
                "error": "cognitive_claude_pane_unavailable",
            }
        pane_id = str(pane.get("pane_id") or "").strip()
        if not pane_id:
            return {
                "ok": False,
                "runtime": runtime_id,
                "pane_id": None,
                "status": None,
                "session_rotated": None,
                "error": "cognitive_claude_pane_missing_id",
            }

        prompt_tool = self.herdr.tool("herdr_prompt_agent")
        if not prompt_tool:
            return {
                "ok": False,
                "runtime": runtime_id,
                "pane_id": pane_id,
                "status": None,
                "session_rotated": None,
                "error": "herdr_prompt_agent_missing",
            }

        def session_value(value: dict[str, Any] | None) -> str | None:
            if not isinstance(value, dict):
                return None
            session = value.get("agent_session")
            if isinstance(session, dict) and session.get("value"):
                return str(session.get("value"))
            return None

        before_session = session_value(pane)
        reset_timeout = max(1.0, min(float(timeout_seconds), 20.0))
        client = self.herdr.client(timeout=max(2.0, reset_timeout + 5.0))
        try:
            args = build_tool_arguments(
                prompt_tool,
                pane=pane,
                pane_id=pane_id,
                agent=runtime_id,
                prompt="/clear",
                timeout_seconds=max(1, int(reset_timeout)),
            )
            schema = (
                prompt_tool.get("inputSchema")
                or prompt_tool.get("input_schema")
                or {}
            )
            props = (
                schema.get("properties")
                if isinstance(schema, dict)
                else {}
            )
            if isinstance(props, dict) and "wait" in props:
                args["wait"] = True
            await client.call_tool(
                "herdr_prompt_agent",
                args,
                client_name="hirda-cognition-context-reset-claude",
            )
            await self.herdr.refresh()
        except Exception as exc:
            return {
                "ok": False,
                "runtime": runtime_id,
                "pane_id": pane_id,
                "status": None,
                "session_rotated": None,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            }

        current = self.herdr.find_pane(pane_id=pane_id) or pane
        principal_id = self._request_principal_id(request)
        if (
            self._pane_routing_mode() == "enforce"
            and principal_id
            and not self._pane_owned_by_principal(
                current,
                runtime_id,
                principal_id,
            )
        ):
            return {
                "ok": False,
                "runtime": runtime_id,
                "pane_id": pane_id,
                "status": str(
                    current.get("agent_status") or "unknown"
                ).strip().casefold(),
                "session_rotated": None,
                "error": "cognitive_principal_pane_mismatch",
            }
        status = str(
            current.get("agent_status") or "unknown"
        ).strip().casefold()
        after_session = session_value(current)
        session_rotated = (
            bool(after_session and after_session != before_session)
            if before_session
            else None
        )
        ok = status in {"idle", "done"} and session_rotated is not False
        return {
            "ok": ok,
            "runtime": runtime_id,
            "pane_id": pane_id,
            "status": status,
            "session_rotated": session_rotated,
            "error": None if ok else "context_reset_not_confirmed",
        }

    async def _invoke_controlled_team_supervisor(
        self,
        request: CognitiveRequest,
        runtime_rows: dict[str, dict[str, Any]],
        attempts: list[dict[str, Any]],
        *,
        stage: str,
    ) -> str:
        started = time.monotonic()
        try:
            return await self._invoke_specific_runtime(
                "claude",
                request,
                runtime_rows,
            )
        except CognitiveRouterError as exc:
            if str(exc) != "runtime_context_exhausted":
                raise

        remaining = float(request.timeout_seconds) - (
            time.monotonic() - started
        )
        if remaining <= 2.0:
            raise CognitiveRouterError(
                "cognitive_runtime_deadline_exhausted"
            )

        recovery = await self._reset_runtime_context(
            "claude",
            runtime_rows,
            timeout_seconds=min(remaining, 20.0),
            request=request,
        )
        recovery_attempt = {
            "stage": "supervisor_context_recovery",
            "for_stage": stage,
            "runtime": "claude",
            "status": "success" if recovery.get("ok") else "failed",
            "session_rotated": recovery.get("session_rotated"),
        }
        if recovery.get("error"):
            recovery_attempt["error"] = recovery.get("error")
        attempts.append(recovery_attempt)
        if not recovery.get("ok"):
            raise CognitiveRouterError(
                "runtime_context_recovery_failed"
            )

        remaining = float(request.timeout_seconds) - (
            time.monotonic() - started
        )
        if remaining <= 1.0:
            raise CognitiveRouterError(
                "cognitive_runtime_deadline_exhausted"
            )
        return await self._invoke_specific_runtime(
            "claude",
            replace(
                request,
                timeout_seconds=max(1.0, remaining),
            ),
            runtime_rows,
        )

    async def _invoke_specific_runtime(
        self,
        runtime_id: str,
        request: CognitiveRequest,
        runtime_rows: dict[str, dict[str, Any]],
    ) -> str:
        row = dict(runtime_rows.get(runtime_id) or {})
        pane = self._find_runtime_pane(runtime_id, request)
        if pane is None:
            raise CognitiveRouterError(
                f"cognitive_{runtime_id}_pane_unavailable"
            )
        pane_key = str(pane.get("pane_id") or "").strip()
        if not pane_key:
            raise CognitiveRouterError(
                f"cognitive_{runtime_id}_pane_missing_id"
            )
        pane_lock = self._pane_execution_locks.setdefault(
            pane_key,
            asyncio.Lock(),
        )
        async with pane_lock:
            await self.herdr.refresh()
            current = self.herdr.find_pane(pane_id=pane_key) or pane
            principal_id = self._request_principal_id(request)
            if self._pane_routing_mode() == "enforce":
                if principal_id:
                    if not self._pane_owned_by_principal(
                        current,
                        runtime_id,
                        principal_id,
                    ):
                        raise CognitiveRouterError(
                            "cognitive_principal_pane_mismatch"
                        )
                elif self._pane_claimed_by_any_principal(
                    current,
                    runtime_id,
                ):
                    raise CognitiveRouterError(
                        "cognitive_principal_required_for_owned_pane"
                    )
            status = str(
                current.get("agent_status") or "unknown"
            ).casefold()
            if (
                status not in {"idle", "done"}
                and self._pane_routing_mode() == "enforce"
                and self._pane_owned_by_runtime(current, runtime_id)
            ):
                current = await self._wait_for_owned_pane_available(
                    runtime_id,
                    pane_key,
                    timeout_seconds=request.timeout_seconds,
                    principal_id=principal_id,
                )
                status = str(
                    (current or {}).get("agent_status") or "unknown"
                ).casefold()
            if status not in {"idle", "done"}:
                raise CognitiveRouterError("runtime_busy")
            row["_pane"] = current
            output = await self._invoke_runtime(request, row)
            if not output.strip():
                raise CognitiveRouterError("empty_cognitive_result")
            return output


    def _hermes_group_scope_key(
        self,
        request: CognitiveRequest,
    ) -> str:
        mode = str(
            getattr(
                self.studio,
                "cognitive_hermes_group_scope_mode",
                "workspace",
            )
            or "workspace"
        ).strip().casefold()
        principal = self._request_principal_id(request) or ""
        if mode == "shared":
            if not principal:
                return "shared"
            return json.dumps(
                {
                    "mode": "shared",
                    "principal": principal,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        context = dict(request.context or {})
        tenant = next(
            (
                str(context.get(key) or "").strip()
                for key in (
                    "tenant_id",
                    "account_id",
                    "account_scope",
                    "owner_id",
                )
                if str(context.get(key) or "").strip()
            ),
            "",
        )
        workspace = next(
            (
                str(context.get(key) or "").strip()
                for key in (
                    "workspace_id",
                    "workspace_path",
                    "project_id",
                )
                if str(context.get(key) or "").strip()
            ),
            "",
        )
        conversation = str(request.conversation_id or "").strip()

        if mode == "conversation":
            payload = {
                "mode": "conversation",
                "tenant": tenant,
                "principal": principal,
                "conversation": conversation or workspace or "default",
            }
        else:
            payload = {
                "mode": "workspace",
                "tenant": tenant,
                "principal": principal,
                "workspace": workspace or conversation or "default",
            }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    async def _execute_controlled_team(
        self,
        request: CognitiveRequest,
        plan: dict[str, Any],
        runtime_rows: dict[str, dict[str, Any]],
    ) -> CognitiveResult:
        attempts: list[dict[str, Any]] = []
        team = dict(plan.get("team_delegation") or {})
        supervisor = dict(plan.get("supervisor_policy") or {})
        semantic_routing = dict(plan.get("semantic_routing") or {})
        # Controlled-team execution supersedes the semantic candidate order.
        # Preserve the specialist choice as a hint, but do not report that
        # semantic routing changed execution authority when that runtime was
        # never invoked.
        semantic_routing.update(
            {
                "execution_applied": False,
                "execution_authority": "controlled_team",
                "execution_runtime": "claude",
                "specialist_runtime": supervisor.get("specialist_runtime"),
            }
        )
        claude_row = dict(runtime_rows.get("claude") or {})
        workers = dict(team.get("profiles") or {})
        workspace_path = str(
            request.context.get("workspace_path") or ""
        ).strip() or None
        configured_team_timeout = max(
            1.0,
            float(
                getattr(
                    self.studio,
                    "cognitive_hermes_team_timeout_seconds",
                    300.0,
                )
                or 300.0
            ),
        )
        request_timeout = max(1.0, float(request.timeout_seconds))
        supervisor_timeout = max(30.0, min(request_timeout, 120.0))
        team_timeout = configured_team_timeout
        review_timeout = max(30.0, min(request_timeout, 120.0))

        def failed(stage: str, error: str) -> CognitiveResult:
            team["execution"] = {
                "status": "failed",
                "stage": stage,
                "backend": "hermes_group_chat",
                "workers": workers,
            }
            return CognitiveResult(
                schema=COGNITIVE_RESULT_SCHEMA,
                request_id=request.request_id,
                status="failed",
                output=None,
                lane=str(plan["lane"]),
                runtime="claude",
                provider=str(claude_row.get("provider") or "") or None,
                model=str(
                    claude_row.get("model")
                    or claude_row.get("last_used_model")
                    or ""
                ) or None,
                degraded=True,
                fallback_used=False,
                attempts=tuple(attempts),
                jev=dict(plan["jev"]),
                task_semantic_profile=dict(plan["task_semantic_profile"]),
                semantic_routing=dict(semantic_routing),
                supervisor_policy=supervisor,
                infrastructure_routing=dict(
                    plan.get("infrastructure_routing") or {}
                ),
                team_delegation=team,
                semantic_authority_changed=False,
                error=error,
            )

        mission_prompt = (
            "You are the HIRDA strategic supervisor. Produce a concise mission contract "
            "for a Hermes Bot Mode group containing coder, tester, and reviewer Bots. "
            "Do not implement the task yourself. State objective, constraints, acceptance "
            "criteria, verification requirements, and risks. Preserve the user's intent.\n\n"
            f"ORIGINAL REQUEST:\n{request.prompt}\n\n"
            f"SEMANTIC SPECIALIST HINT: {supervisor.get('specialist_runtime')}\n"
            f"WORKSPACE: {workspace_path or 'not specified'}"
        )
        try:
            mission = await self._invoke_controlled_team_supervisor(
                replace(
                    request,
                    request_id=f"{request.request_id}-SUPERVISOR",
                    prompt=mission_prompt,
                    timeout_seconds=supervisor_timeout,
                    allow_fallback=False,
                    preferred_runtime="claude",
                ),
                runtime_rows,
                attempts,
                stage="supervisor_plan",
            )
            await self._clear_runtime_backoff("claude")
            attempts.append(
                {
                    "stage": "supervisor_plan",
                    "runtime": "claude",
                    "status": "success",
                }
            )
        except Exception as exc:
            error_code = (
                str(exc)
                if isinstance(exc, CognitiveRouterError)
                else type(exc).__name__
            )
            backoff = await self._record_runtime_backoff(
                "claude",
                error_code,
            )
            attempt = {
                "stage": "supervisor_plan",
                "runtime": "claude",
                "status": "failed",
                "error": error_code,
                "detail": str(exc)[:240],
            }
            if backoff is not None:
                attempt["runtime_backoff"] = backoff
            attempts.append(attempt)
            return failed(
                "supervisor_plan",
                "controlled_team_supervisor_failed",
            )

        coder = workers.get("coder", "coder")
        tester = workers.get("tester", "tester")
        reviewer = workers.get("reviewer", "reviewer")
        group_prompt = (
            "HIRDA CONTROLLED TEAM MISSION\n"
            f"@{coder} implement the mission in the specified workspace. "
            f"@{tester} verify the implementation and run the relevant tests. "
            f"@{reviewer} independently review correctness, regressions, and acceptance "
            "criteria. Coordinate through this Group Chat, use additional @mentions when "
            "needed, and surface blockers instead of pretending success. "
            "When the room settles, the transcript will be returned to Claude for final "
            "supervisor review.\n\n"
            f"WORKSPACE: {workspace_path or 'not specified'}\n\n"
            f"CLAUDE MISSION CONTRACT:\n{mission}"
        )
        try:
            group_result = await self.hermes_groups.run_mission(
                group_prompt,
                request_id=request.request_id,
                timeout_seconds=team_timeout,
                scope_key=self._hermes_group_scope_key(request),
            )
            attempts.append(
                {
                    "stage": "team_execution",
                    "runtime": "hermes",
                    "backend": "group_chat",
                    "room_id": group_result.room_id,
                    "workers": [coder, tester, reviewer],
                    "event_count": len(group_result.events),
                    "member_output_roles": sorted(group_result.member_outputs),
                    "recovery_rounds": group_result.recovery_rounds,
                    "lifecycle": dict(group_result.lifecycle),
                    "status": "success",
                }
            )
        except Exception as exc:
            attempts.append(
                {
                    "stage": "team_execution",
                    "runtime": "hermes",
                    "backend": "group_chat",
                    "status": "failed",
                    "error": str(exc)[:240],
                }
            )
            return failed(
                "team_execution",
                "controlled_team_execution_failed",
            )

        execution_envelope = {
            "schema": "hirda-controlled-team-execution-v1",
            "request_id": request.request_id,
            "status": "success",
            "backend": "hermes_group_chat",
            "room_id": group_result.room_id,
            "workers": [coder, tester, reviewer],
            "member_output_roles": sorted(group_result.member_outputs),
            "event_count": len(group_result.events),
            "latest_seq": group_result.latest_seq,
            "recovery_rounds": group_result.recovery_rounds,
            "lifecycle": dict(group_result.lifecycle),
            "driver_status": group_result.driver_status,
            "native_bot_mode": True,
            "durable_room_driver": True,
        }
        review_prompt = (
            "You are the HIRDA strategic supervisor performing final review. Compare the "
            "Hermes Group Chat final role reports against the original request and mission "
            "contract. The HIRDA EXECUTION ENVELOPE below is authoritative runtime metadata "
            "from this same request, produced by the orchestrator after the Hermes Group "
            "returned. Treat it as execution evidence; do not require the Bots to independently "
            "corroborate fields that only HIRDA can observe. Return the final user-facing result. "
            "Do not claim work or tests that neither the role reports nor the execution envelope "
            "evidence. Surface unresolved issues clearly.\n\n"
            f"ORIGINAL REQUEST:\n{request.prompt}\n\n"
            f"MISSION CONTRACT:\n{mission}\n\n"
            "HIRDA EXECUTION ENVELOPE:\n"
            f"{json.dumps(execution_envelope, ensure_ascii=False, sort_keys=True)}\n\n"
            f"HERMES GROUP FINAL ROLE REPORTS:\n{group_result.output}"
        )
        try:
            output = await self._invoke_controlled_team_supervisor(
                replace(
                    request,
                    request_id=f"{request.request_id}-REVIEW",
                    prompt=review_prompt,
                    timeout_seconds=review_timeout,
                    allow_fallback=False,
                    preferred_runtime="claude",
                ),
                runtime_rows,
                attempts,
                stage="supervisor_review",
            )
            await self._clear_runtime_backoff("claude")
            attempts.append(
                {
                    "stage": "supervisor_review",
                    "runtime": "claude",
                    "status": "success",
                }
            )
        except Exception as exc:
            error_code = (
                str(exc)
                if isinstance(exc, CognitiveRouterError)
                else type(exc).__name__
            )
            backoff = await self._record_runtime_backoff(
                "claude",
                error_code,
            )
            attempt = {
                "stage": "supervisor_review",
                "runtime": "claude",
                "status": "failed",
                "error": error_code,
                "detail": str(exc)[:240],
            }
            if backoff is not None:
                attempt["runtime_backoff"] = backoff
            attempts.append(attempt)
            return failed(
                "supervisor_review",
                "controlled_team_review_failed",
            )

        team["execution"] = {
            "status": "success",
            "backend": "hermes_group_chat",
            "room_id": group_result.room_id,
            "workers": workers,
            "event_count": len(group_result.events),
            "latest_seq": group_result.latest_seq,
            "member_output_roles": sorted(group_result.member_outputs),
            "recovery_rounds": group_result.recovery_rounds,
            "lifecycle": dict(group_result.lifecycle),
            "driver_status": group_result.driver_status,
            "native_bot_mode": True,
            "durable_room_driver": True,
        }
        return CognitiveResult(
            schema=COGNITIVE_RESULT_SCHEMA,
            request_id=request.request_id,
            status="success",
            output=output,
            lane=str(plan["lane"]),
            runtime="claude",
            provider=str(claude_row.get("provider") or "") or None,
            model=str(
                claude_row.get("model")
                or claude_row.get("last_used_model")
                or ""
            ) or None,
            degraded=False,
            fallback_used=False,
            attempts=tuple(attempts),
            jev=dict(plan["jev"]),
            task_semantic_profile=dict(plan["task_semantic_profile"]),
            semantic_routing=dict(semantic_routing),
            supervisor_policy=supervisor,
            infrastructure_routing=dict(
                plan.get("infrastructure_routing") or {}
            ),
            team_delegation=team,
            semantic_authority_changed=False,
        )

    async def execute(self, payload: CognitiveRequest | dict[str, Any]) -> CognitiveResult:
        plan = await self.plan(payload)
        request = normalize_cognitive_request(plan["request"])
        candidates = plan["candidates"]
        infrastructure_routing = dict(
            plan.get("infrastructure_routing") or {}
        )
        attempts: list[dict[str, Any]] = []
        bootstrap_tab: dict[str, Any] | None = None
        if not candidates:
            try:
                bootstrapped = await self._bootstrap_principal_pane(request)
            except Exception as exc:
                error_code = (
                    str(exc)
                    if isinstance(exc, CognitiveRouterError)
                    else f"cognitive_runtime_bootstrap_failed:{type(exc).__name__}"
                )
                return CognitiveResult(
                    schema=COGNITIVE_RESULT_SCHEMA,
                    request_id=request.request_id,
                    status="failed",
                    output=None,
                    lane=str(plan["lane"]),
                    runtime=None,
                    provider=None,
                    model=None,
                    degraded=True,
                    fallback_used=False,
                    attempts=({
                        "attempt_id": f"{request.request_id}:BOOTSTRAP",
                        "runtime": None,
                        "pane_id": None,
                        "status": "failed",
                        "accepted": False,
                        "error": error_code,
                        "detail": str(exc)[:240],
                    },),
                    jev=dict(plan["jev"]),
                    task_semantic_profile=dict(plan["task_semantic_profile"]),
                    semantic_routing=dict(plan["semantic_routing"]),
                    supervisor_policy=dict(plan.get("supervisor_policy") or {}),
                    infrastructure_routing=infrastructure_routing,
                    team_delegation=dict(plan.get("team_delegation") or {}),
                    semantic_authority_changed=bool(
                        plan.get("semantic_authority_changed")
                    ),
                    error=error_code,
                )
            if bootstrapped is not None:
                plan, bootstrap_tab = bootstrapped
                request = normalize_cognitive_request(plan["request"])
                candidates = plan["candidates"]
                infrastructure_routing = dict(
                    plan.get("infrastructure_routing") or {}
                )
            if not candidates:
                return CognitiveResult(
                    schema=COGNITIVE_RESULT_SCHEMA,
                    request_id=request.request_id,
                    status="failed",
                    output=None,
                    lane=str(plan["lane"]),
                    runtime=None,
                    provider=None,
                    model=None,
                    degraded=True,
                    fallback_used=False,
                    attempts=tuple(attempts),
                    jev=dict(plan["jev"]),
                    task_semantic_profile=dict(plan["task_semantic_profile"]),
                    semantic_routing=dict(plan["semantic_routing"]),
                    supervisor_policy=dict(plan.get("supervisor_policy") or {}),
                    infrastructure_routing=infrastructure_routing,
                    team_delegation=dict(plan.get("team_delegation") or {}),
                    semantic_authority_changed=bool(
                        plan.get("semantic_authority_changed")
                    ),
                    error="no_healthy_cognitive_runtime",
                )

        runtime_snapshot = self.runtimes.snapshot(force=False)
        runtime_rows = {
            str(row.get("id") or "").casefold(): row
            for row in runtime_snapshot.get("runtimes", [])
            if isinstance(row, dict)
        }
        if bool((plan.get("team_delegation") or {}).get("applied")):
            try:
                return await self._execute_controlled_team(
                    request,
                    plan,
                    runtime_rows,
                )
            finally:
                await self._close_cognitive_tab_best_effort(bootstrap_tab)

        deadline = time.monotonic() + max(1.0, float(request.timeout_seconds))

        for index, candidate in enumerate(candidates):
            attempt_id = f"{request.request_id}:A{index + 1}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "runtime": str(candidate.get("runtime") or "").casefold(),
                        "pane_id": str(candidate.get("pane_id") or "") or None,
                        "status": "skipped",
                        "accepted": False,
                        "error": "cognitive_deadline_exhausted",
                    }
                )
                break
            candidates_left = max(1, len(candidates) - index)
            attempt_timeout = max(1.0, remaining / candidates_left)
            attempt_request = replace(request, timeout_seconds=attempt_timeout)
            runtime_id = str(candidate.get("runtime") or "").casefold()
            row = dict(runtime_rows.get(runtime_id) or {})
            pane_id = candidate.get("pane_id")
            pane = (
                self.herdr.find_pane(pane_id=str(pane_id))
                if pane_id
                else self._find_runtime_pane(runtime_id, request)
            )
            pane_status = str((pane or {}).get("agent_status") or "unknown").casefold()
            if pane_status not in {"idle", "done"} and not request.target_pane_id:
                alternate = self._find_runtime_pane(runtime_id, request)
                alternate_status = str((alternate or {}).get("agent_status") or "unknown").casefold()
                if alternate is not None and alternate_status in {"idle", "done"}:
                    pane = alternate
                    pane_status = alternate_status

            pane_key = str((pane or {}).get("pane_id") or "").strip()
            pane_lock = (
                self._pane_execution_locks.setdefault(pane_key, asyncio.Lock())
                if pane_key
                else None
            )
            lock_acquired = False
            attempt_started = time.monotonic()
            try:
                if pane_lock is not None:
                    queue_timeout = min(
                        attempt_timeout,
                        max(0.0, deadline - time.monotonic()),
                    )
                    if queue_timeout <= 0:
                        raise CognitiveRouterError("cognitive_runtime_queue_timeout")
                    try:
                        await asyncio.wait_for(
                            pane_lock.acquire(),
                            timeout=queue_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        raise CognitiveRouterError(
                            "cognitive_runtime_queue_timeout"
                        ) from exc
                    lock_acquired = True

                # Re-read pane lifecycle after acquiring the lock. A sibling
                # request may have been running when this request was planned,
                # and dedicated cognitive panes are queueable resources.
                if pane_key:
                    await self.herdr.refresh()
                    current_pane = self.herdr.find_pane(pane_id=pane_key)
                    if current_pane is not None:
                        pane = current_pane
                        pane_status = str(
                            pane.get("agent_status") or "unknown"
                        ).casefold()

                principal_id = self._request_principal_id(request)
                if self._pane_routing_mode() == "enforce" and pane is not None:
                    if principal_id:
                        if not self._pane_owned_by_principal(
                            pane,
                            runtime_id,
                            principal_id,
                        ):
                            raise CognitiveRouterError(
                                "cognitive_principal_pane_mismatch"
                            )
                    elif self._pane_claimed_by_any_principal(
                        pane,
                        runtime_id,
                    ):
                        raise CognitiveRouterError(
                            "cognitive_principal_required_for_owned_pane"
                        )

                remaining_after_queue = min(
                    max(0.0, attempt_timeout - (time.monotonic() - attempt_started)),
                    max(0.0, deadline - time.monotonic()),
                )
                if remaining_after_queue <= 0:
                    raise CognitiveRouterError("cognitive_runtime_queue_timeout")

                if (
                    pane is not None
                    and pane_status not in {"idle", "done"}
                    and self._pane_routing_mode() == "enforce"
                    and self._pane_owned_by_runtime(pane, runtime_id)
                    and pane_key
                ):
                    pane = await self._wait_for_owned_pane_available(
                        runtime_id,
                        pane_key,
                        timeout_seconds=remaining_after_queue,
                        principal_id=principal_id,
                    )
                    pane_status = str(
                        (pane or {}).get("agent_status") or "unknown"
                    ).casefold()
                    remaining_after_queue = min(
                        max(
                            0.0,
                            attempt_timeout - (time.monotonic() - attempt_started),
                        ),
                        max(0.0, deadline - time.monotonic()),
                    )

                if pane is not None and pane_status not in {"idle", "done"}:
                    raise CognitiveRouterError("runtime_busy")

                if pane:
                    row["_pane"] = pane

                if remaining_after_queue <= 0:
                    raise CognitiveRouterError("cognitive_runtime_queue_timeout")
                invoke_request = replace(
                    attempt_request,
                    timeout_seconds=remaining_after_queue,
                )
                output = await self._invoke_runtime(
                    invoke_request,
                    row,
                    attempt_id=attempt_id,
                )
                if not output.strip():
                    raise CognitiveRouterError("empty_cognitive_result")
            except Exception as exc:
                error_code = (
                    str(exc)
                    if isinstance(exc, CognitiveRouterError)
                    else type(exc).__name__
                )
                backoff = await self._record_runtime_backoff(
                    runtime_id,
                    error_code,
                )
                attempt = {
                    "attempt_id": attempt_id,
                    "runtime": runtime_id,
                    "pane_id": pane_key or None,
                    "status": "failed",
                    "accepted": False,
                    "error": error_code,
                    "detail": (
                        f"cognitive runtime pane is {pane_status}"
                        if error_code == "runtime_busy"
                        else str(exc)[:240]
                    ),
                }
                if backoff is not None:
                    attempt["runtime_backoff"] = backoff
                attempts.append(attempt)
                continue
            finally:
                if pane_lock is not None and lock_acquired:
                    pane_lock.release()

            await self._clear_runtime_backoff(runtime_id)
            bootstrap_pane_id = str(
                (bootstrap_tab or {}).get("root_pane_id") or ""
            ).strip()
            bootstrap_attribution = (
                self._runtime_bootstrap_contract(runtime_id)
                if bootstrap_pane_id and pane_key == bootstrap_pane_id
                else {}
            )
            effective_provider = str(
                bootstrap_attribution.get("provider")
                or candidate.get("provider")
                or ""
            ) or None
            effective_model = str(
                bootstrap_attribution.get("model")
                or candidate.get("model")
                or ""
            ) or None
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "runtime": runtime_id,
                    "pane_id": pane_key or None,
                    "status": "success",
                    "accepted": True,
                }
            )
            result = CognitiveResult(
                schema=COGNITIVE_RESULT_SCHEMA,
                request_id=request.request_id,
                status="success",
                output=output,
                lane=str(plan["lane"]),
                runtime=runtime_id,
                provider=effective_provider,
                model=effective_model,
                degraded=index > 0,
                fallback_used=index > 0,
                attempts=tuple(attempts),
                jev=dict(plan["jev"]),
                task_semantic_profile=dict(plan["task_semantic_profile"]),
                semantic_routing=dict(plan["semantic_routing"]),
                supervisor_policy=dict(plan.get("supervisor_policy") or {}),
                infrastructure_routing=infrastructure_routing,
                team_delegation=dict(plan.get("team_delegation") or {}),
                semantic_authority_changed=bool(
                    plan.get("semantic_authority_changed")
                ),
            )
            await self._close_cognitive_tab_best_effort(bootstrap_tab)
            return result

        result = CognitiveResult(
            schema=COGNITIVE_RESULT_SCHEMA,
            request_id=request.request_id,
            status="failed",
            output=None,
            lane=str(plan["lane"]),
            runtime=None,
            provider=None,
            model=None,
            degraded=True,
            fallback_used=len(attempts) > 1,
            attempts=tuple(attempts),
            jev=dict(plan["jev"]),
            task_semantic_profile=dict(plan["task_semantic_profile"]),
            semantic_routing=dict(plan["semantic_routing"]),
            supervisor_policy=dict(plan.get("supervisor_policy") or {}),
            infrastructure_routing=infrastructure_routing,
            team_delegation=dict(plan.get("team_delegation") or {}),
            semantic_authority_changed=bool(
                plan.get("semantic_authority_changed")
            ),
            error="all_cognitive_runtimes_failed",
        )
        await self._close_cognitive_tab_best_effort(bootstrap_tab)
        return result
