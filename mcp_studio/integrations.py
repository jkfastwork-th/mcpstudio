from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urlparse

from .desktop_commander import (
    EXPOSED_DESKTOP_COMMANDER_TOOLS,
    DesktopCommanderBackend,
)
from .graft import ALLOWED_GRAFT_TOOLS, GraftManager
from .pixel_art_studio import PixelArtStudioProvider
from .plugin_folder import (
    DEFAULT_PLUGIN_DIRECTORY,
    DEFAULT_PLUGIN_MANIFEST_NAME,
    DEFAULT_PLUGIN_MAX_COUNT,
    PluginFolderCandidate,
    PluginFolderDiscovery,
)


INTEGRATION_MANIFEST_SCHEMA = "hirda-integration-manifest-v1"
INTEGRATION_REGISTRY_SCHEMA = "hirda-integration-registry-v1"
INTEGRATION_STAGES = (
    "discovered",
    "quarantined",
    "validating",
    "registered",
    "certifying",
    "activating",
    "ready",
)
DEFAULT_INTEGRATION_ENTRYPOINT_GROUP = "hirda.integrations"
_PERMISSION_CLASSES = {"read", "write", "execute", "destructive"}


class IntegrationError(RuntimeError):
    pass


def _clean_text(value: object, *, field_name: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise IntegrationError(f"{field_name} is required")
    return text


def _clean_unique_strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise IntegrationError(f"{field_name} must be a list")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, field_name=field_name)
        if text not in seen:
            seen.add(text)
            cleaned.append(text)
    return tuple(cleaned)


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrationError(f"{field_name} must be an object")
    return dict(value)


@dataclass(frozen=True, slots=True)
class IntegrationManifest:
    integration_id: str
    name: str
    capabilities: tuple[str, ...]
    runtime: dict[str, Any]
    tools: tuple[str, ...]
    permissions: dict[str, str]
    health: dict[str, Any]
    routing: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    schema: str = INTEGRATION_MANIFEST_SCHEMA

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "IntegrationManifest":
        if not isinstance(raw, dict):
            raise IntegrationError("manifest must be an object")
        schema = str(raw.get("schema") or INTEGRATION_MANIFEST_SCHEMA).strip()
        if schema != INTEGRATION_MANIFEST_SCHEMA:
            raise IntegrationError(f"unsupported integration manifest schema: {schema}")

        integration_id = _clean_text(raw.get("id"), field_name="id")
        name = _clean_text(raw.get("name"), field_name="name")
        capabilities = _clean_unique_strings(
            raw.get("capabilities", []), field_name="capabilities"
        )
        tools = _clean_unique_strings(raw.get("tools", []), field_name="tools")
        runtime = _mapping(raw.get("runtime"), field_name="runtime")
        health = _mapping(raw.get("health"), field_name="health")
        routing = _mapping(raw.get("routing", {}), field_name="routing")
        metadata = _mapping(raw.get("metadata", {}), field_name="metadata")
        _clean_text(runtime.get("type"), field_name="runtime.type")
        _clean_text(health.get("type"), field_name="health.type")

        raw_permissions = _mapping(
            raw.get("permissions", {}), field_name="permissions"
        )
        permissions: dict[str, str] = {}
        for tool_name, permission in raw_permissions.items():
            tool = _clean_text(tool_name, field_name="permissions tool")
            category = str(permission or "").strip().lower()
            if category not in _PERMISSION_CLASSES:
                raise IntegrationError(
                    f"permissions[{tool!r}] must be one of {sorted(_PERMISSION_CLASSES)}"
                )
            permissions[tool] = category

        missing_permissions = [tool for tool in tools if tool not in permissions]
        if missing_permissions:
            raise IntegrationError(
                "every declared tool needs a permission class: "
                + ", ".join(missing_permissions)
            )
        unknown_permissions = sorted(set(permissions) - set(tools))
        if unknown_permissions:
            raise IntegrationError(
                "permissions reference undeclared tools: "
                + ", ".join(unknown_permissions)
            )

        preferred = routing.get("preferred_lanes", [])
        if preferred is not None:
            routing["preferred_lanes"] = list(
                _clean_unique_strings(preferred, field_name="routing.preferred_lanes")
            )

        return cls(
            integration_id=integration_id,
            name=name,
            capabilities=capabilities,
            runtime=runtime,
            tools=tools,
            permissions=permissions,
            health=health,
            routing=routing,
            metadata=metadata,
            schema=schema,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "id": self.integration_id,
            "name": self.name,
            "capabilities": list(self.capabilities),
            "runtime": dict(self.runtime),
            "tools": list(self.tools),
            "permissions": dict(self.permissions),
            "health": dict(self.health),
            "routing": dict(self.routing),
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class IntegrationAdapter(Protocol):
    manifest: IntegrationManifest

    async def validate(self) -> dict[str, Any]:
        ...

    async def certify(self) -> dict[str, Any]:
        ...

    async def status(self) -> dict[str, Any]:
        ...


@runtime_checkable
class BackendIntegrationAdapter(Protocol):
    manifest: IntegrationManifest

    async def list_tools(self) -> list[dict[str, Any]]:
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class RegisteredIntegration:
    adapter: IntegrationAdapter
    source: str
    stage: str = "discovered"
    blocked: bool = False
    failed_stage: str | None = None
    error: str | None = None
    validation: dict[str, Any] = field(default_factory=dict)
    certification: dict[str, Any] = field(default_factory=dict)
    tool_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def integration_id(self) -> str:
        return self.adapter.manifest.integration_id


class IntegrationManager:
    """Common lifecycle plus trusted backend-tool activation for HIRDA integrations."""

    def __init__(self) -> None:
        self._integrations: dict[str, RegisteredIntegration] = {}
        self._discovery_errors: list[dict[str, str]] = []
        self._plugin_discovery: PluginFolderDiscovery | None = None
        self._plugin_runtime_enabled = False
        self._plugin_trusted_fingerprints: set[str] = set()
        self._plugin_loaded_fingerprints: dict[str, str] = {}
        self._backend_name_map: dict[str, tuple[str, str]] = {}

    def register(
        self,
        adapter: IntegrationAdapter,
        *,
        source: str = "runtime",
        replace: bool = False,
    ) -> RegisteredIntegration:
        if not isinstance(adapter, IntegrationAdapter):
            raise IntegrationError("adapter must implement IntegrationAdapter contract")
        manifest = adapter.manifest
        if not isinstance(manifest, IntegrationManifest):
            raise IntegrationError("adapter.manifest must be IntegrationManifest")
        integration_id = manifest.integration_id
        if integration_id in self._integrations and not replace:
            raise IntegrationError(f"duplicate integration: {integration_id}")
        registration = RegisteredIntegration(
            adapter=adapter,
            source=_clean_text(source, field_name="source"),
        )
        self._integrations[integration_id] = registration
        self._rebuild_backend_name_map()
        return registration

    def get(self, integration_id: str) -> RegisteredIntegration:
        key = str(integration_id or "").strip()
        try:
            return self._integrations[key]
        except KeyError:
            raise KeyError(key) from None

    def configure_plugin_folder(
        self,
        root: Path,
        *,
        manifest_name: str = DEFAULT_PLUGIN_MANIFEST_NAME,
        max_plugins: int = DEFAULT_PLUGIN_MAX_COUNT,
        runtime_enabled: bool = False,
        trusted_fingerprints: list[str] | tuple[str, ...] | set[str] = (),
    ) -> dict[str, Any]:
        self._plugin_discovery = PluginFolderDiscovery(
            root,
            manifest_name=manifest_name,
            max_plugins=max_plugins,
        )
        self._plugin_runtime_enabled = bool(runtime_enabled)
        self._plugin_trusted_fingerprints = {
            str(value or "").strip().lower()
            for value in trusted_fingerprints
            if str(value or "").strip()
        }
        return self.discover_plugin_folders()

    def _reserved_plugin_ids(self) -> set[str]:
        return {
            integration_id
            for integration_id, registration in self._integrations.items()
            if not registration.source.startswith("folder:")
        }

    def discover_plugin_folders(self) -> dict[str, Any]:
        if self._plugin_discovery is None:
            return self.plugin_folder_snapshot()
        self._plugin_discovery.scan(reserved_ids=self._reserved_plugin_ids())
        # P3 trust is tied to exact manifest+adapter bytes. A rescan that sees
        # changed, removed, invalid, disabled, or no-longer-trusted code must
        # revoke routing immediately. The Python module may remain in sys.modules
        # until process exit, but it is unreachable through HIRDA after removal.
        for integration_id, loaded_fingerprint in list(
            self._plugin_loaded_fingerprints.items()
        ):
            candidate = self._plugin_discovery.candidates.get(integration_id)
            current = str(candidate.fingerprint or "").lower() if candidate else ""
            keep = bool(
                candidate
                and candidate.preflight_ok
                and self._plugin_runtime_enabled
                and current == loaded_fingerprint
                and current in self._plugin_trusted_fingerprints
            )
            if keep:
                continue
            registration = self._integrations.get(integration_id)
            if registration is not None and registration.source.startswith("folder:"):
                self._integrations.pop(integration_id, None)
            self._plugin_loaded_fingerprints.pop(integration_id, None)
        self._rebuild_backend_name_map()
        return self.plugin_folder_snapshot()

    def plugin_folder_snapshot(self) -> dict[str, Any]:
        if self._plugin_discovery is None:
            return {
                "enabled": False,
                "runtime_enabled": False,
                "trusted_fingerprint_count": 0,
                "plugin_count": 0,
                "preflight_ok_count": 0,
                "quarantined_count": 0,
                "code_loaded_count": 0,
                "errors": [],
                "plugins": [],
            }
        snapshot = self._plugin_discovery.snapshot()
        snapshot["runtime_enabled"] = self._plugin_runtime_enabled
        snapshot["trusted_fingerprint_count"] = len(self._plugin_trusted_fingerprints)
        code_loaded_count = 0
        quarantined_count = 0
        for item in snapshot["plugins"]:
            plugin_id = str(item.get("id") or "")
            fingerprint = str((item.get("validation") or {}).get("fingerprint") or "").lower()
            trusted = bool(fingerprint and fingerprint in self._plugin_trusted_fingerprints)
            registration = self._integrations.get(plugin_id)
            loaded = bool(registration and registration.source.startswith("folder:"))
            status = item.setdefault("status", {})
            status["trust_established"] = trusted
            status["code_loaded"] = loaded
            status["runtime_enabled"] = self._plugin_runtime_enabled
            if loaded and registration is not None:
                code_loaded_count += 1
                item["stage"] = registration.stage
                item["blocked"] = registration.blocked
                item["failed_stage"] = registration.failed_stage
                item["error"] = registration.error
                status["quarantined"] = False
                status["quarantine_reason"] = None
            else:
                quarantined_count += 1
                status["quarantined"] = True
                if item.get("validation", {}).get("ok"):
                    if not self._plugin_runtime_enabled:
                        item["error"] = "runtime_activation_disabled"
                        status["quarantine_reason"] = "runtime_activation_disabled"
                    elif not trusted:
                        item["error"] = "trust_not_established"
                        status["quarantine_reason"] = "trust_not_established"
        snapshot["code_loaded_count"] = code_loaded_count
        snapshot["quarantined_count"] = quarantined_count
        return snapshot

    def _folder_candidate(self, integration_id: str) -> PluginFolderCandidate | None:
        if self._plugin_discovery is None:
            return None
        return self._plugin_discovery.candidates.get(integration_id)

    def _folder_detail(self, candidate: PluginFolderCandidate) -> dict[str, Any]:
        snapshot = self.plugin_folder_snapshot()
        item = next(
            (plugin for plugin in snapshot["plugins"] if plugin.get("id") == candidate.plugin_id),
            candidate.as_registry_item(),
        )
        item["schema"] = INTEGRATION_REGISTRY_SCHEMA
        return item

    @staticmethod
    def _probe_ok(result: object, *, phase: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise IntegrationError(f"{phase} probe must return an object")
        normalized = dict(result)
        if not isinstance(normalized.get("ok"), bool):
            raise IntegrationError(f"{phase} probe must return boolean ok")
        return normalized

    @staticmethod
    def _backend_prefix(integration_id: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", integration_id).strip("_")
        return f"hirda__{normalized or 'integration'}__"

    def _rebuild_backend_name_map(self) -> None:
        names: dict[str, tuple[str, str]] = {}
        for integration_id, registration in self._integrations.items():
            if registration.stage != "ready" or registration.blocked:
                continue
            prefix = self._backend_prefix(integration_id)
            for raw_name in registration.tool_catalog:
                names[prefix + raw_name] = (integration_id, raw_name)
        self._backend_name_map = names

    def backend_tools(self) -> list[dict[str, Any]]:
        self._rebuild_backend_name_map()
        output: list[dict[str, Any]] = []
        for exposed_name in sorted(self._backend_name_map):
            integration_id, raw_name = self._backend_name_map[exposed_name]
            registration = self._integrations[integration_id]
            descriptor = dict(registration.tool_catalog[raw_name])
            descriptor["name"] = exposed_name
            description = str(descriptor.get("description") or "").strip()
            descriptor["description"] = (
                f"HIRDA backend [{integration_id}] {description}".strip()
            )
            output.append(descriptor)
        return output

    def backend_permission(self, exposed_name: str) -> str | None:
        self._rebuild_backend_name_map()
        resolved = self._backend_name_map.get(str(exposed_name or ""))
        if resolved is None:
            return None
        integration_id, raw_name = resolved
        return self._integrations[integration_id].adapter.manifest.permissions.get(raw_name)

    def backend_route(self, exposed_name: str) -> tuple[str, str] | None:
        self._rebuild_backend_name_map()
        return self._backend_name_map.get(str(exposed_name or ""))

    def is_backend_tool(self, exposed_name: str) -> bool:
        self._rebuild_backend_name_map()
        return str(exposed_name or "") in self._backend_name_map

    async def call_backend_tool(
        self,
        exposed_name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        self._rebuild_backend_name_map()
        resolved = self._backend_name_map.get(str(exposed_name or ""))
        if resolved is None:
            raise IntegrationError(f"unknown backend tool: {exposed_name}")
        integration_id, raw_name = resolved
        registration = self._integrations[integration_id]
        adapter = registration.adapter
        if not isinstance(adapter, BackendIntegrationAdapter):
            raise IntegrationError(f"integration {integration_id} is not a backend provider")
        return await adapter.call_tool(raw_name, dict(arguments or {}), context=context)

    @staticmethod
    def _validate_folder_adapter_contract(
        candidate: PluginFolderCandidate, adapter: IntegrationAdapter
    ) -> None:
        manifest = adapter.manifest
        checks = {
            "id": manifest.integration_id == candidate.plugin_id,
            "name": manifest.name == candidate.name,
            "capabilities": tuple(manifest.capabilities) == tuple(candidate.capabilities),
            "tools": tuple(manifest.tools) == tuple(candidate.tools),
            "permissions": dict(manifest.permissions) == dict(candidate.permissions),
        }
        failed = [field for field, ok in checks.items() if not ok]
        if failed:
            raise IntegrationError(
                "plugin adapter manifest mismatch: " + ", ".join(failed)
            )

    def activate_trusted_plugins(self) -> dict[str, Any]:
        result = {"activated": [], "quarantined": [], "errors": []}
        if self._plugin_discovery is None:
            return result
        self.discover_plugin_folders()
        for plugin_id in sorted(self._plugin_discovery.candidates):
            candidate = self._plugin_discovery.candidates[plugin_id]
            if not candidate.preflight_ok:
                result["quarantined"].append(plugin_id)
                continue
            fingerprint = str(candidate.fingerprint or "").lower()
            if not self._plugin_runtime_enabled or fingerprint not in self._plugin_trusted_fingerprints:
                result["quarantined"].append(plugin_id)
                continue
            try:
                existing = self._integrations.get(plugin_id)
                if (
                    existing is not None
                    and existing.source.startswith("folder:")
                    and self._plugin_loaded_fingerprints.get(plugin_id) == fingerprint
                ):
                    if plugin_id not in result["activated"]:
                        result["activated"].append(plugin_id)
                    continue
                entry = Path(candidate.root) / str(candidate.adapter.get("entry") or "")
                class_name = str(candidate.adapter.get("class") or "")
                module_name = (
                    f"_hirda_plugin_{re.sub(r'[^a-zA-Z0-9_]+', '_', plugin_id)}_"
                    f"{fingerprint[:12]}"
                )
                spec = importlib.util.spec_from_file_location(module_name, entry)
                if spec is None or spec.loader is None:
                    raise IntegrationError("plugin adapter import spec unavailable")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
                loaded = getattr(module, class_name, None)
                adapter_obj = loaded() if isinstance(loaded, type) else loaded
                if callable(adapter_obj) and not isinstance(adapter_obj, IntegrationAdapter):
                    adapter_obj = adapter_obj()
                if not isinstance(adapter_obj, IntegrationAdapter):
                    raise IntegrationError("plugin class must implement IntegrationAdapter contract")
                adapter = cast(IntegrationAdapter, adapter_obj)
                self._validate_folder_adapter_contract(candidate, adapter)
                self.register(adapter, source=f"folder:{candidate.root}")
                self._plugin_loaded_fingerprints[plugin_id] = fingerprint
                result["activated"].append(plugin_id)
            except Exception as exc:
                result["errors"].append(
                    {"plugin": plugin_id, "error": f"{type(exc).__name__}: {exc}"}
                )
        return result

    async def reconcile(self, integration_id: str) -> dict[str, Any]:
        key = str(integration_id or "").strip()
        registration = self._integrations.get(key)
        if registration is None:
            if self._plugin_discovery is not None:
                self.discover_plugin_folders()
                candidate = self._folder_candidate(key)
                if candidate is not None:
                    return self._folder_detail(candidate)
            raise KeyError(key)

        registration.blocked = False
        registration.failed_stage = None
        registration.error = None
        registration.validation = {}
        registration.certification = {}
        registration.tool_catalog = {}

        registration.stage = "validating"
        try:
            validation = self._probe_ok(
                await registration.adapter.validate(), phase="validation"
            )
        except Exception as exc:
            registration.blocked = True
            registration.failed_stage = "validating"
            registration.error = f"{type(exc).__name__}: {exc}"
            self._rebuild_backend_name_map()
            return await self._detail(registration)
        registration.validation = validation
        if not validation["ok"]:
            registration.blocked = True
            registration.failed_stage = "validating"
            registration.error = str(validation.get("reason") or "validation failed")
            self._rebuild_backend_name_map()
            return await self._detail(registration)

        registration.stage = "registered"
        registration.stage = "certifying"
        try:
            certification = self._probe_ok(
                await registration.adapter.certify(), phase="certification"
            )
        except Exception as exc:
            registration.blocked = True
            registration.failed_stage = "certifying"
            registration.error = f"{type(exc).__name__}: {exc}"
            self._rebuild_backend_name_map()
            return await self._detail(registration)
        registration.certification = certification
        if not certification["ok"]:
            registration.blocked = True
            registration.failed_stage = "certifying"
            registration.error = str(
                certification.get("reason") or "certification failed"
            )
            self._rebuild_backend_name_map()
            return await self._detail(registration)

        if isinstance(registration.adapter, BackendIntegrationAdapter):
            registration.stage = "activating"
            try:
                tools = await registration.adapter.list_tools()
                catalog: dict[str, dict[str, Any]] = {}
                for tool in tools:
                    if not isinstance(tool, dict):
                        raise IntegrationError("backend tool descriptor must be an object")
                    name = str(tool.get("name") or "").strip()
                    schema = tool.get("inputSchema", {})
                    if not name or not isinstance(schema, dict):
                        raise IntegrationError("backend tool requires name and inputSchema")
                    catalog[name] = dict(tool)
                declared = set(registration.adapter.manifest.tools)
                if set(catalog) != declared:
                    raise IntegrationError(
                        "backend tool catalog mismatch: declared="
                        + ",".join(sorted(declared))
                        + " discovered="
                        + ",".join(sorted(catalog))
                    )
                registration.tool_catalog = catalog
            except Exception as exc:
                registration.blocked = True
                registration.failed_stage = "activating"
                registration.error = f"{type(exc).__name__}: {exc}"
                self._rebuild_backend_name_map()
                return await self._detail(registration)

        registration.stage = "ready"
        self._rebuild_backend_name_map()
        return await self._detail(registration)

    async def _detail(self, registration: RegisteredIntegration) -> dict[str, Any]:
        try:
            live_status = await registration.adapter.status()
        except Exception as exc:
            live_status = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "schema": INTEGRATION_REGISTRY_SCHEMA,
            "id": registration.integration_id,
            "name": registration.adapter.manifest.name,
            "source": registration.source,
            "stage": registration.stage,
            "blocked": registration.blocked,
            "failed_stage": registration.failed_stage,
            "error": registration.error,
            "manifest": registration.adapter.manifest.as_dict(),
            "validation": dict(registration.validation),
            "certification": dict(registration.certification),
            "backend_tool_count": len(registration.tool_catalog),
            "backend_tools": sorted(registration.tool_catalog),
            "status": live_status,
        }

    async def detail(
        self, integration_id: str, *, reconcile: bool = False
    ) -> dict[str, Any]:
        key = str(integration_id or "").strip()
        if key in self._integrations:
            if reconcile:
                return await self.reconcile(key)
            return await self._detail(self._integrations[key])
        if reconcile and self._plugin_discovery is not None:
            self.discover_plugin_folders()
        candidate = self._folder_candidate(key)
        if candidate is not None:
            return self._folder_detail(candidate)
        raise KeyError(key)

    def discover_entry_points(
        self,
        *,
        group: str = DEFAULT_INTEGRATION_ENTRYPOINT_GROUP,
        strict: bool = False,
    ) -> dict[str, Any]:
        loaded_ids: list[str] = []
        errors: list[dict[str, str]] = []
        try:
            discovered = metadata.entry_points()
            selector = getattr(discovered, "select", None)
            if callable(selector):
                candidates = selector(group=group)
            else:
                candidates = cast(Any, discovered).get(group, [])
        except Exception as exc:
            if strict:
                raise IntegrationError(
                    f"failed to enumerate integration entry points: {exc}"
                ) from exc
            errors.append({"entry_point": group, "error": type(exc).__name__})
            self._discovery_errors.extend(errors)
            return {"loaded": loaded_ids, "errors": errors}

        for entry_point in cast(Any, candidates):
            name = str(getattr(entry_point, "name", "unknown"))
            try:
                loaded = entry_point.load()
                adapter_obj = loaded() if isinstance(loaded, type) else loaded
                if callable(adapter_obj) and not isinstance(adapter_obj, IntegrationAdapter):
                    adapter_obj = adapter_obj()
                if not isinstance(adapter_obj, IntegrationAdapter):
                    raise IntegrationError(
                        "entry point must provide IntegrationAdapter contract"
                    )
                adapter = cast(IntegrationAdapter, adapter_obj)
                registration = self.register(adapter, source=f"entrypoint:{name}")
                loaded_ids.append(registration.integration_id)
            except Exception as exc:
                if strict:
                    raise IntegrationError(
                        f"failed to load integration entry point {name!r}: {exc}"
                    ) from exc
                errors.append({"entry_point": name, "error": type(exc).__name__})

        self._discovery_errors.extend(errors)
        return {"loaded": loaded_ids, "errors": errors}

    async def snapshot(self, *, reconcile: bool = False) -> dict[str, Any]:
        if reconcile and self._plugin_discovery is not None:
            self.discover_plugin_folders()

        items: list[dict[str, Any]] = []
        for integration_id in sorted(self._integrations):
            if reconcile:
                item = await self.reconcile(integration_id)
            else:
                item = await self._detail(self._integrations[integration_id])
            items.append(item)

        plugin_folder = self.plugin_folder_snapshot()
        loaded_folder_ids = {
            integration_id
            for integration_id, registration in self._integrations.items()
            if registration.source.startswith("folder:")
        }
        items.extend(
            item
            for item in plugin_folder["plugins"]
            if str(item.get("id") or "") not in loaded_folder_ids
        )
        items.sort(
            key=lambda item: (str(item.get("id") or ""), str(item.get("source") or ""))
        )

        return {
            "schema": INTEGRATION_REGISTRY_SCHEMA,
            "lifecycle": list(INTEGRATION_STAGES),
            "integration_count": len(items),
            "ready_count": sum(
                item["stage"] == "ready" and not item["blocked"] for item in items
            ),
            "blocked_count": sum(bool(item["blocked"]) for item in items),
            "quarantined_count": sum(
                item["stage"] == "quarantined" for item in items
            ),
            "backend_tool_count": len(self.backend_tools()),
            "entrypoint_group": DEFAULT_INTEGRATION_ENTRYPOINT_GROUP,
            "discovery_errors": list(self._discovery_errors),
            "plugin_folder": plugin_folder,
            "integrations": items,
        }

    async def close(self) -> None:
        for registration in self._integrations.values():
            close = getattr(registration.adapter, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result


class JevIntegrationAdapter:
    """Expose TypeSafe JEV through the common lifecycle without granting authority."""

    _MODES = {"shadow", "teacher", "enforce"}

    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "jev",
                "name": "TypeSafe JEV",
                "capabilities": [
                    "tool_call_teacher",
                    "bounded_action_judgment",
                    "reflex_shadow_evidence",
                ],
                "runtime": {
                    "type": "remote-http",
                    "authority": "advisory-only",
                },
                "tools": [],
                "permissions": {},
                "health": {"type": "config-probe"},
                "routing": {
                    "mode": "teacher-shadow",
                    "preferred_lanes": [],
                },
                "metadata": {
                    "advisory_only": True,
                    "grant_authority": False,
                    "block_authority": False,
                    "execution_authority": False,
                    "action_creation_authority": False,
                    "hirda_reflex_remains_runtime_authority": True,
                },
            }
        )

    def _snapshot(self) -> dict[str, Any]:
        enabled = bool(getattr(self.studio, "jev_enabled", False))
        mode = str(
            getattr(self.studio, "jev_mode", "shadow") or "shadow"
        ).strip().lower()
        api_url = str(
            getattr(
                self.studio,
                "jev_api_url",
                "https://api.typesafe.ai/v1/systemone",
            )
            or ""
        ).strip()
        key_env = str(
            getattr(self.studio, "jev_api_key_env", "TYPESAFE_API_KEY")
            or "TYPESAFE_API_KEY"
        ).strip()
        model = str(
            getattr(self.studio, "jev_model", "jev-latest") or "jev-latest"
        ).strip()
        timeout = float(
            getattr(self.studio, "jev_timeout_seconds", 2.0) or 0.0
        )
        parsed = urlparse(api_url)
        hostname = (parsed.hostname or "").lower()
        endpoint_valid = bool(
            parsed.scheme in {"http", "https"}
            and hostname
            and (
                parsed.scheme == "https"
                or hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )
        credential_configured = bool(key_env and os.getenv(key_env, "").strip())
        return {
            "enabled": enabled,
            "mode": mode,
            "mode_valid": mode in self._MODES,
            "api_url": api_url,
            "endpoint_valid": endpoint_valid,
            "api_key_env": key_env,
            "credential_configured": credential_configured,
            "model": model,
            "timeout_seconds": timeout,
            "evaluate_read_tools": bool(
                getattr(self.studio, "jev_evaluate_read_tools", False)
            ),
            "configured_fail_closed": bool(
                getattr(self.studio, "jev_fail_closed", False)
            ),
        }

    async def validate(self) -> dict[str, Any]:
        state = self._snapshot()
        reason = None
        if not state["enabled"]:
            reason = "jev_disabled"
        elif not state["mode_valid"]:
            reason = "jev_invalid_mode"
        elif not state["endpoint_valid"]:
            reason = "jev_invalid_api_url"
        elif not state["model"]:
            reason = "jev_model_missing"
        elif state["timeout_seconds"] <= 0:
            reason = "jev_invalid_timeout"
        elif not state["credential_configured"]:
            reason = "missing_api_key"

        return {
            "ok": reason is None,
            "reason": reason,
            **state,
        }

    async def certify(self) -> dict[str, Any]:
        metadata_contract = {
            "advisory_only": True,
            "grant_authority": False,
            "block_authority": False,
            "execution_authority": False,
            "action_creation_authority": False,
            "hirda_reflex_remains_runtime_authority": True,
        }
        metadata_ok = all(
            self.manifest.metadata.get(key) == value
            for key, value in metadata_contract.items()
        )
        no_direct_tools = not self.manifest.tools and not self.manifest.permissions
        routing_ok = self.manifest.routing.get("mode") == "teacher-shadow"
        runtime_ok = self.manifest.runtime.get("authority") == "advisory-only"
        ok = bool(metadata_ok and no_direct_tools and routing_ok and runtime_ok)
        return {
            "ok": ok,
            "reason": None if ok else "jev_authority_contract_mismatch",
            "teacher_only": True,
            "no_direct_tools": no_direct_tools,
            "metadata_contract_ok": metadata_ok,
            "routing_contract_ok": routing_ok,
            "runtime_contract_ok": runtime_ok,
            "authority": {
                "grant": False,
                "block": False,
                "execute": False,
                "create_actions": False,
                "runtime_policy": "hirda-reflex",
            },
        }

    async def status(self) -> dict[str, Any]:
        state = self._snapshot()
        return {
            "ok": bool(
                state["enabled"]
                and state["mode_valid"]
                and state["endpoint_valid"]
                and state["credential_configured"]
                and state["model"]
                and state["timeout_seconds"] > 0
            ),
            **state,
            "network_probe_performed": False,
            "authority": {
                "advisory_only": True,
                "block": False,
                "execute": False,
            },
        }

class DesktopCommanderIntegrationAdapter:
    """HIRDA machine-control backend using a local Desktop Commander MCP process."""

    def __init__(self, studio: Any, *, base_dir: Path | None = None) -> None:
        binary = str(
            getattr(
                studio,
                "desktop_commander_backend_binary",
                "desktop-commander",
            )
            or "desktop-commander"
        ).strip()
        cwd_value = str(
            getattr(studio, "desktop_commander_backend_cwd", "") or ""
        ).strip()
        cwd = Path(cwd_value).expanduser() if cwd_value else (base_dir or Path.home())
        timeout = float(
            getattr(studio, "desktop_commander_backend_timeout_seconds", 15.0) or 15.0
        )
        self.backend = DesktopCommanderBackend(
            binary,
            timeout_seconds=timeout,
            cwd=cwd,
        )
        tools = tuple(EXPOSED_DESKTOP_COMMANDER_TOOLS)
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "desktop-commander",
                "name": "Desktop Commander",
                "capabilities": [
                    "machine_filesystem",
                    "machine_process",
                    "workspace_scoped_execution",
                ],
                "runtime": {
                    "type": "local-stdio-mcp",
                    "authority": "hirda-gated-backend",
                    "binary": binary,
                },
                "tools": list(tools),
                "permissions": dict(EXPOSED_DESKTOP_COMMANDER_TOOLS),
                "health": {"type": "mcp-tool-discovery"},
                "routing": {
                    "mode": "machine-control-backend",
                    "preferred_lanes": ["hermes", "codex", "claude"],
                },
                "metadata": {
                    "backend_capability": True,
                    "raw_mcp_not_exposed": True,
                    "workspace_scope_enforced": True,
                    "process_ownership_enforced": True,
                    "permission_router": "hirda",
                },
            }
        )

    async def validate(self) -> dict[str, Any]:
        binary_ok = self.backend.binary_ok()
        return {
            "ok": binary_ok,
            "reason": None if binary_ok else "desktop_commander_binary_missing",
            "binary": str(self.backend.binary),
            "binary_exists": self.backend.binary.is_file(),
            "binary_executable": binary_ok,
        }

    async def certify(self) -> dict[str, Any]:
        tools = await self.backend.list_tools(refresh=True)
        discovered = {str(tool.get("name") or "") for tool in tools}
        expected = set(self.manifest.tools)
        permission_contract_ok = all(
            self.manifest.permissions.get(name) in _PERMISSION_CLASSES for name in expected
        )
        ok = discovered == expected and permission_contract_ok
        return {
            "ok": ok,
            "reason": None if ok else "desktop_commander_tool_contract_mismatch",
            "expected_tools": sorted(expected),
            "discovered_tools": sorted(discovered),
            "permission_contract_ok": permission_contract_ok,
            "raw_mcp_not_exposed": True,
        }

    async def status(self) -> dict[str, Any]:
        return self.backend.status()

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self.backend.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.backend.call_tool(name, arguments, context=context)

    async def close(self) -> None:
        await self.backend.close()


class GraftIntegrationAdapter:
    """Expose the existing Graft context plane through the common lifecycle."""

    def __init__(self, graft: GraftManager) -> None:
        self.graft = graft
        tools = tuple(sorted(ALLOWED_GRAFT_TOOLS))
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "graft",
                "name": "Graft",
                "capabilities": ["code_graph", "context_search", "call_trace"],
                "runtime": {
                    "type": "local-cli",
                    "authority": "context-read-only",
                },
                "tools": list(tools),
                "permissions": {tool: "read" for tool in tools},
                "health": {"type": "adapter_probe"},
                "routing": {
                    "mode": "context-plane",
                    "preferred_lanes": [],
                },
                "metadata": {
                    "write_authority": False,
                    "destructive_authority": False,
                    "cognitive_memory_write": False,
                },
            }
        )

    async def validate(self) -> dict[str, Any]:
        cli = self.graft._cli_path()
        enabled = self.graft.enabled
        cli_exists = cli.is_file()
        ok = bool(enabled and cli_exists)
        reason = None
        if not enabled:
            reason = "graft_disabled"
        elif not cli_exists:
            reason = "graft_cli_missing"
        return {
            "ok": ok,
            "reason": reason,
            "global_enabled": enabled,
            "cli_path": str(cli),
            "cli_exists": cli_exists,
        }

    async def certify(self) -> dict[str, Any]:
        profile = self.graft.permission_profile()
        expected = {
            "read": True,
            "write": False,
            "execute": True,
            "destructive": False,
            "scope": "workspace",
            "fail_closed_unknown": True,
        }
        tool_contract_ok = set(self.manifest.tools) == set(ALLOWED_GRAFT_TOOLS)
        permission_contract_ok = all(
            self.manifest.permissions.get(tool) == "read"
            for tool in ALLOWED_GRAFT_TOOLS
        )
        authority_ok = profile == expected
        ok = bool(tool_contract_ok and permission_contract_ok and authority_ok)
        return {
            "ok": ok,
            "reason": None if ok else "graft_authority_contract_mismatch",
            "tool_contract_ok": tool_contract_ok,
            "permission_contract_ok": permission_contract_ok,
            "authority_contract_ok": authority_ok,
            "authority": profile,
        }

    async def status(self) -> dict[str, Any]:
        cli = self.graft._cli_path()
        return {
            "ok": bool(self.graft.enabled and cli.is_file()),
            "global_enabled": self.graft.enabled,
            "cli_path": str(cli),
            "cli_exists": cli.is_file(),
            "authority": self.graft.permission_profile(),
            "workspace_activation": "existing-graft-profile",
        }


class PixelArtStudioIntegrationAdapter:
    """Expose deterministic pixel-art candidate generation through HIRDA lifecycle."""

    def __init__(self, provider: PixelArtStudioProvider) -> None:
        self.provider = provider
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "pixel_art_studio",
                "name": "Pixel Art Studio",
                "capabilities": [
                    "pixel_art_sprite",
                    "pixel_art_prop",
                    "pixel_art_icon",
                    "deterministic_asset_compiler",
                ],
                "runtime": {
                    "type": "local-python-library",
                    "authority": "candidate-only",
                },
                "tools": ["pixel_art_generate"],
                "permissions": {"pixel_art_generate": "execute"},
                "health": {"type": "adapter_probe"},
                "routing": {
                    "mode": "visual-provider",
                    "preferred_lanes": ["claude", "codex", "hermes"],
                },
                "metadata": {
                    "provider": "Gamezxz/pixel-art-studio",
                    "nova_arbitrary_code": False,
                    "earth_world_authority": False,
                    "candidate_only": True,
                    "deterministic": True,
                },
            }
        )

    async def validate(self) -> dict[str, Any]:
        state = self.provider.status()
        reason = None
        if not state["enabled"]:
            reason = "pixel_art_studio_disabled"
        elif not state["required_files_ok"]:
            reason = "pixel_art_studio_files_missing"
        elif not state["pin_ok"]:
            reason = "pixel_art_studio_commit_mismatch"
        return {
            "ok": bool(state["ready"]),
            "reason": reason,
            **state,
        }

    async def certify(self) -> dict[str, Any]:
        metadata = self.manifest.metadata
        runtime = self.manifest.runtime
        permissions = self.manifest.permissions
        authority_ok = bool(
            runtime.get("authority") == "candidate-only"
            and metadata.get("candidate_only") is True
            and metadata.get("earth_world_authority") is False
            and metadata.get("nova_arbitrary_code") is False
        )
        permission_ok = permissions == {"pixel_art_generate": "execute"}
        ok = bool(authority_ok and permission_ok)
        return {
            "ok": ok,
            "reason": None if ok else "pixel_art_studio_authority_contract_mismatch",
            "authority_contract_ok": authority_ok,
            "permission_contract_ok": permission_ok,
            "candidate_only": True,
            "earth_world_authority": False,
            "arbitrary_code_from_nova": False,
        }

    async def status(self) -> dict[str, Any]:
        state = self.provider.status()
        return {
            "ok": bool(state["ready"]),
            **state,
            "authority": {
                "candidate_only": True,
                "earth_world_authority": False,
                "arbitrary_code_from_nova": False,
            },
        }


def build_integration_manager(
    graft: GraftManager,
    studio: Any | None = None,
    *,
    base_dir: Path | None = None,
    pixel_art_studio: PixelArtStudioProvider | None = None,
) -> IntegrationManager:
    manager = IntegrationManager()
    manager.register(GraftIntegrationAdapter(graft), source="builtin:graft")
    if pixel_art_studio is not None:
        manager.register(
            PixelArtStudioIntegrationAdapter(pixel_art_studio),
            source="builtin:pixel-art-studio",
        )
    if studio is not None:
        manager.register(JevIntegrationAdapter(studio), source="builtin:jev")
        if bool(getattr(studio, "desktop_commander_backend_enabled", False)):
            manager.register(
                DesktopCommanderIntegrationAdapter(studio, base_dir=base_dir),
                source="builtin:desktop-commander",
            )

        if bool(getattr(studio, "integration_plugin_folder_enabled", True)):
            configured_root = Path(
                str(
                    getattr(
                        studio,
                        "integration_plugin_folder_path",
                        DEFAULT_PLUGIN_DIRECTORY,
                    )
                    or DEFAULT_PLUGIN_DIRECTORY
                )
            ).expanduser()
            if not configured_root.is_absolute() and base_dir is not None:
                configured_root = base_dir / configured_root
            manifest_name = str(
                getattr(
                    studio,
                    "integration_plugin_manifest_name",
                    DEFAULT_PLUGIN_MANIFEST_NAME,
                )
                or DEFAULT_PLUGIN_MANIFEST_NAME
            ).strip()
            max_plugins = max(
                1,
                int(
                    getattr(
                        studio,
                        "integration_plugin_max_count",
                        DEFAULT_PLUGIN_MAX_COUNT,
                    )
                    or DEFAULT_PLUGIN_MAX_COUNT
                ),
            )
            trusted = getattr(studio, "integration_plugin_trusted_fingerprints", []) or []
            manager.configure_plugin_folder(
                configured_root,
                manifest_name=manifest_name,
                max_plugins=max_plugins,
                runtime_enabled=bool(
                    getattr(studio, "integration_plugin_runtime_enabled", False)
                ),
                trusted_fingerprints=list(trusted),
            )
            manager.activate_trusted_plugins()

    if studio is not None and bool(
        getattr(studio, "integration_entrypoints_enabled", False)
    ):
        group = str(
            getattr(
                studio,
                "integration_entrypoint_group",
                DEFAULT_INTEGRATION_ENTRYPOINT_GROUP,
            )
            or DEFAULT_INTEGRATION_ENTRYPOINT_GROUP
        ).strip()
        manager.discover_entry_points(group=group, strict=False)
    return manager
