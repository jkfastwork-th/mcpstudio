from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any


COGNITIVE_OBSERVABILITY_SCHEMA = "hirda-cognitive-observability-v1"
_PLAN_EVENT = "cognition.plan.observed"
_EXECUTE_EVENT = "cognition.execute.observed"
_ERROR_EVENT = "cognition.endpoint.error"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    value = ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
    return round(value, 3)


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _request_meta(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "request_id": str(payload.get("request_id") or "").strip() or None,
        "capability": str(payload.get("capability") or "").strip() or None,
        "preferred_depth": str(payload.get("preferred_depth") or "").strip() or None,
        "preferred_runtime": str(payload.get("preferred_runtime") or "").strip().casefold() or None,
        "allow_fallback": bool(payload.get("allow_fallback", True)),
    }


class CognitiveObservability:
    def __init__(self, db: Any, runtimes: Any, studio: Any) -> None:
        self.db = db
        self.runtimes = runtimes
        self.studio = studio
        self.last_error: str | None = None

    def _runtime_rows(self) -> dict[str, dict[str, Any]]:
        try:
            snapshot = self.runtimes.snapshot(force=False)
        except Exception:
            return {}
        return {
            str(row.get("id") or "").strip().casefold(): dict(row)
            for row in snapshot.get("runtimes", [])
            if isinstance(row, dict) and str(row.get("id") or "").strip()
        }

    @staticmethod
    def _runtime_layer_ready(row: dict[str, Any]) -> bool:
        if str(row.get("status") or "").strip().lower() != "ready":
            return False
        auth = row.get("auth_health")
        auth_status = str(auth.get("status") or "").strip().lower() if isinstance(auth, dict) else "unknown"
        if auth_status in {"unauthenticated", "expired", "error", "failed", "invalid"}:
            return False
        limit = row.get("limit_health")
        limit_status = str(limit.get("status") or "").strip().lower() if isinstance(limit, dict) else "unknown"
        return limit_status not in {"blocked", "exhausted", "constrained", "unavailable", "error"}


    def _routing_summary(self, infrastructure: Any) -> dict[str, Any]:
        routing = dict(infrastructure) if isinstance(infrastructure, dict) else {}
        runtime_rows = self._runtime_rows()
        decisions: list[dict[str, Any]] = []
        rejection_reasons: Counter[str] = Counter()
        suspected_false_rejections: list[dict[str, Any]] = []
        selected_health: list[dict[str, Any]] = []
        filtered = {
            str(item or "").strip().casefold()
            for item in routing.get("filtered_runtimes", [])
            if str(item or "").strip()
        }

        for raw in routing.get("decisions", []):
            if not isinstance(raw, dict):
                continue
            runtime = str(raw.get("runtime") or "").strip().casefold()
            selected = dict(raw.get("selected")) if isinstance(raw.get("selected"), dict) else None
            route_rejections = [
                dict(item)
                for item in raw.get("route_rejections", [])
                if isinstance(item, dict)
            ]
            reasons = [
                str(item.get("reason") or "").strip()
                for item in route_rejections
                if str(item.get("reason") or "").strip()
            ]
            rejection_reasons.update(reasons)

            health = dict(selected.get("health") or {}) if isinstance(selected, dict) else {}
            if health:
                selected_health.append(
                    {
                        "runtime": runtime,
                        "node_id": selected.get("node_id"),
                        "backend_id": selected.get("backend_id"),
                        "load_ratio_1m": health.get("load_ratio_1m"),
                        "memory_available_ratio": health.get("memory_available_ratio"),
                        "backend_latency_ms": health.get("backend_latency_ms"),
                    }
                )

            runtime_layer_ready = self._runtime_layer_ready(runtime_rows.get(runtime, {}))
            if (
                routing.get("mode") == "enforce"
                and runtime in filtered
                and not bool(raw.get("eligible"))
                and runtime_layer_ready
            ):
                suspected_false_rejections.append(
                    {
                        "runtime": runtime,
                        "reason": str(raw.get("reason") or "no_eligible_route"),
                        "route_rejections": reasons,
                    }
                )

            decisions.append(
                {
                    "runtime": runtime,
                    "eligible": bool(raw.get("eligible")),
                    "reason": str(raw.get("reason") or "").strip() or None,
                    "route_rejections": reasons,
                    "runtime_layer_ready": runtime_layer_ready,
                    "selected": (
                        {
                            "node_id": selected.get("node_id"),
                            "backend_id": selected.get("backend_id"),
                            "model_id": selected.get("model_id"),
                            "scope": selected.get("scope"),
                        }
                        if selected is not None
                        else None
                    ),
                }
            )

        return {
            "mode": str(routing.get("mode") or "off"),
            "enforced": bool(routing.get("enforced")),
            "candidate_count_before": int(routing.get("candidate_count_before") or 0),
            "candidate_count_after": int(routing.get("candidate_count_after") or 0),
            "eligible_runtimes": [
                str(item or "").strip().casefold()
                for item in routing.get("eligible_runtimes", [])
                if str(item or "").strip()
            ],
            "filtered_runtimes": sorted(filtered),
            "routing_authority": routing.get("routing_authority"),
            "semantic_authority_changed": bool(routing.get("semantic_authority_changed")),
            "decisions": decisions,
            "rejection_reasons": dict(rejection_reasons),
            "selected_health": selected_health,
            "suspected_false_rejections": suspected_false_rejections,
        }


    async def _add_event(
        self,
        kind: str,
        message: str,
        *,
        data: dict[str, Any],
        request_id: str | None,
        severity: str = "info",
    ) -> None:
        try:
            await self.db.add_event(
                kind,
                message,
                severity=severity,
                data=data,
                stream_id=f"cognition:{request_id}" if request_id else "cognition:unscoped",
                correlation_id=request_id,
            )
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    async def record_plan(
        self,
        payload: Any,
        plan: dict[str, Any],
        *,
        elapsed_ms: float,
    ) -> None:
        meta = _request_meta(payload)
        request = plan.get("request") if isinstance(plan.get("request"), dict) else {}
        request_id = str(request.get("request_id") or meta.get("request_id") or "").strip() or None
        capability = str(request.get("capability") or meta.get("capability") or "").strip() or None
        routing = self._routing_summary(plan.get("infrastructure_routing"))
        data = {
            **meta,
            "request_id": request_id,
            "capability": capability,
            "lane": str(plan.get("lane") or "").strip() or None,
            "deterministic_lane": str(plan.get("deterministic_lane") or "").strip() or None,
            "candidate_runtimes": [
                str(item.get("runtime") or "").strip().casefold()
                for item in plan.get("candidates", [])
                if isinstance(item, dict) and str(item.get("runtime") or "").strip()
            ],
            "routing": routing,
            "identity_continuity": bool(plan.get("identity_continuity", True)),
            "semantic_authority_changed": bool(plan.get("semantic_authority_changed", False)),
            "elapsed_ms": round(max(0.0, float(elapsed_ms)), 3),
        }
        await self._add_event(
            _PLAN_EVENT,
            "Cognitive plan observed",
            data=data,
            request_id=request_id,
            severity=(
                "warning"
                if routing["suspected_false_rejections"]
                or data["semantic_authority_changed"]
                or not data["identity_continuity"]
                else "info"
            ),
        )

    async def record_execute(
        self,
        payload: Any,
        result: dict[str, Any],
        *,
        elapsed_ms: float,
    ) -> None:
        meta = _request_meta(payload)
        request_id = str(result.get("request_id") or meta.get("request_id") or "").strip() or None
        routing = self._routing_summary(result.get("infrastructure_routing"))
        attempts = []
        for raw in result.get("attempts", []):
            if not isinstance(raw, dict):
                continue
            attempts.append(
                {
                    "runtime": str(raw.get("runtime") or "").strip().casefold(),
                    "status": str(raw.get("status") or "").strip().lower(),
                    "error": str(raw.get("error") or "").strip() or None,
                }
            )
        data = {
            **meta,
            "request_id": request_id,
            "status": str(result.get("status") or "").strip().lower(),
            "lane": str(result.get("lane") or "").strip() or None,
            "runtime": str(result.get("runtime") or "").strip().casefold() or None,
            "provider": str(result.get("provider") or "").strip() or None,
            "model": str(result.get("model") or "").strip() or None,
            "degraded": bool(result.get("degraded")),
            "fallback_used": bool(result.get("fallback_used")),
            "attempts": attempts,
            "routing": routing,
            "identity_continuity": bool(result.get("identity_continuity", True)),
            "semantic_authority_changed": bool(result.get("semantic_authority_changed", False)),
            "error": str(result.get("error") or "").strip() or None,
            "elapsed_ms": round(max(0.0, float(elapsed_ms)), 3),
        }
        severity = "info"
        if (
            data["status"] != "success"
            or routing["suspected_false_rejections"]
            or data["semantic_authority_changed"]
            or not data["identity_continuity"]
        ):
            severity = "warning"
        await self._add_event(
            _EXECUTE_EVENT,
            "Cognitive execution observed",
            data=data,
            request_id=request_id,
            severity=severity,
        )

    async def record_error(
        self,
        stage: str,
        payload: Any,
        error: Exception,
        *,
        elapsed_ms: float,
    ) -> None:
        meta = _request_meta(payload)
        request_id = meta.get("request_id")
        await self._add_event(
            _ERROR_EVENT,
            "Cognitive endpoint error",
            severity="warning",
            request_id=request_id,
            data={
                **meta,
                "stage": str(stage or "unknown"),
                "error_type": type(error).__name__,
                "error_code": (
                    str(error)
                    if type(error).__name__ == "CognitiveRouterError"
                    else type(error).__name__
                ),
                "elapsed_ms": round(max(0.0, float(elapsed_ms)), 3),
            },
        )


    async def report(
        self,
        *,
        window_minutes: int | None = None,
    ) -> dict[str, Any]:
        configured_window = int(
            getattr(self.studio, "cognitive_soak_window_minutes", 1440) or 1440
        )
        window = max(1, min(int(window_minutes or configured_window), 7 * 24 * 60))
        cutoff = _utcnow() - timedelta(minutes=window)
        rows = await self.db.list_events(
            kind_prefix="cognition.",
            limit=10000,
            ascending=False,
        )
        events = [
            row
            for row in rows
            if (
                (stamp := _parse_time(row.get("created_at"))) is not None
                and stamp >= cutoff
            )
        ]
        plans = [row for row in events if row.get("kind") == _PLAN_EVENT]
        executions = [row for row in events if row.get("kind") == _EXECUTE_EVENT]
        errors = [row for row in events if row.get("kind") == _ERROR_EVENT]

        plan_latencies = [
            value
            for row in plans
            if (value := _safe_float((row.get("data") or {}).get("elapsed_ms"))) is not None
        ]
        execute_latencies = [
            value
            for row in executions
            if (value := _safe_float((row.get("data") or {}).get("elapsed_ms"))) is not None
        ]

        execution_success = 0
        fallback_count = 0
        degraded_count = 0
        semantic_change_count = 0
        identity_failure_count = 0
        filtered_candidate_count = 0
        suspected_false_rejection_count = 0
        rejection_reasons: Counter[str] = Counter()
        runtime_success: Counter[str] = Counter()
        runtime_failure: Counter[str] = Counter()
        attempt_errors: Counter[str] = Counter()
        filtered_by_runtime: Counter[str] = Counter()
        selected_loads: list[float] = []
        selected_latencies: list[float] = []

        for row in executions:
            data = row.get("data") or {}
            if data.get("status") == "success":
                execution_success += 1
            if bool(data.get("fallback_used")):
                fallback_count += 1
            if bool(data.get("degraded")):
                degraded_count += 1
            if bool(data.get("semantic_authority_changed")):
                semantic_change_count += 1
            if not bool(data.get("identity_continuity", True)):
                identity_failure_count += 1

            runtime = str(data.get("runtime") or "").strip().casefold()
            if runtime and data.get("status") == "success":
                runtime_success[runtime] += 1

            for attempt in data.get("attempts", []):
                if not isinstance(attempt, dict):
                    continue
                attempt_runtime = str(attempt.get("runtime") or "").strip().casefold()
                if attempt.get("status") == "failed" and attempt_runtime:
                    runtime_failure[attempt_runtime] += 1
                error = str(attempt.get("error") or "").strip()
                if error:
                    attempt_errors[error] += 1

            routing = data.get("routing") or {}
            filtered = [
                str(item or "").strip().casefold()
                for item in routing.get("filtered_runtimes", [])
                if str(item or "").strip()
            ]
            filtered_candidate_count += len(filtered)
            filtered_by_runtime.update(filtered)
            suspected_false_rejection_count += len(
                routing.get("suspected_false_rejections", [])
            )
            rejection_reasons.update(
                {
                    str(key): int(value)
                    for key, value in (routing.get("rejection_reasons") or {}).items()
                }
            )
            for health in routing.get("selected_health", []):
                if not isinstance(health, dict):
                    continue
                load = _safe_float(health.get("load_ratio_1m"))
                latency = _safe_float(health.get("backend_latency_ms"))
                if load is not None:
                    selected_loads.append(load)
                if latency is not None:
                    selected_latencies.append(latency)

        for row in plans:
            data = row.get("data") or {}
            if bool(data.get("semantic_authority_changed")):
                semantic_change_count += 1
            if not bool(data.get("identity_continuity", True)):
                identity_failure_count += 1
            routing = data.get("routing") or {}
            suspected_false_rejection_count += len(
                routing.get("suspected_false_rejections", [])
            )

        execution_count = len(executions)
        execution_failure = execution_count - execution_success
        success_percent = (
            round(execution_success / execution_count * 100.0, 3)
            if execution_count
            else None
        )
        fallback_percent = (
            round(fallback_count / execution_count * 100.0, 3)
            if execution_count
            else None
        )

        min_executions = max(
            1,
            int(getattr(self.studio, "cognitive_soak_min_executions", 20) or 20),
        )
        failure_warn = max(
            0.0,
            min(
                100.0,
                float(
                    getattr(
                        self.studio,
                        "cognitive_soak_failure_warn_percent",
                        5.0,
                    )
                    or 5.0
                ),
            ),
        )
        fallback_warn = max(
            0.0,
            min(
                100.0,
                float(
                    getattr(
                        self.studio,
                        "cognitive_soak_fallback_warn_percent",
                        25.0,
                    )
                    or 25.0
                ),
            ),
        )

        state = "healthy"
        reasons: list[str] = []
        if semantic_change_count > 0 or identity_failure_count > 0:
            state = "down"
            reasons.append("semantic_or_identity_integrity_failure")
        elif execution_count < min_executions:
            state = "warming"
            reasons.append("insufficient_execution_samples")
        else:
            failure_percent = 100.0 - float(success_percent or 0.0)
            if failure_percent > failure_warn:
                state = "degraded"
                reasons.append("execution_failure_rate_high")
            if fallback_percent is not None and fallback_percent > fallback_warn:
                state = "degraded"
                reasons.append("fallback_rate_high")
            if suspected_false_rejection_count > 0:
                state = "degraded"
                reasons.append("suspected_false_rejection_review")

        return {
            "schema": COGNITIVE_OBSERVABILITY_SCHEMA,
            "generated_at": _utcnow().isoformat(),
            "window_minutes": window,
            "window_started_at": cutoff.isoformat(),
            "routing_mode": str(
                getattr(self.studio, "cognitive_node_routing_mode", "off")
            ),
            "state": state,
            "state_reasons": reasons,
            "thresholds": {
                "min_executions": min_executions,
                "failure_warn_percent": failure_warn,
                "fallback_warn_percent": fallback_warn,
            },
            "metrics": {
                "plan_count": len(plans),
                "execute_count": execution_count,
                "endpoint_error_count": len(errors),
                "execute_success_count": execution_success,
                "execute_failure_count": execution_failure,
                "execute_success_percent": success_percent,
                "fallback_count": fallback_count,
                "fallback_percent": fallback_percent,
                "degraded_count": degraded_count,
                "filtered_candidate_count": filtered_candidate_count,
                "suspected_false_rejection_count": suspected_false_rejection_count,
                "semantic_authority_change_count": semantic_change_count,
                "identity_continuity_failure_count": identity_failure_count,
                "plan_avg_latency_ms": (
                    round(sum(plan_latencies) / len(plan_latencies), 3)
                    if plan_latencies
                    else None
                ),
                "plan_p95_latency_ms": _percentile(plan_latencies, 0.95),
                "execute_avg_latency_ms": (
                    round(sum(execute_latencies) / len(execute_latencies), 3)
                    if execute_latencies
                    else None
                ),
                "execute_p95_latency_ms": _percentile(execute_latencies, 0.95),
                "selected_load_ratio_avg": (
                    round(sum(selected_loads) / len(selected_loads), 6)
                    if selected_loads
                    else None
                ),
                "selected_backend_latency_p95_ms": _percentile(
                    selected_latencies,
                    0.95,
                ),
            },
            "runtime": {
                "successes": dict(runtime_success),
                "failures": dict(runtime_failure),
                "filtered": dict(filtered_by_runtime),
            },
            "rejections": {
                "reasons": dict(rejection_reasons),
                "attempt_errors": dict(attempt_errors),
            },
            "integrity": {
                "identity_continuity": identity_failure_count == 0,
                "semantic_authority_unchanged": semantic_change_count == 0,
            },
            "privacy": {
                "prompt_recorded": False,
                "output_recorded": False,
            },
            "last_observer_error": self.last_error,
        }
