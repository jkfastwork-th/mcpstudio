from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MachineRegistryError(RuntimeError):
    pass


@dataclass(slots=True)
class MachineDescriptor:
    machine_id: str
    name: str
    enabled: bool = True
    local: bool = False
    os_name: str | None = None
    address: str | None = None
    capabilities: tuple[str, ...] = ()
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    workspace_map: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        for name, raw in self.providers.items():
            item = dict(raw)
            item.pop("token", None)
            item.pop("token_file", None)
            providers[name] = item
        return {
            "id": self.machine_id,
            "name": self.name,
            "enabled": self.enabled,
            "local": self.local,
            "os": self.os_name,
            "address": self.address,
            "capabilities": list(self.capabilities),
            "providers": providers,
            "workspace_keys": sorted(self.workspace_map),
            "metadata": dict(self.metadata),
        }


class MachineRegistry:
    """Config-backed machine inventory with live provider health checks.

    Secrets are referenced by env or token files and never returned by public
    snapshots. The registry intentionally does not discover arbitrary hosts:
    every machine remains operator-declared and fail-closed.
    """

    def __init__(self, studio: Any, *, base_dir: Path | None = None) -> None:
        self.studio = studio
        self.base_dir = (base_dir or Path.cwd()).resolve(strict=False)
        configured = list(getattr(studio, "machine_registry", []) or [])
        local_id = str(getattr(studio, "machine_local_id", "openclaw") or "openclaw").strip()
        self.local_machine_id = local_id
        self._machines: dict[str, MachineDescriptor] = {}
        for raw in configured:
            if not isinstance(raw, dict):
                continue
            machine = self._parse(raw)
            if machine.machine_id in self._machines:
                raise MachineRegistryError(f"duplicate machine id: {machine.machine_id}")
            self._machines[machine.machine_id] = machine
        if local_id not in self._machines:
            self._machines[local_id] = MachineDescriptor(
                machine_id=local_id,
                name=socket.gethostname() or local_id,
                enabled=True,
                local=True,
                os_name=os.name,
                address="127.0.0.1",
                capabilities=("filesystem", "process", "computer_use"),
                providers={
                    "desktop_commander": {"mode": "local"},
                    "computer_use": {"mode": "local"},
                },
            )

    @staticmethod
    def _parse(raw: dict[str, Any]) -> MachineDescriptor:
        machine_id = str(raw.get("id") or "").strip()
        if not machine_id:
            raise MachineRegistryError("machine id is required")
        name = str(raw.get("name") or machine_id).strip() or machine_id
        providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
        workspace_map = raw.get("workspace_map") if isinstance(raw.get("workspace_map"), dict) else {}
        capabilities = tuple(
            dict.fromkeys(str(item).strip() for item in (raw.get("capabilities") or []) if str(item).strip())
        )
        return MachineDescriptor(
            machine_id=machine_id,
            name=name,
            enabled=bool(raw.get("enabled", True)),
            local=bool(raw.get("local", False)),
            os_name=(str(raw.get("os")) if raw.get("os") is not None else None),
            address=(str(raw.get("address")) if raw.get("address") is not None else None),
            capabilities=capabilities,
            providers={str(k): dict(v) for k, v in providers.items() if isinstance(v, dict)},
            workspace_map={str(k): str(v) for k, v in workspace_map.items() if str(k).strip() and str(v).strip()},
            metadata=dict(raw.get("metadata") or {}) if isinstance(raw.get("metadata"), dict) else {},
        )

    def get(self, machine_id: str) -> MachineDescriptor:
        key = str(machine_id or "").strip() or self.local_machine_id
        machine = self._machines.get(key)
        if machine is None:
            raise KeyError(key)
        if not machine.enabled:
            raise MachineRegistryError(f"machine disabled: {key}")
        return machine

    def list(self) -> list[MachineDescriptor]:
        return [self._machines[key] for key in sorted(self._machines)]

    def machine_for_session(self, session: dict[str, Any]) -> MachineDescriptor:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        machine_id = str(metadata.get("machine_id") or self.local_machine_id)
        return self.get(machine_id)

    def workspace_root(self, machine: MachineDescriptor, workspace_key: str, fallback_path: str | None = None) -> str:
        mapped = str(machine.workspace_map.get(str(workspace_key or "")) or "").strip()
        if mapped:
            return mapped
        if machine.local and fallback_path:
            return str(fallback_path)
        raise MachineRegistryError(
            f"machine workspace is not mapped: machine={machine.machine_id} workspace={workspace_key}"
        )

    def _provider_token(self, provider: dict[str, Any]) -> str:
        env_name = str(provider.get("token_env") or "").strip()
        if env_name:
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        token_file = str(provider.get("token_file") or "").strip()
        if token_file:
            path = Path(token_file).expanduser()
            if not path.is_absolute():
                path = self.base_dir / path
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise MachineRegistryError(f"machine provider token file unavailable: {path}") from exc
            if value:
                return value
        raise MachineRegistryError("machine provider token is unavailable")

    def _provider_headers(self, provider: dict[str, Any]) -> dict[str, str]:
        auth_mode = str(provider.get("auth_mode") or "token").strip().lower()
        if auth_mode == "tailnet_ip":
            return {}
        return {"Authorization": f"Bearer {self._provider_token(provider)}"}

    async def provider_health(self, machine: MachineDescriptor, provider_name: str) -> dict[str, Any]:
        provider = dict(machine.providers.get(provider_name) or {})
        mode = str(provider.get("mode") or "").strip()
        if not mode:
            return {"configured": False, "ready": False, "mode": None}
        if mode == "local":
            return {"configured": True, "ready": True, "mode": "local"}
        if mode != "agent":
            return {"configured": True, "ready": False, "mode": mode, "error": "unsupported_provider_mode"}
        endpoint = str(provider.get("endpoint") or "").rstrip("/")
        if not endpoint:
            return {"configured": True, "ready": False, "mode": mode, "error": "endpoint_missing"}
        try:
            timeout = float(provider.get("timeout_seconds") or 3.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    endpoint + "/health",
                    headers=self._provider_headers(provider),
                )
                response.raise_for_status()
                body = response.json()
            return {
                "configured": True,
                "ready": bool(body.get("ok")),
                "mode": mode,
                "endpoint": endpoint,
                "agent": body,
            }
        except Exception as exc:
            return {
                "configured": True,
                "ready": False,
                "mode": mode,
                "endpoint": endpoint,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def snapshot(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for machine in self.list():
            item = machine.public()
            health: dict[str, Any] = {}
            for provider_name in sorted(machine.providers):
                health[provider_name] = await self.provider_health(machine, provider_name)
            item["provider_health"] = health
            item["online"] = bool(machine.enabled and any(v.get("ready") for v in health.values()))
            items.append(item)
        return {
            "local_machine_id": self.local_machine_id,
            "machine_count": len(items),
            "online_count": sum(bool(item["online"]) for item in items),
            "observed_at": _utcnow(),
            "machines": items,
        }

    async def bind_session(
        self,
        db: Any,
        session_id: str,
        machine_id: str,
        *,
        actor: str = "operator",
    ) -> dict[str, Any]:
        machine = self.get(machine_id)
        session = await db.get_managed_session(session_id)
        self.workspace_root(
            machine,
            str(session.get("workspace_key") or ""),
            str(session.get("project_path") or "") or None,
        )
        metadata = dict(session.get("metadata") or {})
        metadata["machine_id"] = machine.machine_id
        metadata["machine_bound_at"] = _utcnow()
        metadata["machine_bound_by"] = actor
        updated = await db.update_managed_session_metadata(session_id, metadata)
        return {"session": updated, "machine": machine.public()}

    async def remote_call(
        self,
        machine: MachineDescriptor,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        workspace_key: str,
        project_path: str | None,
        session_id: str,
    ) -> dict[str, Any]:
        provider = dict(machine.providers.get("desktop_commander") or {})
        if str(provider.get("mode") or "") != "agent":
            raise MachineRegistryError(f"desktop commander agent is unavailable on {machine.machine_id}")
        endpoint = str(provider.get("endpoint") or "").rstrip("/")
        workspace_root = self.workspace_root(machine, workspace_key, project_path)
        timeout = float(provider.get("timeout_seconds") or 20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint + "/v1/tools/call",
                headers=self._provider_headers(provider),
                json={
                    "name": tool_name,
                    "arguments": dict(arguments or {}),
                    "workspace_root": workspace_root,
                    "session_id": session_id,
                },
            )
        if response.status_code >= 400:
            raise MachineRegistryError(
                f"remote machine tool failed ({response.status_code}): {response.text[:500]}"
            )
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise MachineRegistryError(str((payload or {}).get("error") or "remote machine tool failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MachineRegistryError("remote machine returned invalid tool result")
        return result
