from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .machine_registry import MachineRegistry, MachineRegistryError

_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_SAFE_DEFAULT_POLICY: dict[str, Any] = {
    "read": True,
    "write": False,
    "execute": False,
    "destructive": False,
    "fail_closed_unknown": True,
}
_ALLOWED_CAPABILITIES = {"filesystem", "process", "computer_use"}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MachineEnrollmentError(RuntimeError):
    pass


class MachineEnrollmentManager:
    """Discover HIRDA machine agents over Tailscale and gate enrollment.

    Discovery never grants authority. Unknown agents are persisted as pending
    candidates. Only explicit approval materializes a machine in the runtime
    registry. Approved records are persisted in an atomic sidecar so operator
    YAML remains untouched and wins on conflicts.
    """

    SCHEMA = "hirda-machine-enrollment-v1"
    AGENT_SCHEMA = "hirda-machine-agent-v1"

    def __init__(self, studio: Any, registry: MachineRegistry, *, base_dir: Path) -> None:
        self.studio = studio
        self.registry = registry
        self.base_dir = Path(base_dir).resolve(strict=False)
        self.enabled = bool(getattr(studio, "machine_enrollment_enabled", False))
        raw_path = str(getattr(studio, "machine_enrollment_state_path", "data/machine-enrollment.json") or "data/machine-enrollment.json")
        path = Path(raw_path).expanduser()
        self.state_path = path if path.is_absolute() else self.base_dir / path
        self.agent_port = int(getattr(studio, "machine_enrollment_agent_port", 8765) or 8765)
        self.probe_timeout = float(getattr(studio, "machine_enrollment_probe_timeout_seconds", 2.0) or 2.0)
        self.discovery_interval = float(getattr(studio, "machine_enrollment_discovery_interval_seconds", 60.0) or 60.0)
        self._task: asyncio.Task[None] | None = None
        self._discover_lock = asyncio.Lock()
        self.last_discovery_at: str | None = None
        self.last_error: str | None = None
        cidrs = list(getattr(studio, "machine_enrollment_allowed_cidrs", []) or ["100.64.0.0/10", "fd7a:115c:a1e0::/48"])
        self.allowed_networks = [ipaddress.ip_network(str(value), strict=False) for value in cidrs]
        configured_default = getattr(studio, "machine_enrollment_default_policy", None)
        self.default_policy = dict(_SAFE_DEFAULT_POLICY)
        if isinstance(configured_default, dict):
            for key in _SAFE_DEFAULT_POLICY:
                if key in configured_default:
                    self.default_policy[key] = configured_default[key]
        self._state = self._load_state()
        self._restore_approved()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "pending": {},
            "approved": {},
            "rejected": {},
            "updated_at": _utcnow(),
        }

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_state()
        except (OSError, json.JSONDecodeError) as exc:
            raise MachineEnrollmentError(f"machine enrollment state unavailable: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != self.SCHEMA:
            raise MachineEnrollmentError("machine enrollment state schema mismatch")
        state = self._empty_state()
        for key in ("pending", "approved", "rejected"):
            if isinstance(raw.get(key), dict):
                state[key] = dict(raw[key])
        state["updated_at"] = raw.get("updated_at") or _utcnow()
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = _utcnow()
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _restore_approved(self) -> None:
        for machine_id, approved in list(self._state.get("approved", {}).items()):
            if self.registry.contains(machine_id):
                continue
            record = approved.get("machine") if isinstance(approved, dict) else None
            if not isinstance(record, dict):
                continue
            try:
                self.registry.register_machine(record, source="enrollment")
            except Exception:
                # A malformed sidecar record must not break control-plane boot.
                continue

    def _address_allowed(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self.allowed_networks)

    @staticmethod
    def _identity_fingerprint(identity: dict[str, Any]) -> str:
        stable = {
            "schema": identity.get("schema"),
            "machine_id": identity.get("machine_id"),
            "hostname": identity.get("hostname"),
            "platform": identity.get("platform"),
            "architecture": identity.get("architecture"),
            "agent_version": identity.get("agent_version"),
            "capabilities": sorted(str(x) for x in identity.get("capabilities", []) if str(x)),
            "providers": identity.get("providers") if isinstance(identity.get("providers"), dict) else {},
        }
        raw = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _validate_identity(self, identity: dict[str, Any], *, address: str) -> dict[str, Any]:
        if identity.get("schema") != self.AGENT_SCHEMA:
            raise MachineEnrollmentError("unsupported machine agent identity schema")
        machine_id = str(identity.get("machine_id") or "").strip()
        if not _MACHINE_ID_RE.fullmatch(machine_id):
            raise MachineEnrollmentError("invalid machine agent id")
        if not self._address_allowed(address):
            raise MachineEnrollmentError(f"machine address outside enrollment networks: {address}")
        capabilities = [
            value for value in dict.fromkeys(str(x).strip() for x in identity.get("capabilities", []))
            if value in _ALLOWED_CAPABILITIES
        ]
        providers = identity.get("providers") if isinstance(identity.get("providers"), dict) else {}
        return {
            "schema": self.AGENT_SCHEMA,
            "machine_id": machine_id,
            "hostname": str(identity.get("hostname") or machine_id),
            "platform": str(identity.get("platform") or "unknown"),
            "architecture": str(identity.get("architecture") or "unknown"),
            "agent_version": str(identity.get("agent_version") or "unknown"),
            "capabilities": capabilities,
            "providers": providers,
            "address": address,
            "endpoint": self._agent_endpoint(address),
            "fingerprint": self._identity_fingerprint(identity),
        }

    def _agent_endpoint(self, address: str) -> str:
        host = f"[{address}]" if ":" in address else address
        return f"http://{host}:{self.agent_port}"

    def _tailscale_status_sync(self) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MachineEnrollmentError(f"tailscale status unavailable: {exc}") from exc
        if result.returncode != 0:
            raise MachineEnrollmentError(f"tailscale status failed: {result.stderr.strip()[:300]}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MachineEnrollmentError("tailscale status returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MachineEnrollmentError("tailscale status returned invalid payload")
        return payload

    async def _probe(self, address: str) -> dict[str, Any] | None:
        endpoint = self._agent_endpoint(address)
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout) as client:
                response = await client.get(endpoint + "/identity")
            if response.status_code != 200:
                return None
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            return self._validate_identity(payload, address=address)
        except Exception:
            return None

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._discovery_loop(), name="hirda-machine-enrollment")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _discovery_loop(self) -> None:
        while True:
            try:
                await self.discover()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(self.discovery_interval)

    async def discover(self) -> dict[str, Any]:
        if not self.enabled:
            raise MachineEnrollmentError("machine enrollment is disabled")
        async with self._discover_lock:
            return await self._discover_once()

    async def _discover_once(self) -> dict[str, Any]:
        status = await asyncio.to_thread(self._tailscale_status_sync)
        peers = status.get("Peer") if isinstance(status.get("Peer"), dict) else {}
        targets: list[tuple[str, str]] = []
        for peer in peers.values():
            if not isinstance(peer, dict) or not peer.get("Online"):
                continue
            hostname = str(peer.get("HostName") or "unknown")
            addresses = [str(x) for x in (peer.get("TailscaleIPs") or []) if self._address_allowed(str(x))]
            # Prefer IPv4 for the agent probe when available.
            addresses.sort(key=lambda value: (":" in value, value))
            if addresses:
                targets.append((hostname, addresses[0]))

        results = await asyncio.gather(*(self._probe(address) for _, address in targets))
        observed: list[dict[str, Any]] = []
        now = _utcnow()
        for (peer_hostname, address), identity in zip(targets, results):
            if identity is None:
                continue
            machine_id = identity["machine_id"]
            candidate = {
                **identity,
                "peer_hostname": peer_hostname,
                "first_seen_at": now,
                "last_seen_at": now,
            }
            if self.registry.contains(machine_id):
                candidate["status"] = "registered"
                observed.append(candidate)
                continue
            rejected = self._state["rejected"].get(machine_id)
            if isinstance(rejected, dict):
                rejected["last_seen_at"] = now
                rejected["address"] = address
                candidate["status"] = "rejected"
                observed.append(candidate)
                continue
            existing = self._state["pending"].get(machine_id)
            if isinstance(existing, dict):
                candidate["first_seen_at"] = existing.get("first_seen_at") or now
            candidate["status"] = "pending"
            self._state["pending"][machine_id] = candidate
            observed.append(candidate)
        self._save_state()
        self.last_discovery_at = now
        self.last_error = None
        return {
            "enabled": True,
            "observed": observed,
            "pending_count": len(self._state["pending"]),
            "registered_count": len(self.registry.list()),
            "scanned_peer_count": len(targets),
            "observed_at": now,
        }

    def snapshot(self) -> dict[str, Any]:
        def values(name: str) -> list[dict[str, Any]]:
            rows = []
            for machine_id, raw in sorted(self._state.get(name, {}).items()):
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                item.setdefault("machine_id", machine_id)
                item["status"] = name[:-1] if name.endswith("s") else name
                rows.append(item)
            return rows

        return {
            "enabled": self.enabled,
            "storage": "local-sidecar",
            "agent_port": self.agent_port,
            "discovery_interval_seconds": self.discovery_interval,
            "last_discovery_at": self.last_discovery_at,
            "last_error": self.last_error,
            "pending": values("pending"),
            "approved": values("approved"),
            "rejected": values("rejected"),
            "pending_count": len(self._state.get("pending", {})),
        }

    def approve(
        self,
        machine_id: str,
        *,
        name: str | None = None,
        workspace_map: dict[str, str] | None = None,
        policy: dict[str, Any] | None = None,
        actor: str = "operator",
    ) -> dict[str, Any]:
        if not self.enabled:
            raise MachineEnrollmentError("machine enrollment is disabled")
        key = str(machine_id or "").strip()
        candidate = self._state["pending"].get(key)
        if not isinstance(candidate, dict):
            raise KeyError(key)
        merged_policy = dict(self.default_policy)
        if policy:
            for field_name in _SAFE_DEFAULT_POLICY:
                if field_name in policy:
                    merged_policy[field_name] = policy[field_name]
        capabilities = [str(x) for x in candidate.get("capabilities", []) if str(x) in _ALLOWED_CAPABILITIES]
        providers: dict[str, dict[str, Any]] = {}
        advertised = candidate.get("providers") if isinstance(candidate.get("providers"), dict) else {}
        dc = advertised.get("desktop_commander") if isinstance(advertised.get("desktop_commander"), dict) else {}
        if dc.get("available"):
            providers["desktop_commander"] = {
                "mode": "agent",
                "endpoint": str(candidate["endpoint"]),
                "auth_mode": "tailnet_ip",
                "timeout_seconds": 20.0,
            }
        cu = advertised.get("computer_use") if isinstance(advertised.get("computer_use"), dict) else {}
        if cu.get("available"):
            providers["computer_use"] = {
                "mode": "agent",
                "endpoint": str(candidate["endpoint"]),
                "auth_mode": "tailnet_ip",
                "timeout_seconds": 10.0,
            }
        if not providers:
            raise MachineEnrollmentError("candidate has no approved HIRDA backend provider")
        record = {
            "id": key,
            "name": str(name or candidate.get("hostname") or key),
            "enabled": True,
            "local": False,
            "os": str(candidate.get("platform") or "unknown"),
            "address": str(candidate.get("address") or ""),
            "capabilities": capabilities,
            "providers": providers,
            "workspace_map": dict(workspace_map or {}),
            "policy": merged_policy,
            "metadata": {
                "enrollment": {
                    "source": "tailscale-discovery",
                    "approved_at": _utcnow(),
                    "approved_by": actor,
                    "identity_fingerprint": candidate.get("fingerprint"),
                    "agent_version": candidate.get("agent_version"),
                    "architecture": candidate.get("architecture"),
                }
            },
        }
        machine = self.registry.register_machine(record, source="enrollment")
        self._state["pending"].pop(key, None)
        self._state["rejected"].pop(key, None)
        self._state["approved"][key] = {
            "machine_id": key,
            "machine": record,
            "identity_fingerprint": candidate.get("fingerprint"),
            "approved_at": record["metadata"]["enrollment"]["approved_at"],
            "approved_by": actor,
        }
        self._save_state()
        return {"status": "approved", "machine": machine.public()}

    def reject(self, machine_id: str, *, reason: str = "operator-rejected", actor: str = "operator") -> dict[str, Any]:
        key = str(machine_id or "").strip()
        candidate = self._state["pending"].pop(key, None)
        if not isinstance(candidate, dict):
            raise KeyError(key)
        item = {
            **candidate,
            "status": "rejected",
            "rejected_at": _utcnow(),
            "rejected_by": actor,
            "reason": str(reason or "operator-rejected")[:500],
        }
        self._state["rejected"][key] = item
        self._save_state()
        return item
