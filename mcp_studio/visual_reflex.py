from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from .action_adapter import (
    ACTION_ENVELOPE_SCHEMA,
    ActionEnvelope,
    JevActionJudgment,
    evaluate_action_envelope,
    normalize_action_envelope,
)


VISUAL_REFLEX_REQUEST_SCHEMA = "hirda-visual-reflex-request-v1"
VISUAL_REFLEX_RESULT_SCHEMA = "hirda-visual-reflex-result-v1"
MAX_TARGETS = 32

TargetSource = Literal["dom", "uia", "cache", "vision"]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_SOURCE_PRIORITY: dict[str, int] = {
    "dom": 0,
    "uia": 1,
    "cache": 2,
    "vision": 3,
}
_GEOMETRY_KEYS = {
    "x",
    "y",
    "width",
    "height",
    "bounds",
    "rect",
    "rectangle",
    "coordinate",
    "coordinates",
    "screen_x",
    "screen_y",
    "left",
    "top",
    "right",
    "bottom",
}


class VisualReflexError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VisualBounds:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


@dataclass(frozen=True, slots=True)
class VisualTarget:
    target_id: str
    label: str
    role: str
    source: TargetSource
    bounds: VisualBounds
    confidence: float


@dataclass(frozen=True, slots=True)
class VisualReflexRequest:
    request_id: str
    goal: str
    machine_id: str
    viewport_width: float
    viewport_height: float
    targets: tuple[VisualTarget, ...]
    state: dict[str, Any]


def _clean_id(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _ID.fullmatch(text):
        raise VisualReflexError(f"{field} is invalid")
    return text


def _clean_text(value: Any, *, field: str, maximum: int = 500) -> str:
    text = str(value or "").strip()
    if not text:
        raise VisualReflexError(f"{field} is required")
    if len(text) > maximum:
        raise VisualReflexError(f"{field} exceeds maximum length {maximum}")
    return text


def _number(value: Any, *, field: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise VisualReflexError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VisualReflexError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise VisualReflexError(f"{field} must be finite")
    if positive and number <= 0:
        raise VisualReflexError(f"{field} must be greater than zero")
    return number




def _strip_geometry(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_geometry(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _GEOMETRY_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_strip_geometry(item) for item in value]
    return value

def _position_hint(bounds: VisualBounds, viewport_width: float, viewport_height: float) -> str:
    cx, cy = bounds.center
    horizontal = (
        "left"
        if cx < viewport_width / 3
        else "right"
        if cx > viewport_width * 2 / 3
        else "center"
    )
    vertical = (
        "top"
        if cy < viewport_height / 3
        else "bottom"
        if cy > viewport_height * 2 / 3
        else "middle"
    )
    if horizontal == "center" and vertical == "middle":
        return "center"
    if horizontal == "center":
        return vertical
    if vertical == "middle":
        return horizontal
    return f"{vertical}-{horizontal}"


def normalize_visual_reflex_request(payload: dict[str, Any]) -> VisualReflexRequest:
    if not isinstance(payload, dict):
        raise VisualReflexError("visual reflex request must be an object")
    allowed = {
        "schema",
        "request_id",
        "goal",
        "machine_id",
        "viewport",
        "targets",
        "state",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise VisualReflexError(
            f"visual reflex request contains unsupported fields: {', '.join(unknown)}"
        )
    schema = str(payload.get("schema") or VISUAL_REFLEX_REQUEST_SCHEMA).strip()
    if schema != VISUAL_REFLEX_REQUEST_SCHEMA:
        raise VisualReflexError(f"unsupported visual reflex schema: {schema}")

    request_id = _clean_id(payload.get("request_id"), field="request_id")
    goal = _clean_text(payload.get("goal"), field="goal", maximum=2000)
    machine_id = _clean_id(payload.get("machine_id"), field="machine_id")

    viewport = payload.get("viewport")
    if not isinstance(viewport, dict):
        raise VisualReflexError("viewport must be an object")
    unknown_viewport = sorted(set(viewport) - {"width", "height"})
    if unknown_viewport:
        raise VisualReflexError(
            f"viewport contains unsupported fields: {', '.join(unknown_viewport)}"
        )
    viewport_width = _number(
        viewport.get("width"), field="viewport.width", positive=True
    )
    viewport_height = _number(
        viewport.get("height"), field="viewport.height", positive=True
    )

    raw_state = payload.get("state", {})
    if not isinstance(raw_state, dict):
        raise VisualReflexError("state must be an object")

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise VisualReflexError("targets must be a non-empty array")
    if len(raw_targets) > MAX_TARGETS:
        raise VisualReflexError(f"targets exceeds maximum of {MAX_TARGETS}")

    targets: list[VisualTarget] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise VisualReflexError(f"targets[{index}] must be an object")
        allowed_target = {
            "target_id",
            "label",
            "role",
            "source",
            "bounds",
            "confidence",
        }
        unknown_target = sorted(set(raw) - allowed_target)
        if unknown_target:
            raise VisualReflexError(
                f"targets[{index}] contains unsupported fields: "
                f"{', '.join(unknown_target)}"
            )

        target_id = _clean_id(
            raw.get("target_id"), field=f"targets[{index}].target_id"
        )
        if target_id in seen:
            raise VisualReflexError(f"duplicate target_id: {target_id}")
        seen.add(target_id)

        source = str(raw.get("source") or "").strip().lower()
        if source not in _SOURCE_PRIORITY:
            raise VisualReflexError(
                f"targets[{index}].source must be one of {sorted(_SOURCE_PRIORITY)}"
            )

        raw_bounds = raw.get("bounds")
        if not isinstance(raw_bounds, dict):
            raise VisualReflexError(f"targets[{index}].bounds must be an object")
        unknown_bounds = sorted(set(raw_bounds) - {"x", "y", "width", "height"})
        if unknown_bounds:
            raise VisualReflexError(
                f"targets[{index}].bounds contains unsupported fields: "
                f"{', '.join(unknown_bounds)}"
            )
        bounds = VisualBounds(
            x=_number(raw_bounds.get("x"), field=f"targets[{index}].bounds.x"),
            y=_number(raw_bounds.get("y"), field=f"targets[{index}].bounds.y"),
            width=_number(
                raw_bounds.get("width"),
                field=f"targets[{index}].bounds.width",
                positive=True,
            ),
            height=_number(
                raw_bounds.get("height"),
                field=f"targets[{index}].bounds.height",
                positive=True,
            ),
        )
        if (
            bounds.x < 0
            or bounds.y < 0
            or bounds.x + bounds.width > viewport_width
            or bounds.y + bounds.height > viewport_height
        ):
            raise VisualReflexError(f"targets[{index}].bounds is outside viewport")

        confidence = _number(
            raw.get("confidence", 1.0),
            field=f"targets[{index}].confidence",
        )
        if not 0.0 <= confidence <= 1.0:
            raise VisualReflexError(
                f"targets[{index}].confidence must be between 0 and 1"
            )

        targets.append(
            VisualTarget(
                target_id=target_id,
                label=_clean_text(
                    raw.get("label"), field=f"targets[{index}].label"
                ),
                role=_clean_text(
                    raw.get("role"),
                    field=f"targets[{index}].role",
                    maximum=120,
                ),
                source=source,  # type: ignore[arg-type]
                bounds=bounds,
                confidence=confidence,
            )
        )

    return VisualReflexRequest(
        request_id=request_id,
        goal=goal,
        machine_id=machine_id,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        targets=tuple(targets),
        state=dict(raw_state),
    )


def build_visual_action_envelope(request: VisualReflexRequest) -> ActionEnvelope:
    actions = []
    for target in request.targets:
        actions.append(
            {
                "action_id": target.target_id,
                "operation": "click_target",
                "title": target.label,
                "description": (
                    f"UI target role={target.role}; source={target.source}; "
                    f"position={_position_hint(target.bounds, request.viewport_width, request.viewport_height)}."
                ),
                "permission_class": "execute",
                "arguments": {"target_id": target.target_id},
                "constraints": {
                    "source": target.source,
                    "detector_confidence": target.confidence,
                    "coordinate_authority": "hirda_runtime_only",
                },
                "risk": {
                    "consequential": False,
                    "reversible": True,
                    "external_side_effect": True,
                    "destructive": False,
                    "financial": False,
                    "requires_human_approval": False,
                },
            }
        )
    return normalize_action_envelope(
        {
            "schema": ACTION_ENVELOPE_SCHEMA,
            "request_id": request.request_id,
            "provider": "visual-reflex",
            "domain": "computer",
            "goal": request.goal,
            "state": {
                **_strip_geometry(request.state),
                "machine_id": request.machine_id,
                "coordinate_policy": (
                    "JEV chooses target_id only; HIRDA runtime owns coordinates."
                ),
            },
            "actions": actions,
            "metadata": {
                "executor_attached": False,
                "side_effect_free": True,
                "target_count": len(request.targets),
            },
        }
    )


def _fallback_target(request: VisualReflexRequest) -> VisualTarget:
    return min(
        request.targets,
        key=lambda target: (
            _SOURCE_PRIORITY[target.source],
            -target.confidence,
            target.target_id,
        ),
    )


async def evaluate_visual_reflex(
    studio: Any,
    payload: VisualReflexRequest | dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    request = (
        payload
        if isinstance(payload, VisualReflexRequest)
        else normalize_visual_reflex_request(payload)
    )

    build_started = time.perf_counter()
    envelope = build_visual_action_envelope(request)
    build_ms = (time.perf_counter() - build_started) * 1000.0

    jev_started = time.perf_counter()
    judgment: JevActionJudgment = await evaluate_action_envelope(studio, envelope)
    jev_ms = (time.perf_counter() - jev_started) * 1000.0

    by_id = {target.target_id: target for target in request.targets}
    selected = by_id.get(str(judgment.selected_action_id or ""))
    selection_source = "jev"
    if selected is None:
        selected = _fallback_target(request)
        selection_source = "fallback"

    click_x, click_y = selected.bounds.center
    total_ms = (time.perf_counter() - started) * 1000.0
    return {
        "schema": VISUAL_REFLEX_RESULT_SCHEMA,
        "request_id": request.request_id,
        "machine_id": request.machine_id,
        "selected_target_id": selected.target_id,
        "selection_source": selection_source,
        "target": {
            "label": selected.label,
            "role": selected.role,
            "source": selected.source,
            "confidence": selected.confidence,
        },
        "execution_plan": {
            "action": "click",
            "machine_id": request.machine_id,
            "target_id": selected.target_id,
            "x": click_x,
            "y": click_y,
            "button": "left",
            "click_count": 1,
            "permission_class": "execute",
            "runtime_authorization_required": True,
            "may_execute": False,
        },
        "jev": judgment.as_dict(),
        "metrics": {
            "build_ms": round(build_ms, 3),
            "jev_ms": round(jev_ms, 3),
            "total_ms": round(total_ms, 3),
            "candidate_count": len(request.targets),
        },
    }
