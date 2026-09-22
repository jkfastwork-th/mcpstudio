from __future__ import annotations

from dataclasses import dataclass

import pytest

from mcp_studio.action_registry import (
    ACTION_PROVIDER_REGISTRY_SCHEMA,
    ActionProviderRegistry,
    ActionProviderRegistryError,
)


def _payload(provider_id: str = "demo", domain: str = "demo") -> dict:
    return {
        "schema": "hirda-action-envelope-v1",
        "request_id": f"{provider_id}:request:1",
        "provider": provider_id,
        "domain": domain,
        "goal": "Choose a bounded test action.",
        "state": {"ready": True},
        "actions": [
            {
                "action_id": f"{provider_id}:observe",
                "operation": "observe",
                "title": "Observe",
                "description": "Read state without side effects.",
                "permission_class": "read",
                "arguments": {},
                "constraints": {},
                "risk": {
                    "consequential": False,
                    "reversible": True,
                    "external_side_effect": False,
                },
            }
        ],
        "metadata": {"executor_attached": False},
    }


@dataclass
class SyncProvider:
    provider_id: str = "demo"
    domain: str = "demo"
    title: str = "Demo"
    description: str = "Synchronous provider"
    executor_attached: bool = False

    def build_envelope(self):
        return _payload(self.provider_id, self.domain)


@dataclass
class AsyncProvider:
    provider_id: str = "async-demo"
    domain: str = "browser"
    title: str = "Async Demo"
    description: str = "Asynchronous provider"
    executor_attached: bool = False

    async def build_envelope(self):
        return _payload(self.provider_id, self.domain)


def test_registry_rejects_duplicate_provider_ids():
    registry = ActionProviderRegistry()
    registry.register(SyncProvider())
    with pytest.raises(ActionProviderRegistryError, match="duplicate action provider"):
        registry.register(SyncProvider())


def test_registry_rejects_executor_attachment_in_jaa2():
    registry = ActionProviderRegistry()
    provider = SyncProvider()
    provider.executor_attached = True
    with pytest.raises(ActionProviderRegistryError, match="envelope-only"):
        registry.register(provider)


@pytest.mark.asyncio
async def test_registry_builds_and_lists_sync_provider():
    registry = ActionProviderRegistry()
    registration = registry.register(SyncProvider(), source="unit-test")
    assert registration.source == "unit-test"

    envelope = await registry.build_envelope("demo")
    assert envelope.provider == "demo"
    assert envelope.domain == "demo"

    snapshot = await registry.snapshot()
    assert snapshot["schema"] == ACTION_PROVIDER_REGISTRY_SCHEMA
    assert snapshot["provider_count"] == 1
    assert snapshot["providers"][0]["provider_id"] == "demo"
    assert snapshot["providers"][0]["source"] == "unit-test"
    assert snapshot["providers"][0]["status"] == "ready"
    assert snapshot["providers"][0]["action_count"] == 1
    assert snapshot["allow_executors"] is False


@pytest.mark.asyncio
async def test_registry_accepts_async_state_provider_without_contract_change():
    registry = ActionProviderRegistry()
    registry.register(AsyncProvider(), source="async-test")

    detail = await registry.provider_detail("async-demo")
    assert detail["provider_id"] == "async-demo"
    assert detail["domain"] == "browser"
    assert detail["envelope"]["provider"] == "async-demo"
    assert detail["envelope"]["state"]["ready"] is True


@pytest.mark.asyncio
async def test_registry_fails_closed_when_envelope_identity_changes():
    class BadProvider(SyncProvider):
        provider_id = "declared"

        def __init__(self):
            super().__init__(provider_id="declared")

        def build_envelope(self):
            return _payload("other", self.domain)

    registry = ActionProviderRegistry()
    registry.register(BadProvider())
    with pytest.raises(ActionProviderRegistryError, match="changed envelope.provider"):
        await registry.build_envelope("declared")

    snapshot = await registry.snapshot()
    assert snapshot["providers"][0]["status"] == "invalid"
    assert snapshot["providers"][0]["action_count"] is None


def test_registry_entrypoint_discovery_registers_provider_without_core_edit(monkeypatch):
    registry = ActionProviderRegistry()

    class FakeEntryPoint:
        name = "demo-plugin"

        def load(self):
            return SyncProvider(provider_id="plugin-demo")

    class FakeEntryPoints:
        def select(self, *, group):
            assert group == "hirda.action_providers"
            return [FakeEntryPoint()]

    monkeypatch.setattr(
        "mcp_studio.action_registry.metadata.entry_points",
        lambda: FakeEntryPoints(),
    )

    result = registry.discover_entry_points()
    assert result == {"loaded": ["plugin-demo"], "errors": []}
    registration = registry.get("plugin-demo")
    assert registration.source == "entrypoint:demo-plugin"


def test_registry_entrypoint_failure_is_recorded_not_loaded(monkeypatch):
    registry = ActionProviderRegistry()

    class BrokenEntryPoint:
        name = "broken"

        def load(self):
            raise RuntimeError("boom")

    class FakeEntryPoints:
        def select(self, *, group):
            return [BrokenEntryPoint()]

    monkeypatch.setattr(
        "mcp_studio.action_registry.metadata.entry_points",
        lambda: FakeEntryPoints(),
    )

    result = registry.discover_entry_points(strict=False)
    assert result["loaded"] == []
    assert result["errors"] == [{"entry_point": "broken", "error": "RuntimeError"}]
