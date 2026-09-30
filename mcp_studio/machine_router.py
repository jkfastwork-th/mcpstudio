from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .computer import ComputerUseManager
from .integrations import IntegrationManager, IntegrationError
from .machine_registry import MachineRegistry, MachineRegistryError
from .tool_permissions import workspace_scope_violation


_BROWSER_VISUAL_FALLBACK_TOOL = "hirda__browser__visual_fallback"
_CAPABILITY_ROUTE_SCHEMA = "hirda-capability-route-v1"
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PERMISSION_CLASSES = {"read", "write", "execute", "destructive"}


class MachineCapabilityRouter:
    """Select machine + backend while preserving HIRDA's existing policy gates."""

    def __init__(
        self,
        registry: MachineRegistry,
        integrations: IntegrationManager,
        computer: ComputerUseManager,
    ) -> None:
        self.registry = registry
        self.integrations = integrations
        self.computer = computer

    def backend_tools(self) -> list[dict[str, Any]]:
        tools = list(self.integrations.backend_tools())
        tools.append(
            {
                "name": _BROWSER_VISUAL_FALLBACK_TOOL,
                "description": (
                    "Escalate the current managed browser task to session-isolated "
                    "Computer Use/VNC when DOM/CDP automation is unavailable or "
                    "insufficient. This returns a safe viewer descriptor; it does not "
                    "claim the pending click/type action completed."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "maxLength": 1000},
                        "failed_tool": {"type": "string", "maxLength": 200},
                    },
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
            }
        )
        return tools

    def backend_permission(self, name: str) -> str | None:
        if str(name or "") == _BROWSER_VISUAL_FALLBACK_TOOL:
            # descriptor() may provision a session-owned desktop runtime.
            return "execute"
        return self.integrations.backend_permission(name)

    def is_backend_tool(self, name: str) -> bool:
        return str(name or "") == _BROWSER_VISUAL_FALLBACK_TOOL or self.integrations.is_backend_tool(name)

    def authorize_session(self, session: dict[str, Any], category: str):
        return self.registry.authorize_session(session, category)


    def _node_facet_data(
        self,
        node_id: str,
        facet: str,
    ) -> dict[str, Any]:
        lifecycle = getattr(self.registry, "lifecycle", None)
        if not callable(lifecycle):
            return {}
        try:
            record = lifecycle(node_id)
        except (KeyError, MachineRegistryError):
            return {}
        facets = ((record.get("enrichment") or {}).get("facets") or {})
        data = ((facets.get(facet) or {}).get("data") or {})
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _claim(
        *,
        node_id: str,
        scope: str,
        capability: str,
        backend_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "machine_id": node_id,
            "scope": scope,
            "capability": capability,
            "backend_id": backend_id,
            "model_id": model_id,
        }

    def _known_backend_ids(self, node_id: str) -> set[str]:
        backends = self._node_facet_data(node_id, "runtime.backends")
        return {
            str(item.get("id") or "").strip().lower()
            for item in backends.get("backends", [])
            if (
                isinstance(item, dict)
                and bool(item.get("available"))
                and str(item.get("id") or "").strip()
            )
        }

    def _known_model_refs(self, node_id: str) -> set[tuple[str, str]]:
        models = self._node_facet_data(node_id, "runtime.models")
        refs: set[tuple[str, str]] = set()
        for inventory in models.get("inventories", []):
            if not isinstance(inventory, dict):
                continue
            if str(inventory.get("status") or "").strip().lower() != "ready":
                continue
            current_backend = str(
                inventory.get("backend_id") or ""
            ).strip().lower()
            if not current_backend:
                continue
            for model in inventory.get("models", []):
                if not isinstance(model, dict):
                    continue
                current_model = str(model.get("id") or "").strip()
                if current_model:
                    refs.add((current_backend, current_model))
        return refs

    def _capability_declared(
        self,
        node_id: str,
        capability: str,
        *,
        backend_id: str | None,
        model_id: str | None,
    ) -> bool:
        advertised = self._node_facet_data(
            node_id,
            "capabilities.advertised",
        )
        node = advertised.get("node")
        if (
            backend_id is None
            and model_id is None
            and isinstance(node, dict)
            and capability in (node.get("capabilities") or [])
        ):
            return True

        for item in advertised.get("backends", []):
            if not isinstance(item, dict):
                continue
            current_backend = str(item.get("backend_id") or "").strip().lower()
            if (
                capability in (item.get("capabilities") or [])
                and (backend_id is None or current_backend == backend_id)
                and model_id is None
            ):
                return True

        for item in advertised.get("models", []):
            if not isinstance(item, dict):
                continue
            current_backend = str(item.get("backend_id") or "").strip().lower()
            current_model = str(item.get("model_id") or "").strip()
            if (
                capability in (item.get("capabilities") or [])
                and (backend_id is None or current_backend == backend_id)
                and (model_id is None or current_model == model_id)
            ):
                return True
        return False

    def _capability_claims(
        self,
        node_id: str,
        capability: str,
        *,
        backend_id: str | None,
        model_id: str | None,
    ) -> list[dict[str, Any]]:
        advertised = self._node_facet_data(
            node_id,
            "capabilities.advertised",
        )
        known_backends = self._known_backend_ids(node_id)
        known_models = self._known_model_refs(node_id)
        claims: list[dict[str, Any]] = []

        node = advertised.get("node")
        node_capabilities = (
            node.get("capabilities")
            if isinstance(node, dict)
            and isinstance(node.get("capabilities"), list)
            else []
        )
        if (
            capability in node_capabilities
            and backend_id is None
            and model_id is None
        ):
            claims.append(
                self._claim(
                    node_id=node_id,
                    scope="node",
                    capability=capability,
                )
            )

        for item in advertised.get("backends", []):
            if not isinstance(item, dict):
                continue
            current_backend = str(item.get("backend_id") or "").strip().lower()
            if (
                not current_backend
                or current_backend not in known_backends
                or capability not in (item.get("capabilities") or [])
                or (backend_id is not None and current_backend != backend_id)
                or model_id is not None
            ):
                continue
            claims.append(
                self._claim(
                    node_id=node_id,
                    scope="backend",
                    capability=capability,
                    backend_id=current_backend,
                )
            )

        for item in advertised.get("models", []):
            if not isinstance(item, dict):
                continue
            current_backend = str(item.get("backend_id") or "").strip().lower()
            current_model = str(item.get("model_id") or "").strip()
            if (
                not current_backend
                or not current_model
                or current_backend not in known_backends
                or (current_backend, current_model) not in known_models
                or capability not in (item.get("capabilities") or [])
                or (backend_id is not None and current_backend != backend_id)
                or (model_id is not None and current_model != model_id)
            ):
                continue
            claims.append(
                self._claim(
                    node_id=node_id,
                    scope="model",
                    capability=capability,
                    backend_id=current_backend,
                    model_id=current_model,
                )
            )
        return claims

    def _availability(
        self,
        node_id: str,
    ) -> dict[str, Any]:
        resolver = getattr(self.registry, "availability", None)
        if not callable(resolver):
            return {
                "node_id": node_id,
                "fresh": False,
                "stale": True,
                "available": False,
                "state": "health_unavailable",
                "load": {},
                "storage": {},
                "backends": [],
            }
        try:
            result = resolver(node_id)
        except (KeyError, MachineRegistryError):
            return {
                "node_id": node_id,
                "fresh": False,
                "stale": True,
                "available": False,
                "state": "health_unavailable",
                "load": {},
                "storage": {},
                "backends": [],
            }
        return dict(result)

    @staticmethod
    def _backend_health(
        availability: dict[str, Any],
        backend_id: str | None,
    ) -> dict[str, Any] | None:
        if backend_id is None:
            return None
        for item in availability.get("backends", []):
            if (
                isinstance(item, dict)
                and str(item.get("backend_id") or "").strip().lower()
                == backend_id
            ):
                return dict(item)
        return None

    @staticmethod
    def _route_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        health = candidate.get("health") or {}
        load_ratio = health.get("load_ratio_1m")
        memory_ratio = health.get("memory_available_ratio")
        latency = health.get("backend_latency_ms")
        scope_rank = {"model": 0, "backend": 1, "node": 2}.get(
            str(candidate.get("scope") or ""),
            3,
        )

        load_key = (
            float(load_ratio)
            if isinstance(load_ratio, (int, float))
            and math.isfinite(float(load_ratio))
            else math.inf
        )
        latency_key = (
            float(latency)
            if isinstance(latency, (int, float))
            and math.isfinite(float(latency))
            else math.inf
        )
        memory_key = (
            -float(memory_ratio)
            if isinstance(memory_ratio, (int, float))
            and math.isfinite(float(memory_ratio))
            else 0.0
        )
        return (
            load_key,
            latency_key,
            memory_key,
            scope_rank,
            str(candidate.get("node_id") or ""),
            str(candidate.get("backend_id") or ""),
            str(candidate.get("model_id") or ""),
        )

    def resolve_capability_route(
        self,
        capability: str,
        *,
        permission: str,
        machine_id: str | None = None,
        backend_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_capability = str(capability or "").strip().lower()
        normalized_permission = str(permission or "").strip().lower()
        normalized_machine = str(machine_id or "").strip() or None
        normalized_backend = str(backend_id or "").strip().lower() or None
        normalized_model = str(model_id or "").strip() or None

        if not _CAPABILITY_RE.fullmatch(normalized_capability):
            raise MachineRegistryError("invalid capability route capability")
        if normalized_permission not in _PERMISSION_CLASSES:
            raise MachineRegistryError("invalid capability route permission")
        if normalized_model is not None and normalized_backend is None:
            raise MachineRegistryError(
                "model capability route requires backend_id"
            )

        self.registry.sync_trusted_devices()
        eligible: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for machine in self.registry.list():
            node_id = str(machine.machine_id)
            if normalized_machine is not None and node_id != normalized_machine:
                continue

            claims = self._capability_claims(
                node_id,
                normalized_capability,
                backend_id=normalized_backend,
                model_id=normalized_model,
            )
            if not claims:
                declared = self._capability_declared(
                    node_id,
                    normalized_capability,
                    backend_id=normalized_backend,
                    model_id=normalized_model,
                )
                rejected.append(
                    {
                        "node_id": node_id,
                        "machine_id": node_id,
                        "scope": None,
                        "backend_id": normalized_backend,
                        "model_id": normalized_model,
                        "reason": (
                            "lineage_mismatch"
                            if declared
                            else "capability_not_advertised"
                        ),
                    }
                )
                continue

            if not bool(machine.enabled):
                for claim in claims:
                    rejected.append(
                        {
                            **claim,
                            "reason": "machine_disabled",
                        }
                    )
                continue

            policy = self.registry.authorize_session(
                {"metadata": {"machine_id": node_id}},
                normalized_permission,
            )
            if not policy.allowed:
                for claim in claims:
                    rejected.append(
                        {
                            **claim,
                            "reason": "permission_denied",
                            "policy_code": policy.code,
                        }
                    )
                continue

            availability = self._availability(node_id)
            if bool(availability.get("stale")) or not bool(
                availability.get("fresh")
            ):
                for claim in claims:
                    rejected.append(
                        {
                            **claim,
                            "reason": "health_stale",
                            "health_state": availability.get("state"),
                        }
                    )
                continue
            if not bool(availability.get("available")):
                for claim in claims:
                    rejected.append(
                        {
                            **claim,
                            "reason": "node_unavailable",
                            "health_state": availability.get("state"),
                        }
                    )
                continue

            node_load = (
                availability.get("load")
                if isinstance(availability.get("load"), dict)
                else {}
            )
            for claim in claims:
                backend_health = self._backend_health(
                    availability,
                    claim.get("backend_id"),
                )
                if claim.get("backend_id") is not None:
                    if backend_health is None:
                        rejected.append(
                            {
                                **claim,
                                "reason": "backend_health_missing",
                            }
                        )
                        continue
                    if not bool(backend_health.get("available")):
                        rejected.append(
                            {
                                **claim,
                                "reason": "backend_unavailable",
                                "backend_state": backend_health.get("state"),
                            }
                        )
                        continue

                eligible.append(
                    {
                        **claim,
                        "permission": normalized_permission,
                        "health": {
                            "state": availability.get("state"),
                            "age_seconds": availability.get("age_seconds"),
                            "load_ratio_1m": node_load.get("load_ratio_1m"),
                            "memory_available_ratio": node_load.get(
                                "memory_available_ratio"
                            ),
                            "backend_state": (
                                backend_health.get("state")
                                if backend_health is not None
                                else None
                            ),
                            "backend_latency_ms": (
                                backend_health.get("latency_ms")
                                if backend_health is not None
                                else None
                            ),
                        },
                    }
                )

        eligible.sort(key=self._route_sort_key)
        return {
            "schema": _CAPABILITY_ROUTE_SCHEMA,
            "capability": normalized_capability,
            "permission": normalized_permission,
            "filters": {
                "machine_id": normalized_machine,
                "backend_id": normalized_backend,
                "model_id": normalized_model,
            },
            "selected": dict(eligible[0]) if eligible else None,
            "eligible": eligible,
            "rejected": rejected,
            "eligible_count": len(eligible),
            "rejected_count": len(rejected),
            "no_route_reason": None if eligible else "no_eligible_route",
        }

    @staticmethod
    def _backend_result_error(result: dict[str, Any]) -> str | None:
        if bool(result.get("isError")):
            return "backend_result_is_error"
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return None
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text") or "").strip()
            if text.lower().startswith("error:"):
                return text[:1000]
        return None

    async def _browser_visual_fallback(
        self,
        machine: Any,
        *,
        context: dict[str, Any],
        failed_tool: str | None,
        reason: str,
        automatic: bool,
    ) -> dict[str, Any] | None:
        provider = dict(machine.providers.get("computer_use") or {})
        mode = str(provider.get("mode") or "").strip()
        if mode not in {"local", "agent"}:
            return None
        if mode == "local" and not machine.local:
            return None
        managed_session_id = str(context.get("managed_session_id") or "").strip()
        if not managed_session_id:
            return None

        # Automatic escalation must never upgrade a read/write browser call into
        # an execute side effect. If the original gateway decision was not
        # execute, return a routing hint and require a separate explicit
        # visual_fallback call, which passes policy/Reflex/JEV as execute.
        permission_class = str(context.get("permission_class") or "").strip()
        if automatic and permission_class != "execute":
            payload = {
                "ok": True,
                "action_completed": False,
                "fallback_required": True,
                "fallback_mode": "computer_use_vnc",
                "routing_strategy": "dom_cdp_first_visual_fallback",
                "automatic": True,
                "failed_tool": failed_tool,
                "reason": str(reason or "browser_dom_cdp_insufficient")[:1000],
                "managed_session_id": managed_session_id,
                "machine_id": machine.machine_id,
                "visual_agent_action_required": True,
                "fallback_tool": _BROWSER_VISUAL_FALLBACK_TOOL,
                "fallback_permission_required": "execute",
                "computer_use": None,
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}],
                "isError": True,
            }

        try:
            descriptor = await self._computer_descriptor_for_machine(
                machine,
                managed_session_id,
            )
        except Exception:
            return None

        safe_descriptor = {
            key: descriptor.get(key)
            for key in (
                "viewer_url",
                "websocket_path",
                "novnc_available",
                "auth_required",
                "cdp_port",
                "desktop_display",
                "runtime_mode",
                "gpu_mode",
                "gpu_hardware_available",
                "gpu_presentation_mode",
            )
            if key in descriptor
        }
        payload = {
            "ok": True,
            "action_completed": False,
            "fallback_required": True,
            "fallback_mode": "computer_use_vnc",
            "routing_strategy": "dom_cdp_first_visual_fallback",
            "automatic": bool(automatic),
            "failed_tool": failed_tool,
            "reason": str(reason or "browser_dom_cdp_insufficient")[:1000],
            "managed_session_id": managed_session_id,
            "machine_id": machine.machine_id,
            "visual_agent_action_required": True,
            "computer_use": safe_descriptor,
        }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            # Automatic escalation means the original browser action did not
            # complete. Explicit escalation itself is a successful routing call.
            "isError": bool(automatic),
        }

    async def call_backend_tool(
        self,
        exposed_name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        machine_id = str(context.get("machine_id") or self.registry.local_machine_id)
        machine = self.registry.get(machine_id)

        if exposed_name == _BROWSER_VISUAL_FALLBACK_TOOL:
            fallback = await self._browser_visual_fallback(
                machine,
                context=context,
                failed_tool=str(arguments.get("failed_tool") or "").strip() or None,
                reason=str(arguments.get("reason") or "browser_dom_cdp_insufficient"),
                automatic=False,
            )
            if fallback is None:
                raise MachineRegistryError(
                    f"Computer Use browser fallback is unavailable for machine {machine.machine_id}"
                )
            return fallback

        route = self.integrations.backend_route(exposed_name)
        if route is None:
            raise IntegrationError(f"unknown backend tool: {exposed_name}")
        integration_id, raw_name = route
        if integration_id == "openbrowser":
            browser_context = context
            if raw_name == "attach_session":
                managed_session_id = str(context.get("managed_session_id") or "").strip()
                provider = dict(machine.providers.get("computer_use") or {})
                if not managed_session_id or str(provider.get("mode") or "") != "local" or not machine.local:
                    raise MachineRegistryError("OpenBrowser existing-CDP attach requires local HIRDA Computer Use")
                descriptor = await self._computer_descriptor_for_machine(
                    machine,
                    managed_session_id,
                )
                browser_context = dict(context)
                browser_context["cdp_port"] = int(descriptor["cdp_port"])
            try:
                result = await self.integrations.call_backend_tool(
                    exposed_name, arguments, context=browser_context
                )
            except Exception as exc:
                fallback = await self._browser_visual_fallback(
                    machine,
                    context=context,
                    failed_tool=exposed_name,
                    reason=f"{type(exc).__name__}: {exc}",
                    automatic=True,
                )
                if fallback is not None:
                    return fallback
                raise
            backend_error = self._backend_result_error(result)
            if backend_error:
                fallback = await self._browser_visual_fallback(
                    machine,
                    context=context,
                    failed_tool=exposed_name,
                    reason=backend_error,
                    automatic=True,
                )
                if fallback is not None:
                    return fallback
            return result
        if integration_id != "desktop-commander":
            return await self.integrations.call_backend_tool(exposed_name, arguments, context=context)
        provider = dict(machine.providers.get("desktop_commander") or {})
        mode = str(provider.get("mode") or "").strip()
        if mode == "local":
            if not machine.local:
                raise MachineRegistryError("non-local machine cannot use local desktop commander")
            policy = context.get("policy") if isinstance(context.get("policy"), dict) else {}
            if policy.get("scope") == "workspace":
                workspace_root = self.registry.workspace_root(
                    machine,
                    str(context.get("workspace_key") or ""),
                    str(context.get("project_path") or "") or None,
                )
                violation = workspace_scope_violation(arguments, Path(workspace_root))
                if violation:
                    raise MachineRegistryError(f"workspace scope violation: {violation}")
            return await self.integrations.call_backend_tool(exposed_name, arguments, context=context)
        if mode in {"agent", "broker"}:
            remote_arguments = dict(arguments)
            if (
                raw_name == "start_process"
                and str(machine.os_name or "").lower().startswith("win")
                and not str(remote_arguments.get("shell") or "").strip()
            ):
                remote_arguments["shell"] = "cmd.exe"
            return await self.registry.remote_call(
                machine,
                tool_name=raw_name,
                arguments=remote_arguments,
                workspace_key=str(context.get("workspace_key") or ""),
                project_path=(str(context.get("project_path")) if context.get("project_path") else None),
                session_id=str(context.get("managed_session_id") or ""),
            )
        raise MachineRegistryError(
            f"desktop commander is unavailable on machine {machine.machine_id}"
        )

    async def machine_snapshot(self) -> dict[str, Any]:
        self.registry.sync_trusted_devices()
        snapshot = await self.registry.snapshot()
        local_id = self.registry.local_machine_id
        for item in snapshot.get("machines", []):
            if item.get("id") != local_id:
                continue
            health = item.setdefault("provider_health", {})
            if "desktop_commander" in item.get("providers", {}):
                try:
                    detail = await self.integrations.detail("desktop-commander")
                    health["desktop_commander"] = {
                        "configured": True,
                        "ready": bool(detail.get("stage") == "ready" and not detail.get("blocked")),
                        "mode": "local",
                        "stage": detail.get("stage"),
                        "blocked": bool(detail.get("blocked")),
                        "backend_tool_count": int(detail.get("backend_tool_count") or 0),
                    }
                except Exception as exc:
                    health["desktop_commander"] = {
                        "configured": True,
                        "ready": False,
                        "mode": "local",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            if "computer_use" in item.get("providers", {}):
                try:
                    status = await self.computer.status()
                    health["computer_use"] = {
                        "configured": bool(status.get("configured")),
                        "ready": bool(status.get("configured") and status.get("transport_ready", True)),
                        "mode": "local",
                        "runtime_mode": status.get("runtime_mode"),
                        "gpu_mode": status.get("gpu_mode"),
                    }
                except Exception as exc:
                    health["computer_use"] = {
                        "configured": True,
                        "ready": False,
                        "mode": "local",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            item["online"] = bool(item.get("enabled") and any(v.get("ready") for v in health.values()))

        for item in snapshot.get("machines", []):
            node_id = str(item.get("id") or "")
            if not node_id:
                continue
            item["routing_availability"] = self._availability(node_id)
            item["advertised_capabilities"] = self._node_facet_data(
                node_id,
                "capabilities.advertised",
            )

        snapshot["online_count"] = sum(bool(item.get("online")) for item in snapshot.get("machines", []))
        return snapshot

    async def bind_session(self, db: Any, session_id: str, machine_id: str, *, actor: str) -> dict[str, Any]:
        return await self.registry.bind_session(db, session_id, machine_id, actor=actor)

    def session_machine(self, session: dict[str, Any]) -> dict[str, Any]:
        return self.registry.machine_for_session(session).public()

    def machine_policy(self, machine_id: str) -> dict[str, Any]:
        machine = self.registry.get(machine_id)
        return {
            "machine": machine.public(),
            "policy": dict(machine.policy),
        }

    async def _computer_descriptor_for_machine(
        self,
        machine: Any,
        managed_session_id: str,
    ) -> dict[str, Any]:
        provider = dict(machine.providers.get("computer_use") or {})
        mode = str(provider.get("mode") or "").strip()
        if mode == "local":
            if not machine.local:
                raise MachineRegistryError("non-local machine cannot use local Computer Use")
            return await self.computer.descriptor(managed_session_id)
        if mode == "agent":
            remote = await self.registry.remote_computer_descriptor(
                machine,
                session_id=managed_session_id,
            )
            status = await self.computer.status()
            descriptor = dict(remote["descriptor"])
            descriptor.update(
                {
                    "viewer_url": "/computer/novnc/vnc.html",
                    "websocket_path": f"/api/computer/vnc/ws/{managed_session_id}",
                    "novnc_available": bool(status.get("novnc_available")),
                    "auth_required": bool(status.get("auth_required")),
                }
            )
            return descriptor
        raise MachineRegistryError(
            f"Computer Use backend is unavailable for machine {machine.machine_id}"
        )

    def require_local_computer(self, session: dict[str, Any]) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        provider = dict(machine.providers.get("computer_use") or {})
        if str(provider.get("mode") or "") != "local" or not machine.local:
            raise MachineRegistryError(
                f"Computer Use backend is unavailable for machine {machine.machine_id}"
            )
        return machine.public()

    async def computer_descriptor(self, managed_session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        descriptor = await self._computer_descriptor_for_machine(machine, managed_session_id)
        descriptor["machine"] = machine.public()
        return descriptor


    async def computer_monitors(self, session: dict[str, Any]) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        provider = dict(machine.providers.get("computer_use") or {})
        if str(provider.get("mode") or "") != "agent":
            raise MachineRegistryError(
                f"remote monitor selection is unavailable for machine {machine.machine_id}"
            )
        payload = await self.registry.remote_computer_monitors(machine)
        payload["machine"] = machine.public()
        return payload

    async def select_computer_monitor(
        self,
        session: dict[str, Any],
        mode: str,
    ) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        provider = dict(machine.providers.get("computer_use") or {})
        if str(provider.get("mode") or "") != "agent":
            raise MachineRegistryError(
                f"remote monitor selection is unavailable for machine {machine.machine_id}"
            )
        payload = await self.registry.set_remote_computer_monitor(
            machine,
            mode=mode,
        )
        payload["machine"] = machine.public()
        return payload


    async def browser_profiles(self, session: dict[str, Any]) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        payload = await self.registry.remote_browser_profiles(machine)
        payload["machine"] = machine.public()
        return payload

    async def set_browser_profile_alias(
        self,
        session: dict[str, Any],
        *,
        alias: str,
        profile: str,
    ) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        payload = await self.registry.set_remote_browser_profile_alias(
            machine,
            alias=alias,
            profile=profile,
        )
        payload["machine"] = machine.public()
        return payload

    async def rename_browser_profile_alias(
        self,
        session: dict[str, Any],
        *,
        alias: str,
        new_alias: str,
    ) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        payload = await self.registry.rename_remote_browser_profile_alias(
            machine,
            alias=alias,
            new_alias=new_alias,
        )
        payload["machine"] = machine.public()
        return payload

    async def launch_browser_profile(
        self,
        session: dict[str, Any],
        *,
        profile: str,
        url: str | None,
        monitor: str | None,
    ) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        payload = await self.registry.launch_remote_browser_profile(
            machine,
            profile=profile,
            url=url,
            monitor=monitor,
        )
        payload["machine"] = machine.public()
        return payload


    async def computer_transport(
        self,
        managed_session_id: str,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        provider = dict(machine.providers.get("computer_use") or {})
        mode = str(provider.get("mode") or "").strip()
        if mode == "local":
            if not machine.local:
                raise MachineRegistryError("non-local machine cannot use local Computer Use")
            if self.computer.session_isolation_enabled:
                host, port = await self.computer.tcp_target(managed_session_id)
                return {
                    "mode": "tcp",
                    "host": host,
                    "port": port,
                    "machine_id": machine.machine_id,
                }
            return {
                "mode": "websocket",
                "url": self.computer.websocket_target(),
                "machine_id": machine.machine_id,
            }
        if mode == "agent":
            remote = await self.registry.remote_computer_descriptor(
                machine,
                session_id=managed_session_id,
            )
            return {
                "mode": "websocket",
                "url": remote["websocket_url"],
                "machine_id": machine.machine_id,
            }
        raise MachineRegistryError(
            f"Computer Use backend is unavailable for machine {machine.machine_id}"
        )
