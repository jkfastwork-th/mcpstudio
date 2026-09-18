from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_studio.db import Database
from mcp_studio.observability import ObservabilityManager


@pytest.mark.asyncio
async def test_schema_v3_contains_observability_tables(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    schema = await db.schema_status()
    assert schema["current_version"] == 7
    assert schema["expected_version"] == 7
    assert schema["integrity"] == "ok"


@pytest.mark.asyncio
async def test_mcp_request_metrics_and_slo_report(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.add_event(
        "server.status", "Serena: unknown -> healthy", server_id="serena-8001",
        data={"from": "unknown", "to": "healthy"},
    )
    await db.record_observability_sample({
        "upstream_ok": True, "upstream_status": "healthy", "worker_busy": 1,
        "worker_total": 4, "queue_depth": 0, "active_work": 1, "open_alerts": 0,
    })
    await db.record_observability_sample({
        "upstream_ok": True, "upstream_status": "healthy", "worker_busy": 2,
        "worker_total": 4, "queue_depth": 0, "active_work": 1, "open_alerts": 0,
    })
    await db.record_mcp_request(
        server_id="serena-8001", http_method="POST", rpc_method="tools/list",
        status_code=200, latency_ms=12.5, client_class="openai-chatgpt",
    )
    report = await db.observability_report(
        window_minutes=60, availability_target=99.9, mcp_success_target=99.5,
        queue_p95_limit_ms=5000, worker_saturation_warn_percent=85,
        reconnect_rate_warn_per_100=5, oauth_refresh_failures_max=0, orphan_events_max=0,
    )
    assert report["metrics"]["availability_percent"] == 100.0
    assert report["metrics"]["mcp_success_percent"] == 100.0
    assert report["metrics"]["worker_saturation_current_percent"] == 50.0
    assert report["objectives"]["availability"]["state"] == "healthy"
    assert report["objectives"]["mcp_success"]["state"] == "healthy"


@pytest.mark.asyncio
async def test_observability_manager_samples_and_opens_threshold_alert(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init(); await db.ensure_workers(1, "serena-8001")
    await db.add_event(
        "server.status", "Serena: unknown -> down", severity="warning", server_id="serena-8001",
        data={"from": "unknown", "to": "down"},
    )
    settings = SimpleNamespace(
        config_path=tmp_path / "config.yaml",
        studio=SimpleNamespace(
            observability_enabled=True,
            observability_interval_seconds=60.0,
            observability_window_minutes=60,
            observability_retention_days=30,
            observability_alerts_enabled=True,
            slo_availability_target_percent=99.9,
            slo_mcp_success_target_percent=99.5,
            slo_queue_p95_limit_ms=5000.0,
            slo_worker_saturation_warn_percent=85.0,
            slo_reconnect_rate_warn_per_100_requests=5.0,
            slo_oauth_refresh_failures_max=0,
            slo_orphan_events_max=0,
        ),
    )
    obs = ObservabilityManager(settings, db)
    await obs.run_once()
    await obs.run_once()  # two samples are enough to evaluate availability
    report = await obs.report()
    assert report["objectives"]["availability"]["state"] == "down"
    alerts = await db.list_alerts(status="open")
    assert any(a["dedupe_key"] == "slo:availability" for a in alerts)


@pytest.mark.asyncio
async def test_oauth_refresh_failure_counts_from_safe_event(tmp_path: Path):
    db = Database(str(tmp_path / "studio.sqlite3"))
    await db.init()
    await db.add_event(
        "oauth.token.failed", "OAuth token request failed using refresh_token",
        severity="warning", data={"grant_type": "refresh_token", "error": "invalid_grant"},
    )
    report = await db.observability_report(
        window_minutes=60, availability_target=99.9, mcp_success_target=99.5,
        queue_p95_limit_ms=5000, worker_saturation_warn_percent=85,
        reconnect_rate_warn_per_100=5, oauth_refresh_failures_max=0, orphan_events_max=0,
    )
    assert report["metrics"]["oauth_refresh_failures"] == 1
    assert report["objectives"]["oauth_refresh_failures"]["state"] == "down"
