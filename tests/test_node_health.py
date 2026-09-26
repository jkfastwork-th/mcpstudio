from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.node_health import (
    NODE_HEALTH_SCHEMA,
    NodeHealthMonitor,
    evaluate_health_availability,
    mark_node_unavailable,
    normalize_health_observation,
    probe_node_health,
)
from mcp_studio.node_observations import ingest_node_observations
from mcp_studio.node_registry import NodeRegistry
from mcp_studio.settings import StudioConfig


def studio(tmp_path: Path, *, execute: bool = False) -> StudioConfig:
    return StudioConfig(
        machine_local_id="openclaw",
        machine_registry=[
            {
                "id": "openclaw",
                "name": "openclaw",
                "enabled": True,
                "local": True,
                "os": "linux",
                "address": "127.0.0.1",
                "capabilities": ["filesystem", "process"],
                "providers": {"desktop_commander": {"mode": "local"}},
                "policy": {
                    "read": True,
                    "write": True,
                    "execute": execute,
                    "destructive": False,
                    "fail_closed_unknown": True,
                },
            }
        ],
    )


def backend_observation() -> dict:
    return {
        "schema": "hirda-node-backends-v1",
        "observed_at": "2026-09-26T04:59:59+00:00",
        "backends": [
            {
                "id": "runtime-x",
                "kind": "inference",
                "available": True,
                "binary": "/usr/bin/runtime-x",
                "version": "runtime-x 1",
                "capabilities": [],
            }
        ],
    }


def health_observation(*, backend_id: str = "runtime-x") -> dict:
    return {
        "schema": NODE_HEALTH_SCHEMA,
        "observed_at": "2026-09-26T05:00:00+00:00",
        "node": {
            "available": True,
            "state": "ready",
            "load": {
                "logical_cpu_count": 8,
                "load_1m": 2.0,
                "load_5m": 1.5,
                "load_15m": 1.0,
                "load_ratio_1m": 0.25,
                "memory_total_bytes": 16_000,
                "memory_available_bytes": 8_000,
                "memory_available_ratio": 0.5,
            },
            "storage": {
                "total_bytes": 100_000,
                "free_bytes": 60_000,
                "free_ratio": 0.6,
            },
        },
        "backends": [
            {
                "backend_id": backend_id,
                "available": True,
                "state": "ready",
                "latency_ms": 12.5,
            }
        ],
    }


def test_probe_reports_node_load_and_backend_health(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.node_health as health

    monkeypatch.setattr(health.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(health.os, "getloadavg", lambda: (2.0, 1.5, 1.0))
    monkeypatch.setattr(health, "_memory_snapshot", lambda: (16_000, 8_000))
    monkeypatch.setattr(
        health.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100_000, used=40_000, free=60_000),
    )
    monkeypatch.setattr(
        health,
        "_resolve_binary",
        lambda command: f"/usr/bin/{command[0]}",
    )
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ticks = iter([10.0, 10.0125])
    monkeypatch.setattr(health.time, "monotonic", lambda: next(ticks))

    result = probe_node_health(
        [
            {
                "id": "runtime-x",
                "command": ["runtime-x"],
                "health_probe": {
                    "command": ["runtime-x", "health"],
                    "timeout_seconds": 2,
                },
            }
        ],
        storage_path=tmp_path,
        observed_at="2026-09-26T05:00:00+00:00",
    )

    assert result["schema"] == NODE_HEALTH_SCHEMA
    assert result["node"]["load"]["load_ratio_1m"] == 0.25
    assert result["node"]["load"]["memory_available_ratio"] == 0.5
    assert result["node"]["storage"]["free_ratio"] == 0.6
    assert result["backends"] == [
        {
            "backend_id": "runtime-x",
            "available": True,
            "state": "ready",
            "latency_ms": 12.5,
        }
    ]


def test_backend_health_fails_closed_when_probe_fails(monkeypatch) -> None:
    import mcp_studio.node_health as health

    monkeypatch.setattr(
        health,
        "_resolve_binary",
        lambda command: f"/usr/bin/{command[0]}",
    )
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad"),
    )

    result = probe_node_health(
        [
            {
                "id": "runtime-x",
                "command": ["runtime-x"],
                "health_probe": {"command": ["runtime-x", "health"]},
            }
        ]
    )

    assert result["backends"][0]["available"] is False
    assert result["backends"][0]["state"] == "unhealthy"


def test_health_normalizer_sanitizes_untrusted_metrics() -> None:
    raw = health_observation()
    raw["node"]["load"]["logical_cpu_count"] = -1
    raw["node"]["load"]["memory_available_ratio"] = 3
    raw["node"]["storage"]["free_ratio"] = -0.2
    raw["backends"][0]["latency_ms"] = "bad"

    normalized = normalize_health_observation(raw)

    assert normalized is not None
    assert normalized["node"]["load"]["logical_cpu_count"] is None
    assert normalized["node"]["load"]["memory_available_ratio"] is None
    assert normalized["node"]["storage"]["free_ratio"] is None
    assert normalized["backends"][0]["latency_ms"] is None


def test_freshness_turns_stale_health_unavailable() -> None:
    fresh = evaluate_health_availability(
        health_observation(),
        now=datetime(2026, 9, 26, 5, 0, 20, tzinfo=timezone.utc),
        stale_after_seconds=45,
    )
    stale = evaluate_health_availability(
        health_observation(),
        now=datetime(2026, 9, 26, 5, 1, 0, tzinfo=timezone.utc),
        stale_after_seconds=45,
    )

    assert fresh["fresh"] is True
    assert fresh["available"] is True
    assert fresh["backends"][0]["available"] is True
    assert stale["stale"] is True
    assert stale["state"] == "stale"
    assert stale["available"] is False
    assert stale["backends"][0]["available"] is False


def test_ingestion_stamps_node_and_filters_unknown_backend(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    forged = health_observation()
    forged["backends"].append(
        {
            "backend_id": "forged-backend",
            "available": True,
            "state": "ready",
            "latency_ms": 1,
        }
    )

    accepted = ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "health.availability": forged,
        },
        source="authenticated-device",
    )

    stored = accepted["health.availability"]
    assert stored["node_id"] == "openclaw"
    assert [item["backend_id"] for item in stored["backends"]] == ["runtime-x"]


def test_health_observation_does_not_expand_authority(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path, execute=False), base_dir=tmp_path)
    node = registry.get_node("openclaw")
    policy_before = dict(node.policy)
    providers_before = dict(node.providers)
    capabilities_before = tuple(node.capabilities)

    ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "health.availability": health_observation(),
        },
        source="authenticated-device",
    )

    assert node.policy == policy_before
    assert node.providers == providers_before
    assert node.capabilities == capabilities_before
    denied = registry.authorize_session(
        {"metadata": {"machine_id": "openclaw"}},
        "execute",
    )
    assert denied.allowed is False
    assert denied.code == "MACHINE_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_local_health_monitor_refreshes_immediately(monkeypatch, tmp_path: Path) -> None:
    import mcp_studio.node_health as health

    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    monkeypatch.setattr(
        health,
        "probe_node_health",
        lambda raw_specs, storage_path=None: health_observation(),
    )

    monitor = NodeHealthMonitor(
        registry,
        [],
        storage_path=tmp_path,
        interval_seconds=5,
    )
    await monitor.start()
    try:
        stored = registry.lifecycle("openclaw")["enrichment"]["facets"][
            "health.availability"
        ]["data"]
        assert stored["node_id"] == "openclaw"
        assert stored["node"]["available"] is True
    finally:
        await monitor.stop()


def test_disconnect_marks_node_immediately_unavailable(tmp_path: Path) -> None:
    registry = NodeRegistry(studio(tmp_path), base_dir=tmp_path)
    ingest_node_observations(
        registry,
        "openclaw",
        {
            "runtime.backends": backend_observation(),
            "health.availability": health_observation(),
        },
        source="device_runtime",
    )

    offline = mark_node_unavailable(
        registry,
        "openclaw",
        source="device_broker",
    )

    assert offline is not None
    assert offline["node"]["available"] is False
    assert offline["node"]["state"] == "offline"
    snapshot = registry.availability("openclaw", stale_after_seconds=45)
    assert snapshot["node_id"] == "openclaw"
    assert snapshot["available"] is False
    assert snapshot["state"] == "offline"


def test_shared_runtime_refreshes_health_on_heartbeat() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "mcp_studio" / "device_runtime.py"
    ).read_text(encoding="utf-8")
    assert '"health.availability": self.node_health_observation()' in source
    assert '"type": "heartbeat"' in source


def test_broker_disconnect_marks_dynamic_node_offline() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "mcp_studio" / "main.py"
    ).read_text(encoding="utf-8")
    assert "await device_broker.unregister_connection" in source
    assert "mark_node_unavailable(" in source
    assert 'source="device_broker"' in source


def test_n6_core_contains_no_product_or_provider_hardcoding() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (
        (root / "mcp_studio" / "node_health.py").read_text(encoding="utf-8")
        + (root / "mcp_studio" / "node_observations.py").read_text(encoding="utf-8")
    ).casefold()
    for forbidden in ("nova", "ollama", "openvino", "mlx"):
        assert forbidden not in source
