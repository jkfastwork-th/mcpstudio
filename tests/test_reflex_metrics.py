import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from mcp_studio.reflex_metrics import ReflexMetrics


def studio(path: Path, **overrides):
    values = {
        "reflex_dataset_path": str(path),
        "reflex_dataset_enabled": True,
        "reflex_enabled": True,
        "reflex_mode": "enforce",
        "reflex_review_risk_threshold": 0.72,
        "reflex_deny_risk_threshold": 0.92,
        "reflex_min_confidence": 0.88,
        "jev_enabled": True,
        "jev_mode": "shadow",
        "jev_model": "jev-latest",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def write_rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def decision_row(stamp, *, reflex_id, action, risk, lane, agreement=True, teacher=True, signal=None):
    return {
        "schema": "hirda-reflex-dataset-v1",
        "recorded_at": stamp,
        "kind": "decision",
        "reflex_id": reflex_id,
        "workspace_key": "nova-oracle",
        "features": {
            "tool": "execute_shell_command",
            "permission_class": "execute",
            "has_sensitive_arg_key": False,
        },
        "reflex": {
            "action": action,
            "risk": risk,
            "confidence": 0.94,
            "compute_lane": lane,
            "blocked": action != "allow",
            "signals": [signal] if signal else [],
        },
        "teacher": {
            "provider": "typesafe_jev",
            "enabled": teacher,
            "evaluated": teacher,
            "action": action if agreement else "allow",
            "confidence": 0.91 if teacher else None,
            "model": "jev-latest" if teacher else None,
            "error": None,
            "agreement": agreement if teacher else None,
        },
        "timings": {
            "policy_ms": 0.2,
            "reflex_ms": 1.8,
            "teacher_ms": 420.0 if teacher else 0.01,
        },
    }


def test_metrics_aggregate_reflex_teacher_latency_and_signals(tmp_path):
    now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    dataset = tmp_path / "reflex.jsonl"
    rows = [
        decision_row(
            (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z"),
            reflex_id="r1",
            action="allow",
            risk=0.34,
            lane="fast",
        ),
        decision_row(
            (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            reflex_id="r2",
            action="review",
            risk=0.84,
            lane="deep",
            agreement=False,
            signal="privilege_escalation",
        ),
        {
            "schema": "hirda-reflex-dataset-v1",
            "recorded_at": (now - timedelta(minutes=9)).isoformat().replace("+00:00", "Z"),
            "kind": "outcome",
            "reflex_id": "r1",
            "http_success": True,
            "upstream_status": 200,
        },
    ]
    write_rows(dataset, rows)

    payload = ReflexMetrics(studio(dataset)).snapshot(window="24h", now=now)

    assert payload["summary"]["decisions"] == 2
    assert payload["summary"]["allow"] == 1
    assert payload["summary"]["review"] == 1
    assert payload["summary"]["blocked"] == 1
    assert payload["teacher"]["evaluated"] == 2
    assert payload["teacher"]["agreement"] == 1
    assert payload["teacher"]["disagreement"] == 1
    assert payload["teacher"]["agreement_rate"] == 50.0
    assert payload["latency"]["reflex"]["median_ms"] == 1.8
    assert payload["latency"]["teacher"]["median_ms"] == 420.0
    assert payload["outcomes"]["http_success_rate"] == 100.0
    assert payload["distributions"]["compute_lanes"] == {"fast": 1, "deep": 1}
    assert payload["distributions"]["signals"] == [{"signal": "privilege_escalation", "count": 1}]
    assert payload["privacy"]["raw_arguments_exposed"] is False
    assert payload["authority"]["jev_teacher_only"] is True


def test_metrics_respect_window_and_do_not_expose_full_dataset_path(tmp_path):
    now = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    dataset = tmp_path / "private" / "reflex-decisions.jsonl"
    write_rows(
        dataset,
        [
            decision_row(
                (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                reflex_id="fresh",
                action="allow",
                risk=0.2,
                lane="fast",
                teacher=False,
            ),
            decision_row(
                (now - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
                reflex_id="old",
                action="deny",
                risk=0.99,
                lane="deep",
            ),
        ],
    )

    payload = ReflexMetrics(studio(dataset, jev_enabled=False)).snapshot(window="1h", now=now)

    assert payload["summary"]["decisions"] == 1
    assert payload["summary"]["allow"] == 1
    assert payload["summary"]["deny"] == 0
    assert payload["dataset"]["records"] == 2
    assert payload["dataset"]["file"] == "reflex-decisions.jsonl"
    assert str(dataset.parent) not in json.dumps(payload)
    assert payload["teacher"]["availability_rate"] is None
    assert payload["config"]["teacher_enabled"] is False


def test_metrics_empty_dataset_is_stable(tmp_path):
    dataset = tmp_path / "missing.jsonl"
    payload = ReflexMetrics(studio(dataset)).snapshot(window="7d")

    assert payload["state"] == "empty"
    assert payload["summary"]["decisions"] == 0
    assert payload["teacher"]["agreement_rate"] is None
    assert payload["latency"]["reflex"]["median_ms"] is None
    assert payload["recent"] == []
