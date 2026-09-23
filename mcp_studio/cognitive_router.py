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


COGNITIVE_REQUEST_SCHEMA = "hirda-cognitive-request-v1"
COGNITIVE_RESULT_SCHEMA = "hirda-cognitive-result-v1"
COGNITIVE_JUDGMENT_SCHEMA = "hirda-jev-cognitive-judgment-v1"

_CAPABILITIES = {
    "social_dialogue",
    "reasoning_high",
    "reflection",
    "fast_utility",
    "coding",
    "research",
}
_DEPTHS = {"auto", "fast", "deep"}


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
        agent_id=str(payload.get("agent_id") or "").strip() or None,
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
    )


class CognitiveRouter:
    """HIRDA-owned cognitive routing across live agent runtimes.

    Semantic authority remains external to HIRDA. HIRDA selects a compute
    lane/runtime and can fail over between compatible runtimes. JEV is teacher evidence only.
    """

    def __init__(self, studio: Any, herdr: HerdrManager, runtimes: Any) -> None:
        self.studio = studio
        self.herdr = herdr
        self.runtimes = runtimes

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
    def _pane_reserved(pane: dict[str, Any]) -> bool:
        """Keep dedicated certification panes out of ordinary production routing."""
        prefixes = ("hirda-certification", "hirda-live-certification")
        name = str(pane.get("name") or "").strip().casefold()
        label = str(pane.get("label") or "").strip().casefold()
        return name.startswith(prefixes) or label.startswith(prefixes)

    def _find_runtime_pane(self, runtime_id: str) -> dict[str, Any] | None:
        snapshot = getattr(self.herdr, "snapshot", {})
        panes_block = snapshot.get("panes") if isinstance(snapshot, dict) else {}
        panes = panes_block.get("panes", []) if isinstance(panes_block, dict) else []
        candidates = [
            dict(pane)
            for pane in panes
            if isinstance(pane, dict)
            and str(pane.get("agent") or "").casefold() == runtime_id
            and not self._pane_reserved(pane)
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

    def _ordered_candidates(
        self,
        request: CognitiveRequest,
        lane: str,
        runtime_snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in runtime_snapshot.get("runtimes", [])
            if isinstance(row, dict) and self._runtime_healthy(row)
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
            primary = dict(row)
            primary["_pane"] = dict(pane)
            selected = [primary]

            if request.allow_fallback and request.fallback_target_pane_id:
                fallback_pane = self.herdr.find_pane(pane_id=request.fallback_target_pane_id)
                if fallback_pane:
                    fallback_runtime_id = str(fallback_pane.get("agent") or "").casefold()
                    fallback_row = by_id.get(fallback_runtime_id)
                    if fallback_runtime_id and fallback_runtime_id != runtime_id and fallback_row is not None:
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
            pane = self._find_runtime_pane(runtime_id)
            if not pane:
                continue
            candidate = dict(row)
            candidate["_pane"] = pane
            selected.append(candidate)
            if not request.allow_fallback:
                break
        return selected

    async def plan(self, payload: CognitiveRequest | dict[str, Any]) -> dict[str, Any]:
        if isinstance(payload, dict) and "timeout_seconds" not in payload:
            payload = {
                **payload,
                "timeout_seconds": float(
                    getattr(self.studio, "cognitive_router_default_timeout_seconds", 30.0) or 30.0
                ),
            }
        request = normalize_cognitive_request(payload)
        await self.herdr.refresh()
        runtime_snapshot = self.runtimes.snapshot(force=True)
        jev = await evaluate_cognitive_request(self.studio, request)
        deterministic = self._deterministic_lane(request)
        min_confidence = max(0.0, min(1.0, float(getattr(self.studio, "jev_min_confidence", 0.90) or 0.90)))
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
        candidates = self._ordered_candidates(request, lane, runtime_snapshot)
        return {
            "schema": "hirda-cognitive-plan-v1",
            "request": request.as_dict(),
            "lane": lane,
            "deterministic_lane": deterministic,
            "jev": jev.as_dict(),
            "candidates": [
                {
                    "runtime": row.get("id"),
                    "provider": row.get("provider") or row.get("brand"),
                    "model": row.get("model") or row.get("last_used_model"),
                    "free": bool(row.get("free")),
                    "status": row.get("status"),
                    "pane_id": (row.get("_pane") or {}).get("pane_id"),
                }
                for row in candidates
            ],
            "identity_continuity": True,
            "semantic_authority_changed": False,
        }

    async def _invoke_runtime(
        self,
        request: CognitiveRequest,
        row: dict[str, Any],
    ) -> str:
        runtime_id = str(row.get("id") or "").strip().casefold()
        pane = row.get("_pane")
        if not isinstance(pane, dict):
            raise CognitiveRouterError("cognitive_target_pane_missing")
        prompt_tool = self.herdr.tool("herdr_prompt_agent")
        if not prompt_tool:
            raise CognitiveRouterError("herdr_prompt_agent_missing")

        result_start = f"<<<HIRDA_COGNITIVE_RESULT:{request.request_id}>>>"
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
            f"request_id: {request.request_id}\n"
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
            f"request_id: {request.request_id}\n"
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
            raw_prompt = await cognitive_client.call_tool(
                "herdr_prompt_agent",
                args,
                client_name=f"hirda-cognition-{request.request_id.lower()}",
            )
            prompt_result = _decode_text_content(raw_prompt)

            wait_tool = self.herdr.tool("herdr_wait_agent")
            if wait_tool:
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
                    client_name=f"hirda-cognition-wait-{request.request_id.lower()}",
                )

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
                    client_name=f"hirda-cognition-read-{request.request_id.lower()}",
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
                    runtime_error = _runtime_terminal_error(
                        [output, raw_read],
                        request.request_id,
                    )
                    if runtime_error:
                        raise CognitiveRouterError(runtime_error)
                    last_marker_error = exc
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

    async def execute(self, payload: CognitiveRequest | dict[str, Any]) -> CognitiveResult:
        plan = await self.plan(payload)
        request = normalize_cognitive_request(plan["request"])
        candidates = plan["candidates"]
        attempts: list[dict[str, Any]] = []
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
                error="no_healthy_cognitive_runtime",
            )

        runtime_snapshot = self.runtimes.snapshot(force=False)
        runtime_rows = {
            str(row.get("id") or "").casefold(): row
            for row in runtime_snapshot.get("runtimes", [])
            if isinstance(row, dict)
        }
        deadline = time.monotonic() + max(1.0, float(request.timeout_seconds))

        for index, candidate in enumerate(candidates):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                attempts.append(
                    {
                        "runtime": str(candidate.get("runtime") or "").casefold(),
                        "status": "skipped",
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
            pane = self.herdr.find_pane(pane_id=str(pane_id)) if pane_id else self.herdr.find_pane(agent=runtime_id)
            pane_status = str((pane or {}).get("agent_status") or "unknown").casefold()
            if pane_status not in {"idle", "done"} and not request.target_pane_id:
                alternate = self._find_runtime_pane(runtime_id)
                alternate_status = str((alternate or {}).get("agent_status") or "unknown").casefold()
                if alternate is not None and alternate_status in {"idle", "done"}:
                    pane = alternate
                    pane_status = alternate_status
            if pane is not None and pane_status not in {"idle", "done"}:
                attempts.append(
                    {
                        "runtime": runtime_id,
                        "status": "failed",
                        "error": "runtime_busy",
                        "detail": f"cognitive runtime pane is {pane_status}",
                    }
                )
                continue
            if pane:
                row["_pane"] = pane
            try:
                output = await self._invoke_runtime(attempt_request, row)
                if not output.strip():
                    raise CognitiveRouterError("empty_cognitive_result")
            except Exception as exc:
                error_code = (
                    str(exc)
                    if isinstance(exc, CognitiveRouterError)
                    else type(exc).__name__
                )
                attempts.append(
                    {
                        "runtime": runtime_id,
                        "status": "failed",
                        "error": error_code,
                        "detail": str(exc)[:240],
                    }
                )
                continue

            attempts.append({"runtime": runtime_id, "status": "success"})
            return CognitiveResult(
                schema=COGNITIVE_RESULT_SCHEMA,
                request_id=request.request_id,
                status="success",
                output=output,
                lane=str(plan["lane"]),
                runtime=runtime_id,
                provider=str(candidate.get("provider") or "") or None,
                model=str(candidate.get("model") or "") or None,
                degraded=index > 0,
                fallback_used=index > 0,
                attempts=tuple(attempts),
                jev=dict(plan["jev"]),
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
            fallback_used=len(attempts) > 1,
            attempts=tuple(attempts),
            jev=dict(plan["jev"]),
            error="all_cognitive_runtimes_failed",
        )
