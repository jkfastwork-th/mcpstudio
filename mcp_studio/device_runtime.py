from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform as platform_module
import socket
from pathlib import Path
from typing import Any, Callable

import httpx
import websockets
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .desktop_commander_relay import AgentError, DesktopCommanderRelay
from .node_backends import normalize_backend_probe_specs, probe_configured_backends
from .node_hardware import probe_hardware_resources
from .node_models import probe_configured_models
from .node_capabilities import build_capability_advertisement


class DeviceRuntimeError(RuntimeError):
    pass


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DeviceIdentity:
    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self.private_key = private_key

    @property
    def public_key(self) -> str:
        raw = self.private_key.public_key().public_bytes_raw()
        return "ed25519:" + _b64url(raw)

    @property
    def device_id(self) -> str:
        raw = self.private_key.public_key().public_bytes_raw()
        return "dev_" + hashlib.sha256(raw).hexdigest()[:24]

    def sign(self, value: str) -> str:
        return _b64url(self.private_key.sign(str(value).encode("utf-8")))


class DeviceIdentityStore:
    """Portable fallback identity store.

    P5/P6/P7 may replace this adapter with DPAPI/TPM, Secret Service, Keychain,
    or another native store without changing the Device Runtime protocol.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)

    def load_or_create(self) -> DeviceIdentity:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            identity = DeviceIdentity(Ed25519PrivateKey.generate())
            raw = identity.private_key.private_bytes_raw()
            try:
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
            except FileExistsError:
                raw = self.path.read_bytes()
                if len(raw) != 32:
                    raise DeviceRuntimeError("device_identity_invalid")
                return DeviceIdentity(Ed25519PrivateKey.from_private_bytes(raw))
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            return identity
        except OSError as exc:
            raise DeviceRuntimeError(f"device_identity_unavailable:{exc}") from exc

        if len(raw) != 32:
            raise DeviceRuntimeError("device_identity_invalid")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return DeviceIdentity(Ed25519PrivateKey.from_private_bytes(raw))


def _platform_name(value: str | None = None) -> str:
    name = str(value or platform_module.system() or "unknown").strip().lower()
    return {
        "darwin": "macos",
        "windows": "windows",
        "linux": "linux",
    }.get(name, name)


class DeviceRuntimeConfig:
    def __init__(
        self,
        *,
        control_plane_url: str,
        name: str,
        identity_path: str,
        desktop_commander_command: list[str],
        workspaces: dict[str, str],
        capabilities: list[str],
        platform: str,
        architecture: str,
        runtime_version: str,
        backend_probes: list[dict[str, Any]],
        desktop_commander_timeout_seconds: float,
        heartbeat_seconds: float,
        reconnect_min_seconds: float,
        reconnect_max_seconds: float,
    ) -> None:
        self.control_plane_url = control_plane_url
        self.name = name
        self.identity_path = identity_path
        self.desktop_commander_command = desktop_commander_command
        self.workspaces = workspaces
        self.capabilities = capabilities
        self.platform = platform
        self.architecture = architecture
        self.runtime_version = runtime_version
        self.backend_probes = backend_probes
        self.desktop_commander_timeout_seconds = desktop_commander_timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        base_dir: str | Path,
    ) -> "DeviceRuntimeConfig":
        base = Path(base_dir).expanduser().resolve(strict=False)
        control_plane_url = str(raw.get("control_plane_url") or "").strip().rstrip("/")
        if not control_plane_url.startswith(("http://", "https://")):
            raise DeviceRuntimeError("control_plane_url_required")

        identity_value = str(raw.get("identity_path") or ".hirda/device-key.pem").strip()
        identity_path = Path(identity_value).expanduser()
        if not identity_path.is_absolute():
            identity_path = base / identity_path
        identity_path = identity_path.resolve(strict=False)

        command = raw.get("desktop_commander_command")
        if not isinstance(command, list) or not command:
            raise DeviceRuntimeError("desktop_commander_command_required")
        normalized_command = [str(part).strip() for part in command]
        if not all(normalized_command):
            raise DeviceRuntimeError("desktop_commander_command_required")

        workspaces: dict[str, str] = {}
        workspace_raw = raw.get("workspaces")
        if isinstance(workspace_raw, dict):
            for key, value in workspace_raw.items():
                workspace_key = str(key or "").strip()
                local_value = str(value or "").strip()
                if not workspace_key or not local_value:
                    continue
                local_path = Path(local_value).expanduser()
                if not local_path.is_absolute():
                    local_path = base / local_path
                workspaces[workspace_key] = str(local_path.resolve(strict=False))

        capabilities = sorted(
            {
                str(value or "").strip().lower()
                for value in (raw.get("capabilities") or ["filesystem", "process"])
                if str(value or "").strip()
            }
        )
        backend_probes = normalize_backend_probe_specs(raw.get("backend_probes"))
        reconnect_min = max(0.25, float(raw.get("reconnect_min_seconds") or 1.0))
        reconnect_max = max(
            reconnect_min,
            float(raw.get("reconnect_max_seconds") or 30.0),
        )
        return cls(
            control_plane_url=control_plane_url,
            name=str(raw.get("name") or socket.gethostname() or "hirda-device").strip(),
            identity_path=str(identity_path),
            desktop_commander_command=normalized_command,
            workspaces=workspaces,
            capabilities=capabilities,
            platform=_platform_name(str(raw.get("platform") or "") or None),
            architecture=str(
                raw.get("architecture") or platform_module.machine() or "unknown"
            ).strip().lower(),
            runtime_version=str(raw.get("runtime_version") or "0.1").strip(),
            backend_probes=backend_probes,
            desktop_commander_timeout_seconds=max(
                1.0,
                float(raw.get("desktop_commander_timeout_seconds") or 15.0),
            ),
            heartbeat_seconds=max(5.0, float(raw.get("heartbeat_seconds") or 15.0)),
            reconnect_min_seconds=reconnect_min,
            reconnect_max_seconds=reconnect_max,
        )

    @classmethod
    def load(cls, path: str | Path) -> "DeviceRuntimeConfig":
        config_path = Path(path).expanduser().resolve(strict=False)
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DeviceRuntimeError(f"device_runtime_config_invalid:{exc}") from exc
        if not isinstance(raw, dict):
            raise DeviceRuntimeError("device_runtime_config_must_be_object")
        return cls.from_dict(raw, base_dir=config_path.parent)


class WorkspaceResolver:
    def __init__(self, workspaces: dict[str, str]) -> None:
        self.workspaces = {
            str(key): str(Path(value).expanduser().resolve(strict=False))
            for key, value in workspaces.items()
        }

    def resolve(self, workspace_key: str) -> str:
        key = str(workspace_key or "").strip()
        root = self.workspaces.get(key)
        if not root:
            raise DeviceRuntimeError(f"workspace_not_configured:{key}")
        return root


class DesktopCommanderBackend:
    def __init__(
        self,
        relay: DesktopCommanderRelay,
        resolver: WorkspaceResolver,
    ) -> None:
        self.relay = relay
        self.resolver = resolver

    async def call(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        workspace_key: str,
        session_id: str,
    ) -> dict[str, Any]:
        root = self.resolver.resolve(workspace_key)
        try:
            result = await asyncio.to_thread(
                self.relay.call,
                str(name),
                dict(arguments),
                root,
                str(session_id),
            )
        except AgentError as exc:
            raise DeviceRuntimeError(str(exc)) from exc
        if not isinstance(result, dict):
            raise DeviceRuntimeError("desktop_commander_invalid_result")
        return result

    async def close(self) -> None:
        await asyncio.to_thread(self.relay.stop)


class DevicePairingClient:
    def __init__(
        self,
        config: DeviceRuntimeConfig,
        identity: DeviceIdentity,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.identity = identity
        self._client = http_client
        self._owns_client = http_client is None

    async def _client_for_call(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.control_plane_url,
                timeout=15.0,
            )
        return self._client

    async def start(self) -> dict[str, Any]:
        client = await self._client_for_call()
        response = await client.post(
            "/api/device-pairing/sessions",
            json={
                "name": self.config.name,
                "platform": self.config.platform,
                "architecture": self.config.architecture,
                "public_key": self.identity.public_key,
                "capabilities": list(self.config.capabilities),
                "runtime_version": self.config.runtime_version,
            },
        )
        if response.status_code >= 400:
            raise DeviceRuntimeError(
                f"pairing_start_failed:{response.status_code}:{response.text[:500]}"
            )
        result = response.json()
        if not isinstance(result, dict):
            raise DeviceRuntimeError("pairing_start_invalid_response")
        return result

    async def status(self, session_id: str) -> dict[str, Any]:
        client = await self._client_for_call()
        response = await client.get(f"/api/device-pairing/sessions/{session_id}")
        if response.status_code == 404:
            raise DeviceRuntimeError("pairing_session_not_found")
        if response.status_code >= 400:
            raise DeviceRuntimeError(
                f"pairing_status_failed:{response.status_code}:{response.text[:500]}"
            )
        result = response.json()
        if not isinstance(result, dict):
            raise DeviceRuntimeError("pairing_status_invalid_response")
        return result

    async def wait_for_trust(
        self,
        session_id: str,
        *,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 330.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        previous_state: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            status = await self.status(session_id)
            state = str(status.get("state") or "")
            if on_status is not None and state != previous_state:
                on_status(status)
            previous_state = state
            if state == "trusted":
                return status
            if state in {"rejected", "expired", "locked"}:
                raise DeviceRuntimeError(f"pairing_terminal_state:{state}")
            await asyncio.sleep(max(0.25, poll_seconds))
        raise DeviceRuntimeError("pairing_wait_timeout")

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None


class DeviceRuntime:
    def __init__(
        self,
        config: DeviceRuntimeConfig,
        *,
        identity: DeviceIdentity | None = None,
        backend: Any | None = None,
    ) -> None:
        self.config = config
        self.identity = identity or DeviceIdentityStore(config.identity_path).load_or_create()
        resolver = WorkspaceResolver(config.workspaces)
        if backend is None:
            relay = DesktopCommanderRelay(
                config.desktop_commander_command,
                self.identity.device_id,
                timeout_seconds=config.desktop_commander_timeout_seconds,
            )
            backend = DesktopCommanderBackend(relay, resolver)
        self.backend = backend
        self._pairing = DevicePairingClient(config, self.identity)
        self._stopping = asyncio.Event()
        self._websocket: Any | None = None

    @property
    def broker_url(self) -> str:
        if self.config.control_plane_url.startswith("https://"):
            base = "wss://" + self.config.control_plane_url[len("https://") :]
        else:
            base = "ws://" + self.config.control_plane_url[len("http://") :]
        return (
            base.rstrip("/")
            + "/api/device-broker/ws?device_id="
            + self.identity.device_id
        )

    def pairing_client(self) -> DevicePairingClient:
        return self._pairing

    def authenticate_message(self, challenge: dict[str, Any]) -> dict[str, Any]:
        if challenge.get("type") != "challenge":
            raise DeviceRuntimeError("broker_challenge_required")
        if str(challenge.get("device_id") or "") != self.identity.device_id:
            raise DeviceRuntimeError("broker_device_id_mismatch")
        challenge_id = str(challenge.get("challenge_id") or "")
        signing_input = str(challenge.get("signing_input") or "")
        if not challenge_id or not signing_input:
            raise DeviceRuntimeError("broker_challenge_invalid")
        return {
            "type": "authenticate",
            "challenge_id": challenge_id,
            "signature": self.identity.sign(signing_input),
        }

    async def dispatch_rpc(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = str(message.get("request_id") or "")
        method = str(message.get("method") or "")
        try:
            if not request_id:
                raise DeviceRuntimeError("rpc_request_id_required")
            if method != "desktop_commander.call":
                raise DeviceRuntimeError(f"unsupported_rpc_method:{method}")
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise DeviceRuntimeError("rpc_payload_must_be_object")
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                raise DeviceRuntimeError("desktop_commander_arguments_must_be_object")
            workspace_key = str(payload.get("workspace_key") or "")
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise DeviceRuntimeError("managed_session_id_required")
            result = await self.backend.call(
                name=str(payload.get("name") or ""),
                arguments=arguments,
                workspace_key=workspace_key,
                session_id=session_id,
            )
            return {
                "type": "rpc_response",
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except Exception as exc:
            error = str(exc)
            if len(error) > 500:
                error = error[:500]
            return {
                "type": "rpc_response",
                "request_id": request_id,
                "ok": False,
                "error": error,
            }

    def node_observations(self) -> dict[str, Any]:
        storage_path = Path(self.config.identity_path).parent
        backends = probe_configured_backends(self.config.backend_probes)
        models = probe_configured_models(self.config.backend_probes)
        capabilities = build_capability_advertisement(
            self.config.capabilities,
            backends,
            models,
            backend_specs=self.config.backend_probes,
        )
        return {
            "hardware.resources": probe_hardware_resources(storage_path),
            "runtime.backends": backends,
            "runtime.models": models,
            "capabilities.advertised": capabilities,
        }

    async def _run_connection(self) -> None:
        async with websockets.connect(
            self.broker_url,
            max_size=1024 * 1024,
            ping_interval=None,
            close_timeout=5,
        ) as websocket:
            self._websocket = websocket
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                challenge = json.loads(raw)
                await websocket.send(
                    json.dumps(
                        self.authenticate_message(challenge),
                        separators=(",", ":"),
                    )
                )
                raw = await asyncio.wait_for(websocket.recv(), timeout=30.0)
                authenticated = json.loads(raw)
                if (
                    not isinstance(authenticated, dict)
                    or authenticated.get("type") != "authenticated"
                ):
                    raise DeviceRuntimeError("broker_authentication_failed")

                await websocket.send(
                    json.dumps(
                        {
                            "type": "node_observations",
                            "observations": self.node_observations(),
                        },
                        separators=(",", ":"),
                    )
                )

                while not self._stopping.is_set():
                    try:
                        raw = await asyncio.wait_for(
                            websocket.recv(),
                            timeout=self.config.heartbeat_seconds,
                        )
                    except TimeoutError:
                        await websocket.send(
                            json.dumps({"type": "heartbeat"}, separators=(",", ":"))
                        )
                        continue
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                    kind = str(message.get("type") or "")
                    if kind in {"heartbeat_ack", "pong"}:
                        continue
                    if kind == "rpc_request":
                        response = await self.dispatch_rpc(message)
                        await websocket.send(
                            json.dumps(response, separators=(",", ":"))
                        )
            finally:
                self._websocket = None

    async def run_forever(
        self,
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._stopping.clear()
        delay = self.config.reconnect_min_seconds
        while not self._stopping.is_set():
            try:
                await self._run_connection()
                delay = self.config.reconnect_min_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping.is_set():
                    break
                if on_error is not None:
                    on_error(exc)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(
                    self.config.reconnect_max_seconds,
                    max(self.config.reconnect_min_seconds, delay * 2),
                )

    async def stop(self) -> None:
        self._stopping.set()
        websocket = self._websocket
        if websocket is not None:
            try:
                await websocket.close(code=1000, reason="HIRDA device runtime stopping")
            except Exception:
                pass
        await self._pairing.close()
        close = getattr(self.backend, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
