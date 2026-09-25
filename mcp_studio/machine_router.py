from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .computer import ComputerUseManager
from .integrations import IntegrationManager, IntegrationError
from .machine_registry import MachineRegistry, MachineRegistryError
from .tool_permissions import workspace_scope_violation


_BROWSER_VISUAL_FALLBACK_TOOL = "hirda__browser__visual_fallback"


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
            try:
                result = await self.integrations.call_backend_tool(
                    exposed_name, arguments, context=context
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
        if mode == "agent":
            return await self.registry.remote_call(
                machine,
                tool_name=raw_name,
                arguments=arguments,
                workspace_key=str(context.get("workspace_key") or ""),
                project_path=(str(context.get("project_path")) if context.get("project_path") else None),
                session_id=str(context.get("managed_session_id") or ""),
            )
        raise MachineRegistryError(
            f"desktop commander is unavailable on machine {machine.machine_id}"
        )

    async def machine_snapshot(self) -> dict[str, Any]:
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
