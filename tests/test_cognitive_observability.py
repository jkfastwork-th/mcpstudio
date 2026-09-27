from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.cognitive_observability import (
    COGNITIVE_OBSERVABILITY_SCHEMA,
    CognitiveObservability,
)
from mcp_studio.settings import StudioConfig


class FakeDB:
    def __init__(self, *, fail_add: bool = False) -> None:
        self.events: list[dict] = []
        self.fail_add = fail_add

    async def add_event(
        self,
        kind,
        message,
        *,
        severity="info",
        server_id=None,
        data=None,
        stream_id=None,
        correlation_id=None,
    ):
        if self.fail_add:
            raise RuntimeError("ledger unavailable")
        self.events.append(
            {
                "kind": kind,
                "message": message,
                "severity": severity,
                "data": dict(data or {}),
                "stream_id": stream_id,
                "correlation_id": correlation_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def list_events(
        self,
        *,
        kind_prefix=None,
        limit=None,
        ascending=True,
        **kwargs,
    ):
        rows = [
            row
            for row in self.events
            if kind_prefix is None
            or str(row.get("kind") or "").startswith(kind_prefix)
        ]
        if not ascending:
            rows = list(reversed(rows))
        return rows[:limit] if limit is not None else rows


class FakeRuntimes:
    def snapshot(self, *, force=False):
        return {
            "runtimes": [
                {
                    "id": "hermes",
                    "status": "ready",
                    "auth_health": {"status": "healthy"},
                    "limit_health": {"status": "healthy"},
                },
                {
                    "id": "codex",
                    "status": "ready",
                    "auth_health": {"status": "logged_in"},
                    "limit_health": {"status": "interactive_only"},
                },
            ]
        }


def studio(**overrides):
    values = {
        "cognitive_node_routing_mode": "enforce",
        "cognitive_soak_window_minutes": 60,
        "cognitive_soak_min_executions": 1,
        "cognitive_soak_failure_warn_percent": 5.0,
        "cognitive_soak_fallback_warn_percent": 25.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def routing(*, filtered=False):
    return {
        "mode": "enforce",
        "enforced": True,
        "candidate_count_before": 2,
        "candidate_count_after": 1 if filtered else 2,
        "eligible_runtimes": ["codex"] if filtered else ["hermes", "codex"],
        "filtered_runtimes": ["hermes"] if filtered else [],
        "routing_authority": "infrastructure_eligibility_only",
        "semantic_authority_changed": False,
        "decisions": [
            {
                "runtime": "hermes",
                "eligible": not filtered,
                "reason": "no_eligible_route" if filtered else None,
                "selected": (
                    None
                    if filtered
                    else {
                        "node_id": "openclaw",
                        "backend_id": "hermes-runtime",
                        "model_id": None,
                        "scope": "backend",
                        "health": {
                            "load_ratio_1m": 0.2,
                            "memory_available_ratio": 0.6,
                            "backend_latency_ms": 10.0,
                        },
                    }
                ),
                "route_rejections": (
                    [{"reason": "backend_unavailable"}]
                    if filtered
                    else []
                ),
            },
            {
                "runtime": "codex",
                "eligible": True,
                "reason": None,
                "selected": {
                    "node_id": "openclaw",
                    "backend_id": "codex-runtime",
                    "model_id": None,
                    "scope": "backend",
                    "health": {
                        "load_ratio_1m": 0.3,
                        "memory_available_ratio": 0.5,
                        "backend_latency_ms": 20.0,
                    },
                },
                "route_rejections": [],
            },
        ],
    }


def plan(*, filtered=False):
    return {
        "request": {
            "request_id": "REQ-1",
            "capability": "fast_utility",
        },
        "lane": "fast",
        "deterministic_lane": "fast",
        "candidates": [
            {"runtime": "codex"}
            if filtered
            else {"runtime": "hermes"},
            *([] if filtered else [{"runtime": "codex"}]),
        ],
        "infrastructure_routing": routing(filtered=filtered),
        "identity_continuity": True,
        "semantic_authority_changed": False,
    }


def result(*, filtered=False, success=True):
    return {
        "request_id": "REQ-1",
        "status": "success" if success else "failed",
        "output": "SECRET_OUTPUT",
        "lane": "fast",
        "runtime": "codex" if filtered else "hermes",
        "provider": "provider",
        "model": "model",
        "degraded": filtered,
        "fallback_used": filtered,
        "attempts": [
            {
                "runtime": "hermes",
                "status": "failed" if filtered else "success",
                "error": "backend_failed" if filtered else None,
                "detail": "SECRET_ATTEMPT_DETAIL",
            },
            *(
                [{"runtime": "codex", "status": "success"}]
                if filtered
                else []
            ),
        ],
        "infrastructure_routing": routing(filtered=filtered),
        "identity_continuity": True,
        "semantic_authority_changed": False,
        "error": None if success else "all_cognitive_runtimes_failed",
    }


@pytest.mark.asyncio
async def test_plan_event_never_records_prompt():
    db = FakeDB()
    observer = CognitiveObservability(db, FakeRuntimes(), studio())

    await observer.record_plan(
        {
            "request_id": "REQ-1",
            "capability": "fast_utility",
            "prompt": "SECRET_PROMPT",
        },
        plan(),
        elapsed_ms=12.5,
    )

    assert len(db.events) == 1
    encoded = json.dumps(db.events[0]["data"])
    assert "SECRET_PROMPT" not in encoded
    assert "prompt" not in db.events[0]["data"]
    assert db.events[0]["data"]["elapsed_ms"] == 12.5


@pytest.mark.asyncio
async def test_execute_event_never_records_output_or_attempt_detail():
    db = FakeDB()
    observer = CognitiveObservability(db, FakeRuntimes(), studio())

    await observer.record_execute(
        {
            "request_id": "REQ-1",
            "capability": "fast_utility",
            "prompt": "SECRET_PROMPT",
        },
        result(filtered=True),
        elapsed_ms=55.0,
    )

    encoded = json.dumps(db.events[0]["data"])
    assert "SECRET_PROMPT" not in encoded
    assert "SECRET_OUTPUT" not in encoded
    assert "SECRET_ATTEMPT_DETAIL" not in encoded
    assert db.events[0]["data"]["attempts"][0] == {
        "runtime": "hermes",
        "status": "failed",
        "error": "backend_failed",
    }


@pytest.mark.asyncio
async def test_ready_runtime_filtered_by_n7_is_review_signal_not_route_change():
    db = FakeDB()
    observer = CognitiveObservability(db, FakeRuntimes(), studio())

    await observer.record_plan(
        {
            "request_id": "REQ-1",
            "capability": "fast_utility",
        },
        plan(filtered=True),
        elapsed_ms=10,
    )

    suspects = db.events[0]["data"]["routing"][
        "suspected_false_rejections"
    ]
    assert suspects == [
        {
            "runtime": "hermes",
            "reason": "no_eligible_route",
            "route_rejections": ["backend_unavailable"],
        }
    ]


@pytest.mark.asyncio
async def test_report_aggregates_execution_soak_metrics():
    db = FakeDB()
    observer = CognitiveObservability(db, FakeRuntimes(), studio())

    await observer.record_plan(
        {"request_id": "REQ-1", "capability": "fast_utility"},
        plan(),
        elapsed_ms=10,
    )
    await observer.record_execute(
        {"request_id": "REQ-1", "capability": "fast_utility"},
        result(),
        elapsed_ms=100,
    )

    report = await observer.report(window_minutes=60)

    assert report["schema"] == COGNITIVE_OBSERVABILITY_SCHEMA
    assert report["state"] == "healthy"
    assert report["metrics"]["plan_count"] == 1
    assert report["metrics"]["execute_count"] == 1
    assert report["metrics"]["execute_success_percent"] == 100.0
    assert report["metrics"]["fallback_count"] == 0
    assert report["runtime"]["successes"] == {"hermes": 1}
    assert report["integrity"] == {
        "identity_continuity": True,
        "semantic_authority_unchanged": True,
    }
    assert report["privacy"] == {
        "prompt_recorded": False,
        "output_recorded": False,
    }



@pytest.mark.asyncio
async def test_report_aggregates_semantic_shadow_disagreement():
    db = FakeDB()
    observer = CognitiveObservability(
        db,
        FakeRuntimes(),
        studio(cognitive_semantic_routing_mode="shadow"),
    )
    shadow_plan = plan()
    shadow_plan["semantic_routing"] = {
        "mode": "shadow",
        "applied": False,
        "current_selected": "claude",
        "semantic_preference": "codex",
        "would_select": "codex",
        "changed": True,
        "ranked_order": ["codex", "claude", "hermes"],
        "rankings": [
            {"runtime": "codex", "score": 0.94, "profile_source": "builtin_runtime_profile"},
            {"runtime": "claude", "score": 0.84, "profile_source": "builtin_runtime_profile"},
        ],
    }

    await observer.record_plan(
        {"request_id": "REQ-SEMANTIC", "capability": "coding"},
        shadow_plan,
        elapsed_ms=8,
    )

    report = await observer.report(window_minutes=60)

    assert report["semantic_routing_mode"] == "shadow"
    assert report["metrics"]["semantic_shadow_plan_count"] == 1
    assert report["metrics"]["semantic_shadow_disagreement_count"] == 1
    assert report["metrics"]["semantic_shadow_disagreement_percent"] == 100.0
    assert report["semantic_routing"]["preferred_runtimes"] == {"codex": 1}

@pytest.mark.asyncio
async def test_report_flags_suspected_false_rejection_for_review():
    db = FakeDB()
    observer = CognitiveObservability(db, FakeRuntimes(), studio())

    await observer.record_execute(
        {"request_id": "REQ-1", "capability": "fast_utility"},
        result(filtered=True),
        elapsed_ms=120,
    )

    report = await observer.report()

    assert report["state"] == "degraded"
    assert "suspected_false_rejection_review" in report["state_reasons"]
    assert report["metrics"]["suspected_false_rejection_count"] == 1
    assert report["metrics"]["fallback_count"] == 1
    assert report["rejections"]["reasons"] == {
        "backend_unavailable": 1
    }


@pytest.mark.asyncio
async def test_observer_failure_is_fail_open():
    observer = CognitiveObservability(
        FakeDB(fail_add=True),
        FakeRuntimes(),
        studio(),
    )

    await observer.record_plan(
        {"request_id": "REQ-1", "capability": "fast_utility"},
        plan(),
        elapsed_ms=1,
    )

    assert observer.last_error == "RuntimeError: ledger unavailable"


@pytest.mark.asyncio
async def test_report_uses_24_hour_window_by_default():
    observer = CognitiveObservability(
        FakeDB(),
        FakeRuntimes(),
        SimpleNamespace(
            cognitive_node_routing_mode="enforce",
            cognitive_soak_min_executions=1,
            cognitive_soak_failure_warn_percent=5.0,
            cognitive_soak_fallback_warn_percent=25.0,
        ),
    )

    report = await observer.report()

    assert report["window_minutes"] == 1440

def test_soak_default_window_is_24_hours():
    assert StudioConfig().cognitive_soak_window_minutes == 1440


def test_semantic_routing_defaults_to_shadow_and_validates_configuration():
    cfg = StudioConfig()
    assert cfg.cognitive_semantic_routing_mode == "shadow"
    assert cfg.cognitive_semantic_canary_min_margin == pytest.approx(0.03)
    assert cfg.cognitive_runtime_capability_overrides == {}

    canary = StudioConfig(
        cognitive_semantic_routing_mode="canary",
        cognitive_semantic_canary_min_margin=0.05,
    )
    assert canary.cognitive_semantic_routing_mode == "canary"
    assert canary.cognitive_semantic_canary_min_margin == pytest.approx(0.05)

    with pytest.raises(ValueError, match="cognitive_semantic_routing_mode"):
        StudioConfig(cognitive_semantic_routing_mode="enforce")
    with pytest.raises(
        ValueError,
        match="cognitive_semantic_canary_min_margin",
    ):
        StudioConfig(cognitive_semantic_canary_min_margin=1.01)
    with pytest.raises(ValueError, match="cognitive_runtime_capability_overrides"):
        StudioConfig(
            cognitive_runtime_capability_overrides={"codex": "invalid"}
        )

def test_soak_settings_validate_bounds():
    with pytest.raises(ValueError, match="cognitive_soak_window_minutes"):
        StudioConfig(cognitive_soak_window_minutes=0)
    with pytest.raises(ValueError, match="cognitive_soak_min_executions"):
        StudioConfig(cognitive_soak_min_executions=0)
    with pytest.raises(ValueError, match="cognitive_soak_failure_warn_percent"):
        StudioConfig(cognitive_soak_failure_warn_percent=101)


def test_main_exposes_observability_without_touching_router_semantics():
    source = (
        Path(__file__).resolve().parents[1] / "mcp_studio" / "main.py"
    ).read_text(encoding="utf-8")
    assert '@app.get("/api/cognition/observability")' in source
    assert "cognitive_observability.record_plan(" in source
    assert "cognitive_observability.record_execute(" in source
