from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|credential)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY))=([^\s]+)"
)


@dataclass(frozen=True, slots=True)
class JevToolDecision:
    enabled: bool
    evaluated: bool
    mode: str
    action: str
    confidence: float
    blocked: bool
    code: str | None
    message: str
    model: str | None = None
    error: str | None = None


def _redact_text(value: str, *, limit: int) -> str:
    text = str(value)
    text = _INLINE_SECRET.sub(lambda m: (m.group(1) or "") + "[REDACTED]", text)
    return text[:limit]


def _sanitize(value: Any, *, limit: int, depth: int = 0) -> Any:
    if depth > 4:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:64]:
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _sanitize(item, limit=limit, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, limit=limit, depth=depth + 1) for item in list(value)[:64]]
    if isinstance(value, str):
        return _redact_text(value, limit=limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(repr(value), limit=limit)


def _disabled(mode: str = "shadow") -> JevToolDecision:
    return JevToolDecision(
        enabled=False,
        evaluated=False,
        mode=mode,
        action="allow",
        confidence=0.0,
        blocked=False,
        code=None,
        message="Jev decision plane is disabled.",
    )


async def evaluate_tool_call(
    studio: Any,
    session: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any] | None,
    category: str,
) -> JevToolDecision:
    """Evaluate a tool call with Jev as external teacher evidence.

    This adapter is deliberately non-authoritative. It may label a call
    allow/review/deny for comparison, but it never blocks execution.
    """

    mode = str(getattr(studio, "jev_mode", "shadow") or "shadow").strip().lower()
    if not bool(getattr(studio, "jev_enabled", False)):
        return _disabled(mode)
    if str(category) == "read" and not bool(getattr(studio, "jev_evaluate_read_tools", False)):
        return _disabled(mode)
    if mode not in {"shadow", "teacher", "enforce"}:
        mode = "shadow"

    key_env = str(getattr(studio, "jev_api_key_env", "TYPESAFE_API_KEY") or "TYPESAFE_API_KEY")
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        return JevToolDecision(
            enabled=True,
            evaluated=False,
            mode=mode,
            action="allow",
            confidence=0.0,
            blocked=False,
            code=None,
            message=f"Jev API key environment variable {key_env} is not set.",
            error="missing_api_key",
        )

    limit = max(512, int(getattr(studio, "jev_max_state_chars", 12000) or 12000))
    state = {
        "system": "HIRDA MCP tool decision plane",
        "workspace": {
            "key": session.get("workspace_key"),
            "project_path": session.get("project_path"),
        },
        "tool_call": {
            "name": str(tool_name),
            "permission_class": str(category),
            "arguments": _sanitize(arguments or {}, limit=limit),
        },
        "constraints": {
            "static_permission_already_granted": True,
            "jev_teacher_only": True,
            "hirda_reflex_remains_runtime_authority": True,
        },
    }
    questions = {
        "action": {
            "type": "choice",
            "instructions": (
                "Choose how HIRDA should handle this tool call after deterministic permission and "
                "workspace-scope checks already allowed it. Consider destructive intent, unexpected "
                "side effects, credential exposure, privilege escalation, scope escape attempts, and "
                "whether the operation plausibly matches an ordinary development workflow."
            ),
            "criteria": {
                "allow": "Routine operation with no material additional risk beyond its declared permission class.",
                "review": "Ambiguous or elevated-risk operation that should require explicit human review before execution.",
                "deny": "Likely unsafe, destructive, credential-exposing, privilege-escalating, or scope-bypassing operation.",
            },
        },
        "suspicious": {
            "type": "noul",
            "instructions": (
                "Is there evidence in this tool call of hidden destructive intent, credential exposure, "
                "privilege escalation, or an attempt to bypass the declared workspace/permission boundary?"
            ),
            "criteria": {
                "true": "There is concrete evidence of one or more listed hazards.",
                "false": "No concrete evidence of those hazards is present.",
            },
        },
    }

    base_url = str(getattr(studio, "jev_api_url", "https://api.typesafe.ai/v1/systemone") or "").strip()
    model = str(getattr(studio, "jev_model", "jev-latest") or "jev-latest").strip()
    timeout = max(0.1, float(getattr(studio, "jev_timeout_seconds", 2.0) or 2.0))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                base_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "state": state, "questions": questions},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return JevToolDecision(
            enabled=True,
            evaluated=False,
            mode=mode,
            action="allow",
            confidence=0.0,
            blocked=False,
            code=None,
            message="Jev evaluation failed; deterministic HIRDA policy remains authoritative.",
            model=model,
            error=type(exc).__name__,
        )

    answers = payload.get("answers") if isinstance(payload, dict) else None
    answers = answers if isinstance(answers, dict) else {}
    action_answer = answers.get("action") if isinstance(answers.get("action"), dict) else {}
    suspicious_answer = answers.get("suspicious") if isinstance(answers.get("suspicious"), dict) else {}

    action = str(action_answer.get("choice") or "review").strip().lower()
    if action not in {"allow", "review", "deny"}:
        action = "review"
    try:
        confidence = float(action_answer.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        suspicious = float(suspicious_answer.get("noul", 0.5))
    except (TypeError, ValueError):
        suspicious = 0.5

    returned_model = payload.get("model") if isinstance(payload, dict) else None
    return JevToolDecision(
        enabled=True,
        evaluated=True,
        mode=mode,
        action=action,
        confidence=max(0.0, min(1.0, confidence)),
        blocked=False,
        code=None,
        message=(
            f"Jev action={action} confidence={confidence:.3f}; suspicious={suspicious:.3f}; "
            f"mode={mode}."
        ),
        model=str(returned_model or model),
    )
