from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
import os
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urlparse

from .graft import ALLOWED_GRAFT_TOOLS, GraftManager


INTEGRATION_MANIFEST_SCHEMA = "hirda-integration-manifest-v1"
INTEGRATION_REGISTRY_SCHEMA = "hirda-integration-registry-v1"
INTEGRATION_STAGES = (
    "discovered",
    "validating",
    "registered",
    "certifying",
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

    @property
    def integration_id(self) -> str:
        return self.adapter.manifest.integration_id


class IntegrationManager:
    """One lifecycle for HIRDA subsystem integrations.

    Adapters keep subsystem-specific logic; this manager owns only discovery,
    validation, registration, certification, and readiness reporting.
    """

    def __init__(self) -> None:
        self._integrations: dict[str, RegisteredIntegration] = {}
        self._discovery_errors: list[dict[str, str]] = []

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
        return registration

    def get(self, integration_id: str) -> RegisteredIntegration:
        key = str(integration_id or "").strip()
        try:
            return self._integrations[key]
        except KeyError:
            raise KeyError(key) from None

    @staticmethod
    def _probe_ok(result: object, *, phase: str) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise IntegrationError(f"{phase} probe must return an object")
        normalized = dict(result)
        if not isinstance(normalized.get("ok"), bool):
            raise IntegrationError(f"{phase} probe must return boolean ok")
        return normalized

    async def reconcile(self, integration_id: str) -> dict[str, Any]:
        registration = self.get(integration_id)
        registration.blocked = False
        registration.failed_stage = None
        registration.error = None
        registration.validation = {}
        registration.certification = {}

        registration.stage = "validating"
        try:
            validation = self._probe_ok(
                await registration.adapter.validate(), phase="validation"
            )
        except Exception as exc:
            registration.blocked = True
            registration.failed_stage = "validating"
            registration.error = f"{type(exc).__name__}: {exc}"
            return await self._detail(registration)
        registration.validation = validation
        if not validation["ok"]:
            registration.blocked = True
            registration.failed_stage = "validating"
            registration.error = str(validation.get("reason") or "validation failed")
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
            return await self._detail(registration)
        registration.certification = certification
        if not certification["ok"]:
            registration.blocked = True
            registration.failed_stage = "certifying"
            registration.error = str(
                certification.get("reason") or "certification failed"
            )
            return await self._detail(registration)

        registration.stage = "ready"
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
            "status": live_status,
        }

    async def detail(
        self, integration_id: str, *, reconcile: bool = False
    ) -> dict[str, Any]:
        if reconcile:
            return await self.reconcile(integration_id)
        return await self._detail(self.get(integration_id))

    def discover_entry_points(
        self,
        *,
        group: str = DEFAULT_INTEGRATION_ENTRYPOINT_GROUP,
        strict: bool = False,
    ) -> dict[str, Any]:
        """Discover external integrations without changing HIRDA core code.

        Loading a Python entry point executes installed package code, so HIRDA
        keeps this opt-in at the Studio configuration boundary.
        """
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
                registration = self.register(
                    adapter,
                    source=f"entrypoint:{name}",
                )
                loaded_ids.append(registration.integration_id)
            except Exception as exc:
                if strict:
                    raise IntegrationError(
                        f"failed to load integration entry point {name!r}: {exc}"
                    ) from exc
                errors.append(
                    {"entry_point": name, "error": type(exc).__name__}
                )

        self._discovery_errors.extend(errors)
        return {"loaded": loaded_ids, "errors": errors}

    async def snapshot(self, *, reconcile: bool = False) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for integration_id in sorted(self._integrations):
            if reconcile:
                item = await self.reconcile(integration_id)
            else:
                item = await self.detail(integration_id)
            items.append(item)
        return {
            "schema": INTEGRATION_REGISTRY_SCHEMA,
            "lifecycle": list(INTEGRATION_STAGES),
            "integration_count": len(items),
            "ready_count": sum(item["stage"] == "ready" and not item["blocked"] for item in items),
            "blocked_count": sum(bool(item["blocked"]) for item in items),
            "entrypoint_group": DEFAULT_INTEGRATION_ENTRYPOINT_GROUP,
            "discovery_errors": list(self._discovery_errors),
            "integrations": items,
        }




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


def build_integration_manager(graft: GraftManager, studio: Any | None = None) -> IntegrationManager:
    manager = IntegrationManager()
    manager.register(GraftIntegrationAdapter(graft), source="builtin:graft")
    if studio is not None:
        manager.register(JevIntegrationAdapter(studio), source="builtin:jev")
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
