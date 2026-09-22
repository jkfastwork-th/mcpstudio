from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx

from .jev_decision import _sanitize


ACTION_ENVELOPE_SCHEMA = "hirda-action-envelope-v1"
ACTION_JUDGMENT_SCHEMA = "hirda-jev-action-judgment-v1"
MAX_ACTIONS = 32
MAX_GOAL_CHARS = 2000
MAX_TEXT_CHARS = 500

PermissionClass = Literal["read", "write", "execute", "destructive", "unknown"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_PERMISSION_CLASSES = {"read", "write", "execute", "destructive", "unknown"}


class ActionEnvelopeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionRisk:
    consequential: bool = False
    reversible: bool = True
    external_side_effect: bool = False
    destructive: bool = False
    financial: bool = False
    requires_human_approval: bool = False

    def as_dict(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    action_id: str
    operation: str
    title: str
    description: str
    permission_class: PermissionClass
    arguments: dict[str, Any]
    constraints: dict[str, Any]
    risk: ActionRisk

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "operation": self.operation,
            "title": self.title,
            "description": self.description,
            "permission_class": self.permission_class,
            "arguments": self.arguments,
            "constraints": self.constraints,
            "risk": self.risk.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ActionEnvelope:
    schema: str
    request_id: str
    provider: str
    domain: str
    goal: str
    state: dict[str, Any]
    actions: tuple[ActionCandidate, ...]
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "provider": self.provider,
            "domain": self.domain,
            "goal": self.goal,
            "state": self.state,
            "actions": [action.as_dict() for action in self.actions],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class JevActionJudgment:
    schema: str
    enabled: bool
    evaluated: bool
    mode: str
    request_id: str
    provider: str
    domain: str
    selected_action_id: str | None
    confidence: float
    probabilities: dict[str, float]
    needs_human_review: float | None
    needs_more_information: float | None
    consequence_risk: float | None
    uncertainty: float | None
    model: str | None
    error: str | None
    advisory_only: bool = True
    runtime_authorization_required: bool = True
    may_execute: bool = False
    external_action_authority: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text(value: Any, *, field: str, maximum: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ActionEnvelopeError(f"{field} is required")
    if len(text) > maximum:
        raise ActionEnvelopeError(f"{field} exceeds {maximum} characters")
    return text


def _clean_id(value: Any, *, field: str) -> str:
    text = _clean_text(value, field=field, maximum=160)
    if not _ID.fullmatch(text):
        raise ActionEnvelopeError(f"{field} must match {_ID.pattern}")
    return text


def _optional_text(value: Any, *, field: str, maximum: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > maximum:
        raise ActionEnvelopeError(f"{field} exceeds {maximum} characters")
    return text


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ActionEnvelopeError(f"{field} must be an object")
    return dict(value)


def _bool_field(raw: dict[str, Any], name: str, default: bool) -> bool:
    if name not in raw:
        return default
    value = raw[name]
    if not isinstance(value, bool):
        raise ActionEnvelopeError(f"risk.{name} must be a boolean")
    return value


def _risk(value: Any) -> ActionRisk:
    raw = _mapping(value, field="risk")
    allowed = {
        "consequential",
        "reversible",
        "external_side_effect",
        "destructive",
        "financial",
        "requires_human_approval",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ActionEnvelopeError(f"risk contains unsupported fields: {', '.join(unknown)}")
    return ActionRisk(
        consequential=_bool_field(raw, "consequential", False),
        reversible=_bool_field(raw, "reversible", True),
        external_side_effect=_bool_field(raw, "external_side_effect", False),
        destructive=_bool_field(raw, "destructive", False),
        financial=_bool_field(raw, "financial", False),
        requires_human_approval=_bool_field(raw, "requires_human_approval", False),
    )


def normalize_action_envelope(payload: ActionEnvelope | dict[str, Any]) -> ActionEnvelope:
    if isinstance(payload, ActionEnvelope):
        return payload
    if not isinstance(payload, dict):
        raise ActionEnvelopeError("action envelope must be an object")

    allowed_top = {"schema", "request_id", "provider", "domain", "goal", "state", "actions", "metadata"}
    unknown_top = sorted(set(payload) - allowed_top)
    if unknown_top:
        raise ActionEnvelopeError(
            f"action envelope contains unsupported fields: {', '.join(unknown_top)}"
        )

    schema = str(payload.get("schema") or ACTION_ENVELOPE_SCHEMA).strip()
    if schema != ACTION_ENVELOPE_SCHEMA:
        raise ActionEnvelopeError(f"unsupported action envelope schema: {schema}")

    request_id = _clean_id(payload.get("request_id"), field="request_id")
    provider = _clean_id(payload.get("provider"), field="provider")
    domain = _clean_id(payload.get("domain"), field="domain")
    goal = _clean_text(payload.get("goal"), field="goal", maximum=MAX_GOAL_CHARS)
    state = _mapping(payload.get("state"), field="state")
    metadata = _mapping(payload.get("metadata"), field="metadata")

    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ActionEnvelopeError("actions must be a non-empty array")
    if len(raw_actions) > MAX_ACTIONS:
        raise ActionEnvelopeError(f"actions exceeds maximum of {MAX_ACTIONS}")

    actions: list[ActionCandidate] = []
    seen: set[str] = set()
    for index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, dict):
            raise ActionEnvelopeError(f"actions[{index}] must be an object")
        allowed_action = {
            "action_id", "operation", "title", "description", "permission_class",
            "arguments", "constraints", "risk",
        }
        unknown_action = sorted(set(raw_action) - allowed_action)
        if unknown_action:
            raise ActionEnvelopeError(
                f"actions[{index}] contains unsupported fields: {', '.join(unknown_action)}"
            )
        action_id = _clean_id(raw_action.get("action_id"), field=f"actions[{index}].action_id")
        if action_id in seen:
            raise ActionEnvelopeError(f"duplicate action_id: {action_id}")
        seen.add(action_id)

        permission_class = str(raw_action.get("permission_class") or "unknown").strip().lower()
        if permission_class not in _PERMISSION_CLASSES:
            raise ActionEnvelopeError(
                f"actions[{index}].permission_class must be one of {sorted(_PERMISSION_CLASSES)}"
            )

        actions.append(
            ActionCandidate(
                action_id=action_id,
                operation=_clean_id(
                    raw_action.get("operation"),
                    field=f"actions[{index}].operation",
                ),
                title=_clean_text(
                    raw_action.get("title"),
                    field=f"actions[{index}].title",
                ),
                description=_optional_text(
                    raw_action.get("description"),
                    field=f"actions[{index}].description",
                ),
                permission_class=permission_class,  # type: ignore[arg-type]
                arguments=_mapping(
                    raw_action.get("arguments"),
                    field=f"actions[{index}].arguments",
                ),
                constraints=_mapping(
                    raw_action.get("constraints"),
                    field=f"actions[{index}].constraints",
                ),
                risk=_risk(raw_action.get("risk")),
            )
        )

    return ActionEnvelope(
        schema=ACTION_ENVELOPE_SCHEMA,
        request_id=request_id,
        provider=provider,
        domain=domain,
        goal=goal,
        state=state,
        actions=tuple(actions),
        metadata=metadata,
    )


def _model_state(envelope: ActionEnvelope, *, limit: int) -> dict[str, Any]:
    actions = []
    for index, action in enumerate(envelope.actions):
        actions.append(
            {
                "token": f"a{index}",
                "action_id": action.action_id,
                "operation": action.operation,
                "title": _sanitize(action.title, limit=limit),
                "description": _sanitize(action.description, limit=limit),
                "permission_class": action.permission_class,
                "arguments": _sanitize(action.arguments, limit=limit),
                "constraints": _sanitize(action.constraints, limit=limit),
                "risk": action.risk.as_dict(),
            }
        )
    return {
        "system": "HIRDA generic JEV action adapter",
        "request": {
            "request_id": envelope.request_id,
            "provider": envelope.provider,
            "domain": envelope.domain,
            "goal": _sanitize(envelope.goal, limit=limit),
        },
        "state": _sanitize(envelope.state, limit=limit),
        "actions": actions,
        "constraints": {
            "choose_only_from_candidate_tokens": True,
            "jev_is_advisory_only": True,
            "jev_may_not_execute": True,
            "jev_may_not_create_new_actions": True,
            "runtime_policy_authorizes_execution": True,
            "provider_executor_owns_side_effects": True,
        },
    }


def _questions(envelope: ActionEnvelope) -> dict[str, Any]:
    criteria: dict[str, str] = {}
    for index, action in enumerate(envelope.actions):
        flags: list[str] = []
        if action.risk.consequential:
            flags.append("consequential")
        if not action.risk.reversible:
            flags.append("irreversible")
        if action.risk.external_side_effect:
            flags.append("external_side_effect")
        if action.risk.destructive:
            flags.append("destructive")
        if action.risk.financial:
            flags.append("financial")
        if action.risk.requires_human_approval:
            flags.append("requires_human_approval")
        risk_summary = ", ".join(flags) if flags else "no declared elevated risk flags"
        safe_title = str(_sanitize(action.title, limit=MAX_TEXT_CHARS))
        safe_description = str(_sanitize(action.description, limit=MAX_TEXT_CHARS))
        criteria[f"a{index}"] = (
            f"{safe_title}. Operation={action.operation}. "
            f"Permission={action.permission_class}. Risk={risk_summary}. "
            f"{safe_description}".strip()
        )
    return {
        "selected_action": {
            "type": "choice",
            "instructions": (
                "Select the single candidate action that best fits the supplied goal and current state. "
                "Choose only from the provided tokens. Do not invent an action and do not treat this as permission to execute."
            ),
            "criteria": criteria,
        },
        "needs_human_review": {
            "type": "noul",
            "instructions": (
                "How strongly should a human review be required before any selected candidate is executed, "
                "considering declared consequence, financial, destructive, reversibility, and external-side-effect risk?"
            ),
        },
        "needs_more_information": {
            "type": "noul",
            "instructions": (
                "How strongly is the supplied state insufficient to choose responsibly among the listed candidates?"
            ),
        },
        "consequence_risk": {
            "type": "noul",
            "instructions": (
                "How consequential is executing the best-fitting candidate, using only declared state/action risk metadata?"
            ),
        },
        "uncertainty": {
            "type": "noul",
            "instructions": (
                "How uncertain is the best action choice given the supplied state and candidate set?"
            ),
        },
    }


def _probability(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return max(0.0, min(1.0, number))


def _answer_map(payload: dict[str, Any]) -> dict[str, Any]:
    answers = payload.get("answers")
    return answers if isinstance(answers, dict) else {}


def _fallback(
    envelope: ActionEnvelope,
    *,
    enabled: bool,
    mode: str,
    model: str | None,
    error: str | None,
) -> JevActionJudgment:
    return JevActionJudgment(
        schema=ACTION_JUDGMENT_SCHEMA,
        enabled=enabled,
        evaluated=False,
        mode=mode,
        request_id=envelope.request_id,
        provider=envelope.provider,
        domain=envelope.domain,
        selected_action_id=None,
        confidence=0.0,
        probabilities={},
        needs_human_review=None,
        needs_more_information=None,
        consequence_risk=None,
        uncertainty=None,
        model=model,
        error=error,
    )


async def evaluate_action_envelope(
    studio: Any,
    payload: ActionEnvelope | dict[str, Any],
) -> JevActionJudgment:
    """Ask JEV to judge a bounded action space without executing anything."""

    envelope = normalize_action_envelope(payload)
    enabled = bool(getattr(studio, "jev_enabled", False))
    mode = str(getattr(studio, "jev_mode", "shadow") or "shadow").strip().lower()
    if mode not in {"shadow", "teacher", "enforce"}:
        mode = "shadow"
    model = str(getattr(studio, "jev_model", "jev-latest") or "jev-latest").strip()
    if not enabled:
        return _fallback(envelope, enabled=False, mode=mode, model=None, error=None)

    key_env = str(getattr(studio, "jev_api_key_env", "TYPESAFE_API_KEY") or "TYPESAFE_API_KEY")
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        return _fallback(
            envelope,
            enabled=True,
            mode=mode,
            model=model,
            error="missing_api_key",
        )

    limit = max(512, int(getattr(studio, "jev_max_state_chars", 12000) or 12000))
    base_url = str(
        getattr(studio, "jev_api_url", "https://api.typesafe.ai/v1/systemone")
        or ""
    ).strip()
    timeout = max(0.1, float(getattr(studio, "jev_timeout_seconds", 2.0) or 2.0))
    token_to_action = {
        f"a{index}": action.action_id
        for index, action in enumerate(envelope.actions)
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                base_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "state": _model_state(envelope, limit=limit),
                    "questions": _questions(envelope),
                },
            )
            response.raise_for_status()
            body = response.json()
    except Exception:
        return _fallback(
            envelope,
            enabled=True,
            mode=mode,
            model=model,
            error="jev_request_failed",
        )

    if not isinstance(body, dict):
        return _fallback(
            envelope,
            enabled=True,
            mode=mode,
            model=model,
            error="jev_invalid_response",
        )

    answers = _answer_map(body)
    selected = answers.get("selected_action")
    if not isinstance(selected, dict):
        return _fallback(
            envelope,
            enabled=True,
            mode=mode,
            model=str(body.get("model") or model),
            error="jev_invalid_response",
        )
    token = str(selected.get("choice") or "").strip()
    selected_action_id = token_to_action.get(token)
    if selected_action_id is None:
        return _fallback(
            envelope,
            enabled=True,
            mode=mode,
            model=str(body.get("model") or model),
            error="jev_invalid_action",
        )

    probabilities: dict[str, float] = {}
    raw_probabilities = selected.get("probabilities")
    if isinstance(raw_probabilities, dict):
        for candidate_token, value in raw_probabilities.items():
            action_id = token_to_action.get(str(candidate_token))
            if action_id is not None:
                probabilities[action_id] = _probability(value)

    def noul(name: str) -> float | None:
        answer = answers.get(name)
        if not isinstance(answer, dict) or "noul" not in answer:
            return None
        return _probability(answer.get("noul"))

    return JevActionJudgment(
        schema=ACTION_JUDGMENT_SCHEMA,
        enabled=True,
        evaluated=True,
        mode=mode,
        request_id=envelope.request_id,
        provider=envelope.provider,
        domain=envelope.domain,
        selected_action_id=selected_action_id,
        confidence=_probability(selected.get("confidence")),
        probabilities=probabilities,
        needs_human_review=noul("needs_human_review"),
        needs_more_information=noul("needs_more_information"),
        consequence_risk=noul("consequence_risk"),
        uncertainty=noul("uncertainty"),
        model=str(body.get("model") or model),
        error=None,
    )


def sample_action_envelope() -> dict[str, Any]:
    return {
        "schema": ACTION_ENVELOPE_SCHEMA,
        "request_id": "demo:browser:search-001",
        "provider": "demo-browser",
        "domain": "browser",
        "goal": "Choose the next safe step to continue a search workflow.",
        "state": {
            "page": "search",
            "query_present": True,
            "results_visible": False,
        },
        "actions": [
            {
                "action_id": "browser:wait",
                "operation": "wait",
                "title": "Wait for current page state",
                "description": "Do not cause an external side effect.",
                "permission_class": "read",
                "arguments": {},
                "constraints": {"milliseconds": 500},
                "risk": {
                    "consequential": False,
                    "reversible": True,
                    "external_side_effect": False,
                },
            },
            {
                "action_id": "browser:submit-search",
                "operation": "submit",
                "title": "Submit the existing search query",
                "description": "Triggers navigation but no financial or destructive action.",
                "permission_class": "execute",
                "arguments": {"target": "search-form"},
                "constraints": {"query_already_present": True},
                "risk": {
                    "consequential": False,
                    "reversible": True,
                    "external_side_effect": True,
                },
            },
        ],
        "metadata": {
            "demo": True,
            "executor_attached": False,
        },
    }
