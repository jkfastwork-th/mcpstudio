from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

import yaml


PLUGIN_MANIFEST_SCHEMA = "hirda-plugin-v1"
DEFAULT_PLUGIN_MANIFEST_NAME = "hirda-plugin.yaml"
DEFAULT_PLUGIN_DIRECTORY = "plugins"
DEFAULT_PLUGIN_MAX_COUNT = 128
_PERMISSION_CLASSES = {"read", "write", "execute", "destructive"}
_PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_INLINE_SECRET_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
}


@dataclass(frozen=True, slots=True)
class PluginFolderCandidate:
    plugin_id: str
    name: str
    version: str | None
    root: str
    manifest_path: str | None
    manifest_sha256: str | None
    preflight_ok: bool
    quarantine_reason: str
    code_loaded: bool = False
    schema: str | None = None
    adapter: dict[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permissions: dict[str, str] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    issues: tuple[str, ...] = ()

    def as_registry_item(self) -> dict[str, Any]:
        manifest = {
            "schema": self.schema,
            "id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "adapter": dict(self.adapter),
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "permissions": dict(self.permissions),
            "health": dict(self.health),
            "routing": dict(self.routing),
            "metadata": dict(self.metadata),
        }
        return {
            "id": self.plugin_id,
            "name": self.name,
            "source": f"folder:{self.root}",
            "stage": "quarantined",
            "blocked": True,
            "failed_stage": None if self.preflight_ok else "manifest_preflight",
            "error": self.quarantine_reason,
            "manifest": manifest,
            "validation": {
                "ok": self.preflight_ok,
                "reason": None if self.preflight_ok else self.quarantine_reason,
                "issues": list(self.issues),
                "manifest_sha256": self.manifest_sha256,
            },
            "certification": {},
            "status": {
                "ok": False,
                "code_loaded": False,
                "trust_established": False,
                "quarantined": True,
                "quarantine_reason": self.quarantine_reason,
                "manifest_sha256": self.manifest_sha256,
            },
            "plugin": {
                "version": self.version,
                "root": self.root,
                "manifest_path": self.manifest_path,
                "manifest_sha256": self.manifest_sha256,
            },
        }


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _clean_unique_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("must be a list")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        if not text:
            raise ValueError("items must be non-empty strings")
        if text not in seen:
            seen.add(text)
            cleaned.append(text)
    return tuple(cleaned)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("must be an object")
    return dict(value)


def _contains_inline_secret(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(
                r"[^a-z0-9]+",
                "_",
                str(key or "").strip().lower(),
            ).strip("_")
            if normalized.endswith("_env"):
                looks_secret = False
            else:
                looks_secret = (
                    normalized in _INLINE_SECRET_KEYS
                    or normalized.endswith(("_secret", "_token", "_password"))
                    or normalized.startswith(("secret_", "token_", "password_"))
                )
            if looks_secret and nested not in (None, "", False):
                return True
            if _contains_inline_secret(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_inline_secret(item) for item in value)
    return False


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _entry_has_symlink_component(plugin_root: Path, entry: Path) -> bool:
    relative = entry.relative_to(plugin_root)
    current = plugin_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _candidate(
    plugin_root: Path,
    *,
    plugin_id: str | None = None,
    name: str | None = None,
    version: str | None = None,
    manifest_path: Path | None = None,
    manifest_sha256: str | None = None,
    preflight_ok: bool = False,
    quarantine_reason: str,
    schema: str | None = None,
    adapter: dict[str, Any] | None = None,
    capabilities: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    permissions: dict[str, str] | None = None,
    health: dict[str, Any] | None = None,
    routing: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    issues: tuple[str, ...] = (),
) -> PluginFolderCandidate:
    fallback_id = plugin_root.name
    return PluginFolderCandidate(
        plugin_id=plugin_id or fallback_id,
        name=name or plugin_id or fallback_id,
        version=version,
        root=str(plugin_root),
        manifest_path=str(manifest_path) if manifest_path is not None else None,
        manifest_sha256=manifest_sha256,
        preflight_ok=preflight_ok,
        quarantine_reason=quarantine_reason,
        schema=schema,
        adapter=dict(adapter or {}),
        capabilities=tuple(capabilities),
        tools=tuple(tools),
        permissions=dict(permissions or {}),
        health=dict(health or {}),
        routing=dict(routing or {}),
        metadata=dict(metadata or {}),
        issues=tuple(issues),
    )


def inspect_plugin_folder(
    plugin_root: Path,
    *,
    manifest_name: str = DEFAULT_PLUGIN_MANIFEST_NAME,
    reserved_ids: set[str] | None = None,
) -> PluginFolderCandidate:
    reserved_ids = set(reserved_ids or set())
    if plugin_root.is_symlink():
        return _candidate(
            plugin_root,
            quarantine_reason="plugin_directory_symlink",
            issues=("plugin directories may not be symlinks",),
        )
    if not plugin_root.is_dir():
        return _candidate(
            plugin_root,
            quarantine_reason="plugin_directory_invalid",
            issues=("plugin path must be a directory",),
        )

    manifest_path = plugin_root / manifest_name
    if not manifest_path.exists():
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            quarantine_reason="manifest_missing",
            issues=(f"{manifest_name} is required",),
        )
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            quarantine_reason="manifest_path_invalid",
            issues=("manifest must be a regular non-symlink file",),
        )

    try:
        raw_bytes = manifest_path.read_bytes()
    except OSError as exc:
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            quarantine_reason="manifest_read_failed",
            issues=(type(exc).__name__,),
        )
    if len(raw_bytes) > 262_144:
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            manifest_sha256=sha256(raw_bytes).hexdigest(),
            quarantine_reason="manifest_too_large",
            issues=("manifest exceeds 256 KiB",),
        )

    digest = sha256(raw_bytes).hexdigest()
    try:
        raw = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="manifest_parse_failed",
            issues=(type(exc).__name__,),
        )
    if not isinstance(raw, dict):
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="manifest_must_be_object",
            issues=("manifest root must be a mapping",),
        )

    if _contains_inline_secret(raw):
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="inline_secret_not_allowed",
            schema=_clean_text(raw.get("schema")) or None,
            issues=("store secret values in environment variables, not plugin manifests",),
        )

    schema = _clean_text(raw.get("schema"))
    if schema != PLUGIN_MANIFEST_SCHEMA:
        return _candidate(
            plugin_root,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="unsupported_plugin_schema",
            schema=schema or None,
            issues=(f"expected schema {PLUGIN_MANIFEST_SCHEMA}",),
        )

    plugin_id = _clean_text(raw.get("id"))
    name = _clean_text(raw.get("name"))
    version = _clean_text(raw.get("version"))
    if not plugin_id or not _PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        return _candidate(
            plugin_root,
            plugin_id=plugin_root.name,
            name=name or plugin_root.name,
            version=version or None,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="invalid_plugin_id",
            schema=schema,
            issues=("id must match ^[a-z0-9][a-z0-9._-]{0,63}$",),
        )
    if plugin_root.name != plugin_id:
        return _candidate(
            plugin_root,
            plugin_id=plugin_root.name,
            name=name or plugin_id,
            version=version or None,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="plugin_id_directory_mismatch",
            schema=schema,
            issues=(f"directory name must equal plugin id {plugin_id!r}",),
        )
    if plugin_id in reserved_ids:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name or plugin_id,
            version=version or None,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="duplicate_integration_id",
            schema=schema,
            issues=("plugin id conflicts with an existing integration",),
        )
    if not name:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=plugin_id,
            version=version or None,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="plugin_name_missing",
            schema=schema,
            issues=("name is required",),
        )
    if not version:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="plugin_version_missing",
            schema=schema,
            issues=("version is required",),
        )

    try:
        adapter = _mapping(raw.get("adapter"))
    except ValueError:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="adapter_contract_invalid",
            schema=schema,
            issues=("adapter must be an object",),
        )
    adapter_runtime = _clean_text(adapter.get("runtime")).lower()
    entry = _clean_text(adapter.get("entry"))
    adapter_class = _clean_text(adapter.get("class"))
    if adapter_runtime != "python":
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="unsupported_adapter_runtime",
            schema=schema,
            adapter=adapter,
            issues=("P1/P2 supports python manifests only; code is not loaded",),
        )
    if not entry or not adapter_class:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="adapter_contract_invalid",
            schema=schema,
            adapter=adapter,
            issues=("adapter.entry and adapter.class are required",),
        )

    entry_relative = Path(entry)
    if entry_relative.is_absolute() or ".." in entry_relative.parts:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="adapter_path_escape",
            schema=schema,
            adapter=adapter,
            issues=("adapter.entry must remain inside the plugin directory",),
        )
    lexical_entry_path = plugin_root / entry_relative
    if _entry_has_symlink_component(plugin_root, lexical_entry_path):
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="adapter_symlink_not_allowed",
            schema=schema,
            adapter=adapter,
            issues=("adapter path may not contain symlink components",),
        )

    root_resolved = plugin_root.resolve()
    entry_path = lexical_entry_path.resolve()
    if not _path_is_within(entry_path, root_resolved):
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="adapter_path_escape",
            schema=schema,
            adapter=adapter,
            issues=("resolved adapter.entry escapes the plugin directory",),
        )
    if not entry_path.is_file():
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="adapter_entry_missing",
            schema=schema,
            adapter=adapter,
            issues=(f"adapter entry not found: {entry}",),
        )
    try:
        capabilities = _clean_unique_strings(raw.get("capabilities", []))
        tools = _clean_unique_strings(raw.get("tools", []))
        permissions_raw = _mapping(raw.get("permissions", {}))
        health = _mapping(raw.get("health", {"type": "adapter_probe"}))
        routing = _mapping(raw.get("routing", {}))
        metadata = _mapping(raw.get("metadata", {}))
    except ValueError as exc:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="manifest_contract_invalid",
            schema=schema,
            adapter=adapter,
            issues=(str(exc),),
        )

    permissions: dict[str, str] = {}
    for tool_name, raw_permission in permissions_raw.items():
        tool = _clean_text(tool_name)
        permission = _clean_text(raw_permission).lower()
        if not tool or permission not in _PERMISSION_CLASSES:
            return _candidate(
                plugin_root,
                plugin_id=plugin_id,
                name=name,
                version=version,
                manifest_path=manifest_path,
                manifest_sha256=digest,
                quarantine_reason="permission_contract_invalid",
                schema=schema,
                adapter=adapter,
                capabilities=capabilities,
                tools=tools,
                permissions=permissions,
                health=health,
                routing=routing,
                metadata=metadata,
                issues=("permissions must map tool names to supported permission classes",),
            )
        permissions[tool] = permission

    missing_permissions = [tool for tool in tools if tool not in permissions]
    unknown_permissions = sorted(set(permissions) - set(tools))
    if missing_permissions or unknown_permissions:
        issues: list[str] = []
        if missing_permissions:
            issues.append("missing permissions: " + ", ".join(missing_permissions))
        if unknown_permissions:
            issues.append("permissions reference undeclared tools: " + ", ".join(unknown_permissions))
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="permission_contract_invalid",
            schema=schema,
            adapter=adapter,
            capabilities=capabilities,
            tools=tools,
            permissions=permissions,
            health=health,
            routing=routing,
            metadata=metadata,
            issues=tuple(issues),
        )
    if any(permission == "destructive" for permission in permissions.values()):
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="destructive_permission_requires_approval",
            schema=schema,
            adapter=adapter,
            capabilities=capabilities,
            tools=tools,
            permissions=permissions,
            health=health,
            routing=routing,
            metadata=metadata,
            issues=("destructive permission cannot auto-pass manifest preflight",),
        )

    health_type = _clean_text(health.get("type"))
    if not health_type:
        return _candidate(
            plugin_root,
            plugin_id=plugin_id,
            name=name,
            version=version,
            manifest_path=manifest_path,
            manifest_sha256=digest,
            quarantine_reason="health_contract_invalid",
            schema=schema,
            adapter=adapter,
            capabilities=capabilities,
            tools=tools,
            permissions=permissions,
            health=health,
            routing=routing,
            metadata=metadata,
            issues=("health.type is required",),
        )

    return _candidate(
        plugin_root,
        plugin_id=plugin_id,
        name=name,
        version=version,
        manifest_path=manifest_path,
        manifest_sha256=digest,
        preflight_ok=True,
        quarantine_reason="trust_not_established",
        schema=schema,
        adapter=adapter,
        capabilities=capabilities,
        tools=tools,
        permissions=permissions,
        health=health,
        routing=routing,
        metadata=metadata,
        issues=(),
    )


class PluginFolderDiscovery:
    """Manifest-only local plugin discovery.

    P1/P2 never imports adapter modules. Every discovered folder remains
    quarantined until a later trust/runtime phase explicitly approves code load.
    """

    def __init__(
        self,
        root: Path,
        *,
        manifest_name: str = DEFAULT_PLUGIN_MANIFEST_NAME,
        max_plugins: int = DEFAULT_PLUGIN_MAX_COUNT,
    ) -> None:
        # Keep the lexical root so scan() can detect a symlink instead of
        # silently resolving it into a trusted-looking directory.
        self.root = root.expanduser().absolute()
        self.manifest_name = manifest_name
        self.max_plugins = max(1, int(max_plugins))
        self.candidates: dict[str, PluginFolderCandidate] = {}
        self.errors: list[dict[str, str]] = []

    def scan(self, *, reserved_ids: set[str] | None = None) -> dict[str, Any]:
        self.candidates = {}
        self.errors = []
        reserved_ids = set(reserved_ids or set())

        manifest_name = str(self.manifest_name or "").strip()
        manifest_path = Path(manifest_name)
        if (
            not manifest_name
            or manifest_path.is_absolute()
            or len(manifest_path.parts) != 1
            or manifest_name in {".", ".."}
            or "/" in manifest_name
            or "\\" in manifest_name
        ):
            self.errors.append(
                {
                    "path": str(self.root),
                    "error": "plugin_manifest_name_invalid",
                }
            )
            return self.snapshot()

        if not self.root.exists():
            return self.snapshot()
        if self.root.is_symlink():
            self.errors.append(
                {"path": str(self.root), "error": "plugin_root_symlink_not_allowed"}
            )
            return self.snapshot()
        if not self.root.is_dir():
            self.errors.append(
                {"path": str(self.root), "error": "plugin_root_not_directory"}
            )
            return self.snapshot()

        folders = [
            item
            for item in sorted(self.root.iterdir(), key=lambda path: path.name)
            if not item.name.startswith(".")
        ]
        if len(folders) > self.max_plugins:
            self.errors.append(
                {
                    "path": str(self.root),
                    "error": "plugin_count_limit_exceeded",
                }
            )
            folders = folders[: self.max_plugins]

        for folder in folders:
            candidate = inspect_plugin_folder(
                folder,
                manifest_name=manifest_name,
                reserved_ids=reserved_ids,
            )
            self.candidates[candidate.plugin_id] = candidate

        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        items = [
            self.candidates[plugin_id].as_registry_item()
            for plugin_id in sorted(self.candidates)
        ]
        return {
            "enabled": True,
            "root": str(self.root),
            "root_exists": self.root.exists(),
            "manifest_name": self.manifest_name,
            "max_plugins": self.max_plugins,
            "plugin_count": len(items),
            "preflight_ok_count": sum(
                bool(item["validation"]["ok"]) for item in items
            ),
            "quarantined_count": len(items),
            "code_loaded_count": 0,
            "errors": list(self.errors),
            "plugins": items,
        }
