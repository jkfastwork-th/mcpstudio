from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MachineRegistryError(RuntimeError):
    pass


_MACHINE_PERMISSION_KEYS = ("read", "write", "execute", "destructive")
_DEFAULT_MACHINE_POLICY: dict[str, Any] = {
    "read": True,
    "write": True,
    "execute": True,
    "destructive": False,
    "fail_closed_unknown": True,
}
_DYNAMIC_DEVICE_POLICY: dict[str, Any] = {
    "read": True,
    "write": False,
    "execute": False,
    "destructive": False,
    "fail_closed_unknown": True,
}


def _normalize_machine_policy(raw: object) -> dict[str, Any]:
    policy = dict(_DEFAULT_MACHINE_POLICY)
    if raw is None:
        return policy
    if not isinstance(raw, dict):
        raise MachineRegistryError("machine policy must be an object")
    for key in _MACHINE_PERMISSION_KEYS:
        if key not in raw:
            continue
        if not isinstance(raw[key], bool):
            raise MachineRegistryError(f"machine policy {key} must be boolean")
        policy[key] = raw[key]
    if "fail_closed_unknown" in raw:
        if not isinstance(raw["fail_closed_unknown"], bool):
            raise MachineRegistryError("machine policy fail_closed_unknown must be boolean")
        policy["fail_closed_unknown"] = raw["fail_closed_unknown"]
    return policy


@dataclass(frozen=True, slots=True)
class MachinePolicyDecision:
    allowed: bool
    category: str
    code: str | None
    message: str
    machine_id: str
    policy: dict[str, Any]


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
    policy: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULT_MACHINE_POLICY))
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
            "policy": dict(self.policy),
            "metadata": dict(self.metadata),
        }


class MachineRegistry:
    """Config-backed machine inventory with live provider health checks.

    Secrets are referenced by env or token files and never returned by public
    snapshots. The registry intentionally does not discover arbitrary hosts:
    every machine remains operator-declared and fail-closed.
    """

    def __init__(
        self,
        studio: Any,
        *,
        base_dir: Path | None = None,
        device_pairing: Any | None = None,
        device_broker: Any | None = None,
    ) -> None:
        self.studio = studio
        self.base_dir = (base_dir or Path.cwd()).resolve(strict=False)
        self.device_pairing = device_pairing
        self.device_broker = device_broker
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
                    "hirda_native": {"mode": "local"},
                    "computer_use": {"mode": "local"},
                },
                policy=dict(_DEFAULT_MACHINE_POLICY),
            )
        self.sync_trusted_devices()

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
            policy=_normalize_machine_policy(raw.get("policy")),
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

    def contains(self, machine_id: str) -> bool:
        return str(machine_id or "").strip() in self._machines

    def register_machine(
        self, raw: dict[str, Any], *, source: str = "runtime", replace: bool = False
    ) -> MachineDescriptor:
        machine = self._parse(raw)
        if machine.machine_id in self._machines and not replace:
            raise MachineRegistryError(f"machine already registered: {machine.machine_id}")
        metadata = dict(machine.metadata)
        metadata.setdefault("registry_source", source)
        machine.metadata = metadata
        self._machines[machine.machine_id] = machine
        return machine

    def list(self) -> list[MachineDescriptor]:
        return [self._machines[key] for key in sorted(self._machines)]


    def sync_trusted_devices(self) -> None:
        pairing = self.device_pairing
        if pairing is None or not bool(getattr(pairing, "enabled", False)):
            return
        snapshot = pairing.list_devices()
        desired: set[str] = set()
        for raw in snapshot.get("devices", []):
            if not isinstance(raw, dict) or raw.get("trust_state") != "trusted":
                continue
            device_id = str(raw.get("device_id") or "").strip()
            if not device_id or device_id == self.local_machine_id:
                continue
            desired.add(device_id)
            existing = self._machines.get(device_id)
            if (
                existing is not None
                and existing.metadata.get("registry_source") != "device_pairing"
            ):
                # A static/operator machine always wins over dynamic materialization.
                continue

            capabilities = tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in (raw.get("capabilities") or [])
                    if str(item).strip()
                )
            )
            providers: dict[str, dict[str, Any]] = {
                "device_broker": {"mode": "broker"},
            }
            if any(item in {"filesystem", "process"} for item in capabilities):
                providers["desktop_commander"] = {"mode": "broker", "fallback": True}
                if "hirda_native" in capabilities:
                    providers["hirda_native"] = {"mode": "broker"}

            effective_policy = dict(_DYNAMIC_DEVICE_POLICY)
            raw_policy = raw.get("policy")
            if isinstance(raw_policy, dict):
                for key in (*_MACHINE_PERMISSION_KEYS, "fail_closed_unknown"):
                    if key in raw_policy and isinstance(raw_policy[key], bool):
                        effective_policy[key] = raw_policy[key]

            self.register_machine(
                {
                    "id": device_id,
                    "name": str(raw.get("name") or device_id),
                    "enabled": True,
                    "local": False,
                    "os": raw.get("platform"),
                    "address": None,
                    "capabilities": list(capabilities),
                    "providers": providers,
                    "workspace_map": {},
                    "policy": effective_policy,
                    "metadata": {
                        "registry_source": "device_pairing",
                        "dynamic": True,
                        "device_id": device_id,
                        "fingerprint": raw.get("fingerprint"),
                        "runtime_version": raw.get("runtime_version"),
                        "trust_state": raw.get("trust_state"),
                        "paired_at": raw.get("paired_at"),
                    },
                },
                source="device_pairing",
                replace=True,
            )

        for machine_id, machine in list(self._machines.items()):
            if (
                machine.metadata.get("registry_source") == "device_pairing"
                and machine_id not in desired
            ):
                self._machines.pop(machine_id, None)

    def machine_for_session(self, session: dict[str, Any]) -> MachineDescriptor:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        machine_id = str(metadata.get("machine_id") or self.local_machine_id)
        return self.get(machine_id)

    def authorize_session(
        self,
        session: dict[str, Any],
        category: str,
    ) -> MachinePolicyDecision:
        machine = self.machine_for_session(session)
        policy = dict(machine.policy)
        normalized = str(category or "unknown").strip().lower() or "unknown"
        if normalized == "unknown":
            allowed = not bool(policy.get("fail_closed_unknown", True))
            return MachinePolicyDecision(
                allowed=allowed,
                category=normalized,
                code=None if allowed else "MACHINE_PERMISSION_UNCLASSIFIED",
                message=(
                    "Unclassified machine capability allowed by machine policy."
                    if allowed
                    else f"Machine {machine.machine_id} blocks unclassified capabilities."
                ),
                machine_id=machine.machine_id,
                policy=policy,
            )
        if normalized not in _MACHINE_PERMISSION_KEYS:
            return MachinePolicyDecision(
                allowed=False,
                category=normalized,
                code="MACHINE_PERMISSION_UNCLASSIFIED",
                message=f"Machine {machine.machine_id} has no policy class for {normalized}.",
                machine_id=machine.machine_id,
                policy=policy,
            )
        allowed = bool(policy.get(normalized, False))
        return MachinePolicyDecision(
            allowed=allowed,
            category=normalized,
            code=None if allowed else "MACHINE_PERMISSION_DENIED",
            message=(
                f"{normalized.upper()} permission granted by machine {machine.machine_id}."
                if allowed
                else f"{normalized.upper()} permission is disabled for machine {machine.machine_id}."
            ),
            machine_id=machine.machine_id,
            policy=policy,
        )

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
        if mode == "broker":
            if self.device_broker is None:
                return {"configured": True, "ready": False, "mode": mode, "error": "device_broker_unavailable"}
            status = await self.device_broker.connection_status(machine.machine_id)
            return {
                "configured": True,
                "ready": bool(status.get("connected")),
                "mode": mode,
                "connection": status,
            }
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
            ready = bool(body.get("ok"))
            if provider_name == "computer_use":
                ready = bool(ready and body.get("computer_use"))
            elif provider_name == "desktop_commander":
                ready = bool(ready and body.get("desktop_commander", True))
            elif provider_name == "hirda_native":
                ready = bool(ready and body.get("hirda_native", True))
            return {
                "configured": True,
                "ready": ready,
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
        native_provider = dict(machine.providers.get("hirda_native") or {})
        desktop_provider = dict(machine.providers.get("desktop_commander") or {})
        broker_managed = "broker" in {
            str(native_provider.get("mode") or ""),
            str(desktop_provider.get("mode") or ""),
        }
        if not broker_managed:
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
        provider_name: str = "hirda_native",
    ) -> dict[str, Any]:
        provider = dict(machine.providers.get(provider_name) or {})
        mode = str(provider.get("mode") or "").strip()

        if mode == "broker":
            if self.device_broker is None:
                raise MachineRegistryError(
                    f"device broker is unavailable for {machine.machine_id}"
                )
            try:
                result = await self.device_broker.call_device(
                    machine.machine_id,
                    method="hirda_native.call" if provider_name == "hirda_native" else "desktop_commander.call",
                    payload={
                        "name": tool_name,
                        "arguments": dict(arguments or {}),
                        "workspace_key": str(workspace_key or ""),
                        "session_id": str(session_id or ""),
                    },
                    timeout_seconds=float(provider.get("timeout_seconds") or 20.0),
                )
            except Exception as exc:
                raise MachineRegistryError(
                    f"broker machine tool failed: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(result, dict):
                raise MachineRegistryError("broker machine returned invalid tool result")
            return result

        if mode != "agent":
            raise MachineRegistryError(
                f"machine computer provider is unavailable on {machine.machine_id}"
            )

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
            raise MachineRegistryError(
                str((payload or {}).get("error") or "remote machine tool failed")
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MachineRegistryError("remote machine returned invalid tool result")
        return result


    @staticmethod
    def _validate_remote_computer_websocket_url(
        machine: MachineDescriptor,
        websocket_url: str,
    ) -> str:
        value = str(websocket_url or "").strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise MachineRegistryError("remote computer websocket URL is invalid")
        if parsed.username or parsed.password:
            raise MachineRegistryError("remote computer websocket URL must not contain credentials")
        machine_host = str(machine.address or "").strip().strip("[]").lower()
        if not machine_host or parsed.hostname.lower() != machine_host:
            raise MachineRegistryError(
                f"remote computer websocket host mismatch for machine {machine.machine_id}"
            )
        return value

    async def remote_computer_descriptor(
        self,
        machine: MachineDescriptor,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        provider = dict(machine.providers.get("computer_use") or {})
        if str(provider.get("mode") or "") != "agent":
            raise MachineRegistryError(f"computer use agent is unavailable on {machine.machine_id}")
        endpoint = str(provider.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise MachineRegistryError("computer use endpoint is unavailable")
        if not session_id:
            raise MachineRegistryError("managed session id is required for remote computer use")
        timeout = float(provider.get("timeout_seconds") or 10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                endpoint + "/v1/computer/descriptor",
                headers=self._provider_headers(provider),
                json={"session_id": session_id},
            )
        if response.status_code >= 400:
            raise MachineRegistryError(
                f"remote computer descriptor failed ({response.status_code}): {response.text[:500]}"
            )
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise MachineRegistryError(
                str((payload or {}).get("error") or "remote computer descriptor failed")
            )
        descriptor = payload.get("descriptor")
        if not isinstance(descriptor, dict):
            raise MachineRegistryError("remote computer descriptor is invalid")
        websocket_url = self._validate_remote_computer_websocket_url(
            machine,
            str(payload.get("websocket_url") or ""),
        )
        safe_descriptor = {
            key: descriptor.get(key)
            for key in (
                "runtime_mode",
                "transport",
                "desktop_display",
                "cdp_port",
                "gpu_mode",
                "gpu_hardware_available",
                "gpu_presentation_mode",
            )
            if key in descriptor
        }
        return {
            "descriptor": safe_descriptor,
            "websocket_url": websocket_url,
        }
