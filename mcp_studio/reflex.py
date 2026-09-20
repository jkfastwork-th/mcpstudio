from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REFLEX_VERSION = "hirda-reflex-v0.1"

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|credential)",
    re.IGNORECASE,
)
_FORCE_FLAG = re.compile(r"(?:^|\s)(?:-f|--force|--hard)(?:\s|$)", re.IGNORECASE)
_PRIVILEGE = re.compile(r"(?:^|[;&|]\s*)(?:sudo|su)\b", re.IGNORECASE)
_NETWORK_WRITE = re.compile(
    r"\b(?:curl|wget)\b.*(?:--data(?:-binary)?|-d\b|--upload-file|-T\b|--request\s+(?:POST|PUT|PATCH|DELETE)|-X\s*(?:POST|PUT|PATCH|DELETE))",
    re.IGNORECASE,
)
_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:authorization|bearer|password|secret|token|api[_-]?key|credential)\b"
)
_RISKY_GIT = re.compile(
    r"\bgit\s+(?:checkout|switch|merge|rebase|cherry-pick|tag)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ReflexDecision:
    enabled: bool
    evaluated: bool
    mode: str
    action: str
    risk: float
    confidence: float
    compute_lane: str
    blocked: bool
    code: str | None
    message: str
    version: str
    signals: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["signals"] = list(self.signals)
        return value


@dataclass(frozen=True, slots=True)
class ReflexFeatures:
    tool: str
    permission_class: str
    arg_key_count: int
    has_sensitive_arg_key: bool
    shell_command: str | None
    shell_subcommand: str | None
    has_force_flag: bool
    has_privilege_signal: bool
    has_network_write_signal: bool
    has_credential_text_signal: bool
    risky_git_operation: bool
    argument_size_bucket: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _base_tool_name(name: str) -> str:
    return str(name or "").strip().rsplit("__", 1)[-1]


def _bounded(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 3)


def _command_shape(arguments: dict[str, Any]) -> tuple[str | None, str | None, str]:
    raw = arguments.get("command")
    if not isinstance(raw, str) or not raw.strip():
        return None, None, ""
    text = raw.strip()
    first_segment = re.split(r"\s*(?:&&|\|\||;|\|)\s*", text, maxsplit=1)[0]
    tokens = first_segment.split()
    if not tokens:
        return None, None, text
    command = Path(tokens[0]).name
    subcommand = tokens[1] if len(tokens) > 1 and not tokens[1].startswith("-") else None
    return command[:80], subcommand[:80] if subcommand else None, text


def extract_features(
    tool_name: str,
    arguments: dict[str, Any] | None,
    category: str,
) -> ReflexFeatures:
    args = arguments or {}
    command, subcommand, command_text = _command_shape(args)
    sensitive_key = any(_SENSITIVE_KEY.search(str(key)) for key in args)
    serialized_size = 0
    credential_text = False
    for key, value in list(args.items())[:64]:
        if _SENSITIVE_KEY.search(str(key)):
            continue
        if isinstance(value, str):
            serialized_size += len(value)
            if _CREDENTIAL_TEXT.search(value):
                credential_text = True
        elif isinstance(value, (list, tuple, dict)):
            try:
                serialized_size += len(json.dumps(value, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                serialized_size += 256
        else:
            serialized_size += len(str(value))
    if serialized_size < 512:
        bucket = "small"
    elif serialized_size < 4096:
        bucket = "medium"
    else:
        bucket = "large"

    return ReflexFeatures(
        tool=_base_tool_name(tool_name),
        permission_class=str(category or "unknown"),
        arg_key_count=len(args),
        has_sensitive_arg_key=bool(sensitive_key),
        shell_command=command,
        shell_subcommand=subcommand,
        has_force_flag=bool(_FORCE_FLAG.search(command_text)),
        has_privilege_signal=bool(_PRIVILEGE.search(command_text)),
        has_network_write_signal=bool(_NETWORK_WRITE.search(command_text)),
        has_credential_text_signal=bool(credential_text or _CREDENTIAL_TEXT.search(command_text)),
        risky_git_operation=bool(_RISKY_GIT.search(command_text)),
        argument_size_bucket=bucket,
    )


def evaluate_tool_call(
    studio: Any,
    tool_name: str,
    arguments: dict[str, Any] | None,
    category: str,
) -> tuple[ReflexDecision, ReflexFeatures]:
    """Run HIRDA's local System-One gate after deterministic permissions pass."""

    mode = str(getattr(studio, "reflex_mode", "enforce") or "enforce").strip().lower()
    if mode not in {"shadow", "enforce"}:
        mode = "enforce"
    features = extract_features(tool_name, arguments, category)

    if not bool(getattr(studio, "reflex_enabled", True)):
        return (
            ReflexDecision(
                enabled=False,
                evaluated=False,
                mode=mode,
                action="allow",
                risk=0.0,
                confidence=0.0,
                compute_lane="fast",
                blocked=False,
                code=None,
                message="HIRDA Reflex is disabled.",
                version=REFLEX_VERSION,
                signals=(),
            ),
            features,
        )

    if (
        features.permission_class == "read"
        and not bool(getattr(studio, "reflex_evaluate_read_tools", False))
    ):
        return (
            ReflexDecision(
                enabled=True,
                evaluated=False,
                mode=mode,
                action="allow",
                risk=0.03,
                confidence=0.99,
                compute_lane="fast",
                blocked=False,
                code=None,
                message="HIRDA Reflex skipped a read-only tool by policy.",
                version=REFLEX_VERSION,
                signals=("read_fast_path",),
            ),
            features,
        )

    base_risk = {
        "read": 0.05,
        "write": 0.24,
        "execute": 0.34,
        "destructive": 0.90,
        "unknown": 0.72,
    }.get(features.permission_class, 0.60)

    risk = base_risk
    signals: list[str] = []
    if features.has_sensitive_arg_key:
        risk += 0.12
        signals.append("sensitive_argument_key")
    if features.has_credential_text_signal:
        risk += 0.18
        signals.append("credential_text")
    if features.has_force_flag:
        risk += 0.14
        signals.append("force_flag")
    if features.risky_git_operation:
        risk += 0.12
        signals.append("stateful_git_operation")
    if features.has_privilege_signal:
        risk += 0.42
        signals.append("privilege_escalation")
    if features.has_network_write_signal:
        risk += 0.45
        signals.append("network_write")
    if features.argument_size_bucket == "large":
        risk += 0.05
        signals.append("large_argument_surface")

    risk = _bounded(risk)
    deny_threshold = float(getattr(studio, "reflex_deny_risk_threshold", 0.92) or 0.92)
    review_threshold = float(getattr(studio, "reflex_review_risk_threshold", 0.72) or 0.72)
    min_confidence = float(getattr(studio, "reflex_min_confidence", 0.88) or 0.88)

    if risk >= deny_threshold:
        action = "deny"
    elif risk >= review_threshold:
        action = "review"
    else:
        action = "allow"

    # V0 confidence describes rule coverage, not learned statistical calibration.
    # Explicit hazard signals are high-confidence; routine writes/executes are
    # deliberately a little lower until outcome data is available for V1.
    if features.has_privilege_signal or features.has_network_write_signal:
        confidence = 0.98
    elif action in {"review", "deny"}:
        confidence = 0.94
    elif features.permission_class == "read":
        confidence = 0.99
    elif signals:
        confidence = 0.91
    else:
        confidence = 0.89

    complexity = (
        features.argument_size_bucket == "large"
        or len(signals) >= 2
        or risk >= review_threshold
    )
    compute_lane = "deep" if complexity else "fast"

    should_block = (
        mode == "enforce"
        and action in {"review", "deny"}
        and confidence >= min_confidence
    )
    code = None
    if should_block:
        code = "REFLEX_REVIEW_REQUIRED" if action == "review" else "REFLEX_TOOL_DENIED"

    message = (
        f"HIRDA Reflex action={action} risk={risk:.3f} confidence={confidence:.3f} "
        f"lane={compute_lane} mode={mode}."
    )
    return (
        ReflexDecision(
            enabled=True,
            evaluated=True,
            mode=mode,
            action=action,
            risk=risk,
            confidence=_bounded(confidence),
            compute_lane=compute_lane,
            blocked=should_block,
            code=code,
            message=message,
            version=REFLEX_VERSION,
            signals=tuple(signals),
        ),
        features,
    )


def decision_fingerprint(
    session: dict[str, Any],
    features: ReflexFeatures,
    request_id: Any,
) -> str:
    payload = {
        "workspace_key": session.get("workspace_key"),
        "features": features.as_dict(),
        "request_id": request_id,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def append_dataset_record(studio: Any, record: dict[str, Any]) -> None:
    if not bool(getattr(studio, "reflex_dataset_enabled", True)):
        return
    raw_path = str(
        getattr(studio, "reflex_dataset_path", "./data/reflex-decisions.jsonl")
        or "./data/reflex-decisions.jsonl"
    ).strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "schema": "hirda-reflex-dataset-v1",
            **record,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    except OSError:
        # Dataset capture must never become runtime authority.
        return
