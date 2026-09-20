from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any


WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percent(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return {"samples": 0, "median_ms": None, "p95_ms": None}
    p95_index = max(0, min(len(clean) - 1, math.ceil(len(clean) * 0.95) - 1))
    return {
        "samples": len(clean),
        "median_ms": round(float(median(clean)), 3),
        "p95_ms": round(clean[p95_index], 3),
    }


class ReflexMetrics:
    """Read-only aggregate metrics over the HIRDA Reflex decision dataset."""

    def __init__(self, studio: Any):
        self.studio = studio

    @property
    def path(self) -> Path:
        raw = str(
            getattr(self.studio, "reflex_dataset_path", "./data/reflex-decisions.jsonl")
            or "./data/reflex-decisions.jsonl"
        ).strip()
        return Path(raw).expanduser()

    def _read(self) -> list[dict[str, Any]]:
        path = self.path
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("schema") == "hirda-reflex-dataset-v1":
                        rows.append(row)
        except OSError:
            return []
        return rows

    @staticmethod
    def _bucket_time(value: datetime, window: str) -> str:
        if window in {"1h", "24h"}:
            return value.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
        return value.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")

    def snapshot(
        self,
        *,
        window: str = "24h",
        recent: int = 20,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if window not in WINDOWS:
            window = "24h"
        recent = max(1, min(100, int(recent)))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        since = current - WINDOWS[window]
        rows = self._read()

        total_records = len(rows)
        timestamps = [_parse_time(row.get("recorded_at")) for row in rows]
        valid_timestamps = [stamp for stamp in timestamps if stamp is not None]
        first_recorded_at = min(valid_timestamps).isoformat().replace("+00:00", "Z") if valid_timestamps else None
        last_recorded_at = max(valid_timestamps).isoformat().replace("+00:00", "Z") if valid_timestamps else None

        window_rows = [
            row for row in rows
            if (stamp := _parse_time(row.get("recorded_at"))) is not None and stamp >= since
        ]
        decisions = [row for row in window_rows if row.get("kind") == "decision"]
        outcomes = [row for row in window_rows if row.get("kind") == "outcome"]

        action_counts: Counter[str] = Counter()
        lane_counts: Counter[str] = Counter()
        permission_counts: Counter[str] = Counter()
        signal_counts: Counter[str] = Counter()
        workspace_counts: Counter[str] = Counter()
        risk_counts: Counter[str] = Counter()
        teacher_action_counts: Counter[str] = Counter()
        teacher_error_counts: Counter[str] = Counter()
        reflex_latency: list[float] = []
        teacher_latency: list[float] = []
        policy_latency: list[float] = []
        teacher_enabled = 0
        teacher_evaluated = 0
        teacher_agreed = 0
        teacher_disagreed = 0
        blocked = 0
        confidence_total = 0.0
        risk_total = 0.0
        timeline: Counter[str] = Counter()
        timeline_allow: Counter[str] = Counter()
        timeline_review: Counter[str] = Counter()
        timeline_deny: Counter[str] = Counter()

        review_threshold = float(getattr(self.studio, "reflex_review_risk_threshold", 0.72) or 0.72)
        deny_threshold = float(getattr(self.studio, "reflex_deny_risk_threshold", 0.92) or 0.92)

        recent_rows: list[dict[str, Any]] = []
        for row in decisions:
            reflex = _mapping(row.get("reflex"))
            features = _mapping(row.get("features"))
            teacher = _mapping(row.get("teacher"))
            action = str(reflex.get("action") or "unknown")
            lane = str(reflex.get("compute_lane") or "unknown")
            permission = str(features.get("permission_class") or "unknown")
            workspace = str(row.get("workspace_key") or "unknown")
            try:
                risk = float(reflex.get("risk") or 0.0)
            except (TypeError, ValueError):
                risk = 0.0
            try:
                confidence = float(reflex.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0

            action_counts[action] += 1
            lane_counts[lane] += 1
            permission_counts[permission] += 1
            workspace_counts[workspace] += 1
            blocked += int(bool(reflex.get("blocked")))
            risk_total += risk
            confidence_total += confidence

            if risk >= deny_threshold:
                risk_counts["deny"] += 1
            elif risk >= review_threshold:
                risk_counts["review"] += 1
            elif risk >= 0.35:
                risk_counts["elevated"] += 1
            else:
                risk_counts["low"] += 1

            signals = _list(reflex.get("signals"))
            for signal in signals:
                clean = str(signal or "").strip()
                if clean:
                    signal_counts[clean] += 1

            if teacher.get("enabled"):
                teacher_enabled += 1
            if teacher.get("evaluated"):
                teacher_evaluated += 1
                teacher_action = str(teacher.get("action") or "unknown")
                teacher_action_counts[teacher_action] += 1
                if teacher.get("agreement") is True:
                    teacher_agreed += 1
                elif teacher.get("agreement") is False:
                    teacher_disagreed += 1
            error = str(teacher.get("error") or "").strip()
            if error:
                teacher_error_counts[error] += 1

            timings = _mapping(row.get("timings"))
            policy_value = _safe_float(timings.get("policy_ms"))
            reflex_value = _safe_float(timings.get("reflex_ms"))
            teacher_value = _safe_float(timings.get("teacher_ms"))
            if policy_value is not None:
                policy_latency.append(policy_value)
            if reflex_value is not None:
                reflex_latency.append(reflex_value)
            if teacher.get("enabled") and teacher_value is not None:
                teacher_latency.append(teacher_value)

            stamp = _parse_time(row.get("recorded_at"))
            if stamp:
                bucket = self._bucket_time(stamp, window)
                timeline[bucket] += 1
                if action == "allow":
                    timeline_allow[bucket] += 1
                elif action == "review":
                    timeline_review[bucket] += 1
                elif action == "deny":
                    timeline_deny[bucket] += 1

            recent_rows.append({
                "recorded_at": row.get("recorded_at"),
                "reflex_id": row.get("reflex_id"),
                "workspace_key": row.get("workspace_key"),
                "tool": features.get("tool"),
                "permission_class": permission,
                "action": action,
                "risk": round(risk, 3),
                "confidence": round(confidence, 3),
                "compute_lane": lane,
                "blocked": bool(reflex.get("blocked")),
                "signals": [str(signal) for signal in signals[:8]],
                "teacher": {
                    "enabled": bool(teacher.get("enabled")),
                    "evaluated": bool(teacher.get("evaluated")),
                    "action": teacher.get("action") if teacher.get("evaluated") else None,
                    "agreement": teacher.get("agreement") if teacher.get("evaluated") else None,
                    "error": teacher.get("error"),
                    "model": teacher.get("model"),
                },
                "timings": {
                    "policy_ms": timings.get("policy_ms"),
                    "reflex_ms": timings.get("reflex_ms"),
                    "teacher_ms": timings.get("teacher_ms"),
                },
            })

        transport_success = sum(1 for row in outcomes if row.get("http_success") is True)
        transport_failures = sum(1 for row in outcomes if row.get("http_success") is False)
        decision_count = len(decisions)

        series = [
            {
                "bucket": bucket,
                "decisions": timeline[bucket],
                "allow": timeline_allow[bucket],
                "review": timeline_review[bucket],
                "deny": timeline_deny[bucket],
            }
            for bucket in sorted(timeline)
        ]

        recent_rows.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)

        return {
            "version": "hirda-reflex-metrics-v1",
            "window": window,
            "generated_at": current.isoformat().replace("+00:00", "Z"),
            "state": "ready" if rows else "empty",
            "config": {
                "reflex_enabled": bool(getattr(self.studio, "reflex_enabled", True)),
                "reflex_mode": str(getattr(self.studio, "reflex_mode", "enforce") or "enforce"),
                "review_risk_threshold": review_threshold,
                "deny_risk_threshold": deny_threshold,
                "min_confidence": float(getattr(self.studio, "reflex_min_confidence", 0.88) or 0.88),
                "teacher_enabled": bool(getattr(self.studio, "jev_enabled", False)),
                "teacher_mode": str(getattr(self.studio, "jev_mode", "shadow") or "shadow"),
                "teacher_model": str(getattr(self.studio, "jev_model", "jev-latest") or "jev-latest"),
            },
            "summary": {
                "decisions": decision_count,
                "allow": action_counts["allow"],
                "review": action_counts["review"],
                "deny": action_counts["deny"],
                "blocked": blocked,
                "allow_rate": _percent(action_counts["allow"], decision_count),
                "review_rate": _percent(action_counts["review"], decision_count),
                "deny_rate": _percent(action_counts["deny"], decision_count),
                "average_risk": round(risk_total / decision_count, 3) if decision_count else None,
                "average_confidence": round(confidence_total / decision_count, 3) if decision_count else None,
            },
            "teacher": {
                "enabled_decisions": teacher_enabled,
                "evaluated": teacher_evaluated,
                "availability_rate": _percent(teacher_evaluated, teacher_enabled),
                "agreement": teacher_agreed,
                "disagreement": teacher_disagreed,
                "agreement_rate": _percent(teacher_agreed, teacher_evaluated),
                "actions": dict(teacher_action_counts),
                "errors": [
                    {"error": name, "count": count}
                    for name, count in teacher_error_counts.most_common(12)
                ],
            },
            "latency": {
                "policy": _latency_summary(policy_latency),
                "reflex": _latency_summary(reflex_latency),
                "teacher": _latency_summary(teacher_latency),
            },
            "outcomes": {
                "transport_records": len(outcomes),
                "http_success": transport_success,
                "http_failure": transport_failures,
                "http_success_rate": _percent(transport_success, len(outcomes)),
                "semantics": "transport_only_not_semantic_correctness",
            },
            "distributions": {
                "actions": dict(action_counts),
                "risk": dict(risk_counts),
                "compute_lanes": dict(lane_counts),
                "permission_classes": dict(permission_counts),
                "workspaces": dict(workspace_counts),
                "signals": [
                    {"signal": name, "count": count}
                    for name, count in signal_counts.most_common(12)
                ],
            },
            "timeline": series,
            "recent": recent_rows[:recent],
            "dataset": {
                "enabled": bool(getattr(self.studio, "reflex_dataset_enabled", True)),
                "file": self.path.name,
                "records": total_records,
                "first_recorded_at": first_recorded_at,
                "last_recorded_at": last_recorded_at,
            },
            "privacy": {
                "raw_arguments_exposed": False,
                "credentials_exposed": False,
                "dataset_path_exposed": False,
            },
            "authority": {
                "static_policy_remains_hard_boundary": True,
                "reflex_may_only_narrow_allowed_calls": True,
                "jev_teacher_only": True,
            },
        }
