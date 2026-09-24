from __future__ import annotations

from pathlib import Path

import pytest

from mcp_studio.db import Database
from mcp_studio.graft import GraftManager
from mcp_studio.integrations import (
    INTEGRATION_MANIFEST_SCHEMA,
    GraftIntegrationAdapter,
    JevIntegrationAdapter,
    IntegrationError,
    IntegrationManager,
    IntegrationManifest,
    build_integration_manager,
)
from mcp_studio.settings import Settings, StudioConfig


def _manifest(integration_id: str = "demo") -> IntegrationManifest:
    return IntegrationManifest.from_mapping(
        {
            "schema": INTEGRATION_MANIFEST_SCHEMA,
            "id": integration_id,
            "name": "Demo",
            "capabilities": ["search"],
            "runtime": {"type": "local"},
            "tools": ["demo_search"],
            "permissions": {"demo_search": "read"},
            "health": {"type": "adapter_probe"},
            "routing": {"preferred_lanes": ["hermes"]},
        }
    )


def _jev_studio(**overrides) -> StudioConfig:
    studio = StudioConfig(
        jev_enabled=True,
        jev_mode="shadow",
        jev_api_url="https://api.typesafe.ai/v1/systemone",
        jev_api_key_env="TYPESAFE_API_KEY",
        jev_model="jev-latest",
        jev_timeout_seconds=2.0,
    )
    for key, value in overrides.items():
        setattr(studio, key, value)
    return studio


class PassingAdapter:
    def __init__(self, integration_id: str = "demo") -> None:
        self.manifest = _manifest(integration_id)

    async def validate(self):
        return {"ok": True, "runtime": "present"}

    async def certify(self):
        return {"ok": True, "contract": "pass"}

    async def status(self):
        return {"ok": True}


class FailingValidationAdapter(PassingAdapter):
    async def validate(self):
        return {"ok": False, "reason": "runtime_missing"}


class FailingCertificationAdapter(PassingAdapter):
    async def certify(self):
        return {"ok": False, "reason": "contract_mismatch"}


def test_manifest_requires_permission_for_every_tool():
    with pytest.raises(IntegrationError, match="every declared tool"):
        IntegrationManifest.from_mapping(
            {
                "id": "bad",
                "name": "Bad",
                "capabilities": [],
                "runtime": {"type": "local"},
                "tools": ["read"],
                "permissions": {},
                "health": {"type": "adapter_probe"},
                "routing": {},
            }
        )


@pytest.mark.asyncio
async def test_manager_collapses_integration_into_single_ready_lifecycle():
    manager = IntegrationManager()
    manager.register(PassingAdapter(), source="unit-test")

    result = await manager.reconcile("demo")

    assert result["stage"] == "ready"
    assert result["blocked"] is False
    assert result["validation"]["ok"] is True
    assert result["certification"]["ok"] is True

    snapshot = await manager.snapshot()
    assert snapshot["integration_count"] == 1
    assert snapshot["ready_count"] == 1
    assert snapshot["blocked_count"] == 0


@pytest.mark.asyncio
async def test_manager_reports_exact_validation_failure_stage():
    manager = IntegrationManager()
    manager.register(FailingValidationAdapter(), source="unit-test")

    result = await manager.reconcile("demo")

    assert result["stage"] == "validating"
    assert result["blocked"] is True
    assert result["failed_stage"] == "validating"
    assert result["error"] == "runtime_missing"


@pytest.mark.asyncio
async def test_manager_reports_exact_certification_failure_stage():
    manager = IntegrationManager()
    manager.register(FailingCertificationAdapter(), source="unit-test")

    result = await manager.reconcile("demo")

    assert result["stage"] == "certifying"
    assert result["blocked"] is True
    assert result["failed_stage"] == "certifying"
    assert result["error"] == "contract_mismatch"


def test_manager_rejects_duplicate_integration_ids():
    manager = IntegrationManager()
    manager.register(PassingAdapter())
    with pytest.raises(IntegrationError, match="duplicate integration"):
        manager.register(PassingAdapter())


def test_manager_discovers_external_integration_entrypoint_without_core_edit(monkeypatch):
    manager = IntegrationManager()

    class FakeEntryPoint:
        name = "demo-plugin"

        def load(self):
            return PassingAdapter(integration_id="external-demo")

    class FakeEntryPoints:
        def select(self, *, group):
            assert group == "hirda.integrations"
            return [FakeEntryPoint()]

    monkeypatch.setattr(
        "mcp_studio.integrations.metadata.entry_points",
        lambda: FakeEntryPoints(),
    )

    result = manager.discover_entry_points()
    assert result == {"loaded": ["external-demo"], "errors": []}
    assert manager.get("external-demo").source == "entrypoint:demo-plugin"


@pytest.mark.asyncio
async def test_graft_is_migrated_into_common_lifecycle_without_new_authority(tmp_path: Path):
    cli = tmp_path / "graft"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            graft_enabled=True,
            graft_cli_path=str(cli),
        ),
        servers=[],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    graft = GraftManager(settings, db)
    adapter = GraftIntegrationAdapter(graft)
    manager = IntegrationManager()
    manager.register(adapter, source="builtin:graft")

    result = await manager.reconcile("graft")

    assert result["stage"] == "ready"
    assert result["manifest"]["metadata"]["write_authority"] is False
    assert result["manifest"]["metadata"]["destructive_authority"] is False
    assert set(result["manifest"]["tools"])
    assert set(result["manifest"]["permissions"].values()) == {"read"}
    assert result["certification"]["authority"]["write"] is False
    assert result["certification"]["authority"]["destructive"] is False


@pytest.mark.asyncio
async def test_graft_validation_blocks_cleanly_when_disabled(tmp_path: Path):
    settings = Settings(
        studio=StudioConfig(
            database=str(tmp_path / "studio.sqlite3"),
            graft_enabled=False,
            graft_cli_path=str(tmp_path / "missing"),
        ),
        servers=[],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(settings.studio.database)
    manager = IntegrationManager()
    manager.register(GraftIntegrationAdapter(GraftManager(settings, db)))

    result = await manager.reconcile("graft")

    assert result["stage"] == "validating"
    assert result["failed_stage"] == "validating"
    assert result["error"] == "graft_disabled"


@pytest.mark.asyncio
async def test_jev_is_migrated_without_tools_or_runtime_authority(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "configured-for-test")
    manager = IntegrationManager()
    manager.register(JevIntegrationAdapter(_jev_studio()), source="builtin:jev")

    result = await manager.reconcile("jev")

    assert result["stage"] == "ready"
    assert result["blocked"] is False
    assert result["manifest"]["tools"] == []
    assert result["manifest"]["permissions"] == {}
    assert result["manifest"]["runtime"]["authority"] == "advisory-only"
    assert result["certification"]["teacher_only"] is True
    assert result["certification"]["authority"] == {
        "grant": False,
        "block": False,
        "execute": False,
        "create_actions": False,
        "runtime_policy": "hirda-reflex",
    }
    assert result["status"]["credential_configured"] is True
    assert result["status"]["network_probe_performed"] is False


@pytest.mark.asyncio
async def test_jev_missing_key_stops_at_validation_without_secret_value(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    manager = IntegrationManager()
    manager.register(JevIntegrationAdapter(_jev_studio()), source="builtin:jev")

    result = await manager.reconcile("jev")

    assert result["stage"] == "validating"
    assert result["blocked"] is True
    assert result["failed_stage"] == "validating"
    assert result["error"] == "missing_api_key"
    assert result["validation"]["api_key_env"] == "TYPESAFE_API_KEY"
    assert result["validation"]["credential_configured"] is False


@pytest.mark.asyncio
async def test_jev_rejects_insecure_non_loopback_http_endpoint(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "configured-for-test")
    manager = IntegrationManager()
    manager.register(
        JevIntegrationAdapter(
            _jev_studio(jev_api_url="http://example.test/v1/systemone")
        )
    )

    result = await manager.reconcile("jev")

    assert result["stage"] == "validating"
    assert result["error"] == "jev_invalid_api_url"


def test_builder_registers_jev_as_builtin_without_main_wiring(tmp_path: Path):
    settings = Settings(
        studio=_jev_studio(),
        servers=[],
        tunnels=[],
        config_path=tmp_path / "config.yaml",
    )
    db = Database(str(tmp_path / "studio.sqlite3"))
    graft = GraftManager(settings, db)

    manager = build_integration_manager(graft, settings.studio)

    registration = manager.get("jev")
    assert registration.source == "builtin:jev"
    assert registration.adapter.manifest.integration_id == "jev"
