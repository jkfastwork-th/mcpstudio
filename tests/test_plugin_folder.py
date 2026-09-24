from __future__ import annotations

from pathlib import Path

import pytest

from mcp_studio.integrations import IntegrationManager
from mcp_studio.plugin_folder import PluginFolderDiscovery, inspect_plugin_folder


def _plugin(root: Path, plugin_id: str = "demo", *, entry: str = "adapter.py",
            permission: str = "read", extra: str = "",
            adapter_source: str = "class DemoIntegration:\n    pass\n") -> Path:
    path = root / plugin_id
    path.mkdir(parents=True)
    (path / "adapter.py").write_text(adapter_source, encoding="utf-8")
    (path / "hirda-plugin.yaml").write_text(
        f"""schema: hirda-plugin-v1
id: {plugin_id}
name: Demo Plugin
version: 1.0.0
adapter:
  runtime: python
  entry: {entry}
  class: DemoIntegration
capabilities: [demo_read]
tools: [demo_read]
permissions:
  demo_read: {permission}
health:
  type: adapter_probe
routing:
  preferred_lanes: [hermes]
{extra}""",
        encoding="utf-8",
    )
    return path


def test_valid_manifest_stays_quarantined(tmp_path: Path):
    candidate = inspect_plugin_folder(_plugin(tmp_path))
    assert candidate.preflight_ok is True
    assert candidate.quarantine_reason == "trust_not_established"
    item = candidate.as_registry_item()
    assert item["stage"] == "quarantined"
    assert item["status"]["code_loaded"] is False


def test_discovery_does_not_import_adapter(tmp_path: Path):
    marker = tmp_path / "imported"
    _plugin(
        tmp_path,
        adapter_source=(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('yes')\n"
            "class DemoIntegration:\n    pass\n"
        ),
    )
    snapshot = PluginFolderDiscovery(tmp_path).scan()
    assert snapshot["preflight_ok_count"] == 1
    assert snapshot["code_loaded_count"] == 0
    assert not marker.exists()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("escape", "adapter_path_escape"),
        ("duplicate", "duplicate_integration_id"),
        ("secret", "inline_secret_not_allowed"),
        ("destructive", "destructive_permission_requires_approval"),
    ],
)
def test_security_preflight(tmp_path: Path, kind: str, expected: str):
    if kind == "escape":
        candidate = inspect_plugin_folder(_plugin(tmp_path, entry="../outside.py"))
    elif kind == "duplicate":
        candidate = inspect_plugin_folder(_plugin(tmp_path, "jev"), reserved_ids={"jev"})
    elif kind == "secret":
        candidate = inspect_plugin_folder(
            _plugin(tmp_path, extra="metadata:\n  api_key: forbidden\n")
        )
    else:
        candidate = inspect_plugin_folder(_plugin(tmp_path, permission="destructive"))
    assert candidate.preflight_ok is False
    assert candidate.quarantine_reason == expected


@pytest.mark.asyncio
async def test_manager_surfaces_folder_plugin_without_registering_code(tmp_path: Path):
    _plugin(tmp_path)
    manager = IntegrationManager()
    manager.configure_plugin_folder(tmp_path)
    snapshot = await manager.snapshot()
    detail = await manager.detail("demo")
    assert snapshot["integration_count"] == 1
    assert snapshot["ready_count"] == 0
    assert snapshot["quarantined_count"] == 1
    assert detail["stage"] == "quarantined"
    assert detail["status"]["code_loaded"] is False


def test_composite_inline_secret_is_quarantined(tmp_path: Path):
    candidate = inspect_plugin_folder(
        _plugin(tmp_path, extra="metadata:\n  client_secret: forbidden\n")
    )
    assert candidate.preflight_ok is False
    assert candidate.quarantine_reason == "inline_secret_not_allowed"


def test_secret_environment_reference_is_allowed(tmp_path: Path):
    candidate = inspect_plugin_folder(
        _plugin(tmp_path, extra="metadata:\n  client_secret_env: CLIENT_SECRET\n")
    )
    assert candidate.preflight_ok is True
    assert candidate.quarantine_reason == "trust_not_established"


def test_adapter_symlink_is_quarantined(tmp_path: Path):
    plugin = _plugin(tmp_path)
    real_adapter = plugin / "real_adapter.py"
    real_adapter.write_text("class DemoIntegration:\n    pass\n", encoding="utf-8")
    (plugin / "adapter.py").unlink()
    (plugin / "adapter.py").symlink_to(real_adapter)
    candidate = inspect_plugin_folder(plugin)
    assert candidate.preflight_ok is False
    assert candidate.quarantine_reason == "adapter_symlink_not_allowed"


def test_plugin_root_symlink_is_rejected(tmp_path: Path):
    real_root = tmp_path / "real-plugins"
    real_root.mkdir()
    link_root = tmp_path / "linked-plugins"
    link_root.symlink_to(real_root, target_is_directory=True)
    snapshot = PluginFolderDiscovery(link_root).scan()
    assert snapshot["plugin_count"] == 0
    assert snapshot["errors"] == [
        {
            "path": str(link_root.absolute()),
            "error": "plugin_root_symlink_not_allowed",
        }
    ]


def test_manifest_name_path_escape_is_rejected(tmp_path: Path):
    _plugin(tmp_path)
    snapshot = PluginFolderDiscovery(
        tmp_path,
        manifest_name="../hirda-plugin.yaml",
    ).scan()
    assert snapshot["plugin_count"] == 0
    assert snapshot["errors"] == [
        {
            "path": str(tmp_path.absolute()),
            "error": "plugin_manifest_name_invalid",
        }
    ]


def test_plugin_count_limit_is_deterministic(tmp_path: Path):
    _plugin(tmp_path, "a")
    _plugin(tmp_path, "b")
    discovery = PluginFolderDiscovery(tmp_path, max_plugins=1)

    snapshot = discovery.scan()

    assert snapshot["plugin_count"] == 1
    assert snapshot["plugins"][0]["id"] == "a"
    assert snapshot["errors"] == [
        {
            "path": str(tmp_path.absolute()),
            "error": "plugin_count_limit_exceeded",
        }
    ]


def _runtime_adapter_source(plugin_id: str = "demo") -> str:
    return f'''from mcp_studio.integrations import IntegrationManifest\n\nclass DemoIntegration:\n    def __init__(self):\n        self.manifest = IntegrationManifest.from_mapping({{\n            "id": "{plugin_id}",\n            "name": "Demo Plugin",\n            "capabilities": ["demo_read"],\n            "runtime": {{"type": "local-python"}},\n            "tools": ["demo_read"],\n            "permissions": {{"demo_read": "read"}},\n            "health": {{"type": "adapter_probe"}},\n            "routing": {{"preferred_lanes": ["hermes"]}},\n        }})\n\n    async def validate(self):\n        return {{"ok": True}}\n\n    async def certify(self):\n        return {{"ok": True}}\n\n    async def status(self):\n        return {{"ok": True}}\n'''


def test_plugin_fingerprint_covers_adapter_code(tmp_path: Path):
    plugin = _plugin(tmp_path, adapter_source=_runtime_adapter_source())
    first = inspect_plugin_folder(plugin)
    assert first.preflight_ok is True
    assert first.adapter_sha256
    assert first.fingerprint

    (plugin / "adapter.py").write_text(
        _runtime_adapter_source() + "\n# changed\n", encoding="utf-8"
    )
    second = inspect_plugin_folder(plugin)
    assert second.preflight_ok is True
    assert second.manifest_sha256 == first.manifest_sha256
    assert second.adapter_sha256 != first.adapter_sha256
    assert second.fingerprint != first.fingerprint


def test_p3_untrusted_plugin_never_imports_code(tmp_path: Path):
    marker = tmp_path / "runtime-imported"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('loaded')\n"
        + _runtime_adapter_source()
    )
    plugin = _plugin(tmp_path, adapter_source=source)
    candidate = inspect_plugin_folder(plugin)
    assert candidate.fingerprint

    manager = IntegrationManager()
    manager.configure_plugin_folder(
        tmp_path,
        runtime_enabled=True,
        trusted_fingerprints=[],
    )
    result = manager.activate_trusted_plugins()

    assert result["activated"] == []
    assert result["quarantined"] == ["demo"]
    assert not marker.exists()
    detail = manager.plugin_folder_snapshot()["plugins"][0]
    assert detail["status"]["code_loaded"] is False
    assert detail["status"]["trust_established"] is False


@pytest.mark.asyncio
async def test_p3_trusted_fingerprint_loads_then_reconciles(tmp_path: Path):
    marker = tmp_path / "runtime-imported"
    source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('loaded')\n"
        + _runtime_adapter_source()
    )
    plugin = _plugin(tmp_path, adapter_source=source)
    candidate = inspect_plugin_folder(plugin)
    assert candidate.fingerprint

    manager = IntegrationManager()
    manager.configure_plugin_folder(
        tmp_path,
        runtime_enabled=True,
        trusted_fingerprints=[candidate.fingerprint],
    )
    result = manager.activate_trusted_plugins()

    assert result["activated"] == ["demo"]
    assert result["errors"] == []
    assert marker.read_text() == "loaded"
    detail = await manager.reconcile("demo")
    assert detail["stage"] == "ready"
    assert detail["source"].startswith("folder:")
    snapshot = manager.plugin_folder_snapshot()
    assert snapshot["code_loaded_count"] == 1
    assert snapshot["quarantined_count"] == 0


@pytest.mark.asyncio
async def test_p3_rescan_revokes_plugin_when_adapter_fingerprint_changes(tmp_path: Path):
    plugin = _plugin(tmp_path, adapter_source=_runtime_adapter_source())
    candidate = inspect_plugin_folder(plugin)
    assert candidate.fingerprint

    manager = IntegrationManager()
    manager.configure_plugin_folder(
        tmp_path,
        runtime_enabled=True,
        trusted_fingerprints=[candidate.fingerprint],
    )
    first = manager.activate_trusted_plugins()
    assert first["activated"] == ["demo"]
    assert (await manager.reconcile("demo"))["stage"] == "ready"

    (plugin / "adapter.py").write_text(
        _runtime_adapter_source() + "\n# fingerprint changed\n", encoding="utf-8"
    )
    manager.discover_plugin_folders()

    with pytest.raises(KeyError):
        manager.get("demo")
    detail = await manager.detail("demo")
    assert detail["stage"] == "quarantined"
    assert detail["status"]["code_loaded"] is False
    assert detail["status"]["trust_established"] is False


def test_p3_activation_is_idempotent_for_same_fingerprint(tmp_path: Path):
    plugin = _plugin(tmp_path, adapter_source=_runtime_adapter_source())
    candidate = inspect_plugin_folder(plugin)
    assert candidate.fingerprint
    manager = IntegrationManager()
    manager.configure_plugin_folder(
        tmp_path,
        runtime_enabled=True,
        trusted_fingerprints=[candidate.fingerprint],
    )
    first = manager.activate_trusted_plugins()
    second = manager.activate_trusted_plugins()
    assert first["activated"] == ["demo"]
    assert second["activated"] == ["demo"]
    assert second["errors"] == []
