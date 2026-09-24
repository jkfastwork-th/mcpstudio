from __future__ import annotations

from typing import Any

from .computer import ComputerUseManager
from .integrations import IntegrationManager, IntegrationError
from .machine_registry import MachineRegistry, MachineRegistryError


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
        return self.integrations.backend_tools()

    def backend_permission(self, name: str) -> str | None:
        return self.integrations.backend_permission(name)

    def is_backend_tool(self, name: str) -> bool:
        return self.integrations.is_backend_tool(name)

    def authorize_session(self, session: dict[str, Any], category: str):
        return self.registry.authorize_session(session, category)

    async def call_backend_tool(
        self,
        exposed_name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        machine_id = str(context.get("machine_id") or self.registry.local_machine_id)
        machine = self.registry.get(machine_id)
        route = self.integrations.backend_route(exposed_name)
        if route is None:
            raise IntegrationError(f"unknown backend tool: {exposed_name}")
        integration_id, raw_name = route
        if integration_id != "desktop-commander":
            return await self.integrations.call_backend_tool(exposed_name, arguments, context=context)
        provider = dict(machine.providers.get("desktop_commander") or {})
        mode = str(provider.get("mode") or "").strip()
        if mode == "local":
            if not machine.local:
                raise MachineRegistryError("non-local machine cannot use local desktop commander")
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

    def require_local_computer(self, session: dict[str, Any]) -> dict[str, Any]:
        machine = self.registry.machine_for_session(session)
        provider = dict(machine.providers.get("computer_use") or {})
        if str(provider.get("mode") or "") != "local" or not machine.local:
            raise MachineRegistryError(
                f"Computer Use backend is unavailable for machine {machine.machine_id}"
            )
        return machine.public()

    async def computer_descriptor(self, managed_session_id: str, session: dict[str, Any]) -> dict[str, Any]:
        machine = self.require_local_computer(session)
        descriptor = await self.computer.descriptor(managed_session_id)
        descriptor["machine"] = machine
        return descriptor
