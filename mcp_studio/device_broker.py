from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .device_pairing import DevicePairingError, DevicePairingManager


class DeviceBrokerError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise DeviceBrokerError("invalid_base64url") from exc


class DeviceBroker:
    """Authenticated outbound device connection broker.

    Devices initiate the WebSocket. HIRDA admits a connection only after the
    paired Ed25519 private key signs a fresh one-time server challenge.
    """

    SCHEMA = "hirda-device-broker-v1"
    SIGNING_PREFIX = "HIRDA_DEVICE_BROKER_V1"

    def __init__(self, studio: Any, pairing: DevicePairingManager) -> None:
        self.studio = studio
        self.pairing = pairing
        self.enabled = bool(getattr(studio, "device_broker_enabled", False))
        self.challenge_ttl_seconds = max(
            5,
            min(
                120,
                int(getattr(studio, "device_broker_challenge_ttl_seconds", 30) or 30),
            ),
        )
        self.session_ttl_seconds = max(
            60,
            min(
                86400,
                int(getattr(studio, "device_broker_session_ttl_seconds", 900) or 900),
            ),
        )
        self.heartbeat_timeout_seconds = max(
            10,
            min(
                300,
                int(getattr(studio, "device_broker_heartbeat_timeout_seconds", 45) or 45),
            ),
        )
        self.max_message_bytes = max(
            4096,
            min(
                4 * 1024 * 1024,
                int(getattr(studio, "device_broker_max_message_bytes", 262144) or 262144),
            ),
        )
        self._challenges: dict[str, dict[str, Any]] = {}
        self._connections: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise DeviceBrokerError("device_broker_disabled")

    def _trusted_device(self, device_id: str) -> dict[str, Any]:
        try:
            return self.pairing.trusted_device(device_id)
        except KeyError as exc:
            raise DeviceBrokerError("unknown_device") from exc
        except DevicePairingError as exc:
            raise DeviceBrokerError(str(exc)) from exc

    def _public_key_bytes(self, device: dict[str, Any]) -> bytes:
        value = str(device.get("public_key") or "")
        if not value.startswith("ed25519:"):
            raise DeviceBrokerError("trusted_device_public_key_invalid")
        raw = _b64url_decode(value.split(":", 1)[1])
        if len(raw) != 32:
            raise DeviceBrokerError("trusted_device_public_key_invalid")
        return raw

    def _prune_challenges(self) -> None:
        now = _utcnow()
        expired = [
            challenge_id
            for challenge_id, item in self._challenges.items()
            if _parse_iso(str(item["expires_at"])) <= now
        ]
        for challenge_id in expired:
            self._challenges.pop(challenge_id, None)

    def issue_challenge(self, device_id: str) -> dict[str, Any]:
        self._require_enabled()
        device = self._trusted_device(device_id)
        self._prune_challenges()
        now = _utcnow()
        challenge_id = "chal_" + secrets.token_urlsafe(18)
        nonce = secrets.token_urlsafe(32)
        expires_at = _iso(now + timedelta(seconds=self.challenge_ttl_seconds))
        signing_input = "\n".join(
            [
                self.SIGNING_PREFIX,
                str(device["device_id"]),
                challenge_id,
                nonce,
                expires_at,
            ]
        )
        self._challenges[challenge_id] = {
            "challenge_id": challenge_id,
            "device_id": str(device["device_id"]),
            "nonce": nonce,
            "created_at": _iso(now),
            "expires_at": expires_at,
            "signing_input": signing_input,
        }
        return {
            "schema": self.SCHEMA,
            "type": "challenge",
            "algorithm": "ed25519",
            "device_id": str(device["device_id"]),
            "challenge_id": challenge_id,
            "nonce": nonce,
            "expires_at": expires_at,
            "signing_input": signing_input,
        }

    def verify_challenge(self, challenge_id: str, signature: str) -> dict[str, Any]:
        self._require_enabled()
        # Pop before verification: every challenge is single-use even when the
        # signature is wrong, preventing online retry against a fixed challenge.
        challenge = self._challenges.pop(str(challenge_id), None)
        if not isinstance(challenge, dict):
            raise DeviceBrokerError("challenge_unknown_or_consumed")
        if _parse_iso(str(challenge["expires_at"])) <= _utcnow():
            raise DeviceBrokerError("challenge_expired")

        device = self._trusted_device(str(challenge["device_id"]))
        raw_signature = _b64url_decode(str(signature or "").strip())
        if len(raw_signature) != 64:
            raise DeviceBrokerError("invalid_ed25519_signature")
        public_key = Ed25519PublicKey.from_public_bytes(self._public_key_bytes(device))
        try:
            public_key.verify(
                raw_signature,
                str(challenge["signing_input"]).encode("utf-8"),
            )
        except InvalidSignature as exc:
            raise DeviceBrokerError("device_signature_invalid") from exc
        return device

    async def register_connection(
        self,
        device_id: str,
        websocket: Any,
        *,
        remote: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        device = self._trusted_device(device_id)
        now = _utcnow()
        connection_id = "devconn_" + secrets.token_urlsafe(18)
        item = {
            "connection_id": connection_id,
            "device_id": str(device["device_id"]),
            "connected_at": _iso(now),
            "last_seen_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=self.session_ttl_seconds)),
            "remote": remote,
            "transport": "wss",
            "websocket": websocket,
            "send_lock": asyncio.Lock(),
        }
        previous: dict[str, Any] | None = None
        async with self._lock:
            previous = self._connections.get(str(device["device_id"]))
            self._connections[str(device["device_id"])] = item

        if isinstance(previous, dict):
            old_socket = previous.get("websocket")
            if old_socket is not None and old_socket is not websocket:
                try:
                    await old_socket.close(code=4410, reason="Device connection replaced")
                except Exception:
                    pass
        return self._public_connection(item)

    async def unregister_connection(self, device_id: str, connection_id: str) -> None:
        failed: list[asyncio.Future[Any]] = []
        async with self._lock:
            current = self._connections.get(str(device_id))
            if isinstance(current, dict) and current.get("connection_id") == connection_id:
                self._connections.pop(str(device_id), None)
            for request_id, pending in list(self._pending.items()):
                if (
                    pending.get("device_id") == str(device_id)
                    and pending.get("connection_id") == connection_id
                ):
                    future = pending.get("future")
                    if isinstance(future, asyncio.Future) and not future.done():
                        failed.append(future)
                    self._pending.pop(request_id, None)
        for future in failed:
            future.set_exception(DeviceBrokerError("device_connection_closed"))

    async def touch_connection(self, device_id: str, connection_id: str) -> dict[str, Any]:
        # Re-check trust on every heartbeat so owner revocation cuts authority
        # without waiting for the broker session TTL to expire.
        self._trusted_device(device_id)
        now = _utcnow()
        async with self._lock:
            item = self._connections.get(str(device_id))
            if not isinstance(item, dict) or item.get("connection_id") != connection_id:
                raise DeviceBrokerError("device_connection_not_active")
            if _parse_iso(str(item["expires_at"])) <= now:
                self._connections.pop(str(device_id), None)
                raise DeviceBrokerError("device_connection_expired")
            item["last_seen_at"] = _iso(now)
            return self._public_connection(item)

    async def remaining_seconds(self, device_id: str, connection_id: str) -> float:
        async with self._lock:
            item = self._connections.get(str(device_id))
            if not isinstance(item, dict) or item.get("connection_id") != connection_id:
                return 0.0
            return max(
                0.0,
                (_parse_iso(str(item["expires_at"])) - _utcnow()).total_seconds(),
            )

    async def connection_status(self, device_id: str) -> dict[str, Any]:
        async with self._lock:
            item = self._connections.get(str(device_id))
            if not isinstance(item, dict):
                return {
                    "connected": False,
                    "device_id": str(device_id),
                }
            if _parse_iso(str(item["expires_at"])) <= _utcnow():
                self._connections.pop(str(device_id), None)
                return {
                    "connected": False,
                    "device_id": str(device_id),
                }
            return {
                "connected": True,
                **self._public_connection(item),
            }

    async def call_device(
        self,
        device_id: str,
        *,
        method: str,
        payload: dict[str, Any],
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        self._require_enabled()
        self._trusted_device(device_id)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        request_id = "devrpc_" + secrets.token_urlsafe(18)

        async with self._lock:
            item = self._connections.get(str(device_id))
            if not isinstance(item, dict):
                raise DeviceBrokerError("device_offline")
            if _parse_iso(str(item["expires_at"])) <= _utcnow():
                self._connections.pop(str(device_id), None)
                raise DeviceBrokerError("device_connection_expired")
            connection_id = str(item["connection_id"])
            websocket = item.get("websocket")
            send_lock = item.get("send_lock")
            if websocket is None or not isinstance(send_lock, asyncio.Lock):
                raise DeviceBrokerError("device_connection_invalid")
            self._pending[request_id] = {
                "request_id": request_id,
                "device_id": str(device_id),
                "connection_id": connection_id,
                "future": future,
            }

        message = {
            "schema": self.SCHEMA,
            "type": "rpc_request",
            "request_id": request_id,
            "connection_id": connection_id,
            "method": str(method or "").strip(),
            "payload": dict(payload or {}),
        }
        try:
            async with send_lock:
                await websocket.send_json(message)
            result = await asyncio.wait_for(
                future,
                timeout=max(1.0, min(120.0, float(timeout_seconds))),
            )
        except TimeoutError as exc:
            raise DeviceBrokerError("device_rpc_timeout") from exc
        finally:
            async with self._lock:
                current = self._pending.get(request_id)
                if isinstance(current, dict) and current.get("future") is future:
                    self._pending.pop(request_id, None)

        if not isinstance(result, dict):
            raise DeviceBrokerError("device_rpc_invalid_result")
        if not bool(result.get("ok")):
            raise DeviceBrokerError(str(result.get("error") or "device_rpc_failed"))
        body = result.get("result")
        if not isinstance(body, dict):
            raise DeviceBrokerError("device_rpc_invalid_result")
        return body

    async def resolve_rpc_response(
        self,
        device_id: str,
        connection_id: str,
        message: dict[str, Any],
    ) -> bool:
        request_id = str(message.get("request_id") or "")
        if not request_id:
            return False
        async with self._lock:
            pending = self._pending.get(request_id)
            if not isinstance(pending, dict):
                return False
            if (
                pending.get("device_id") != str(device_id)
                or pending.get("connection_id") != str(connection_id)
            ):
                return False
            future = pending.get("future")
            if not isinstance(future, asyncio.Future) or future.done():
                self._pending.pop(request_id, None)
                return False
            future.set_result(
                {
                    "ok": bool(message.get("ok")),
                    "result": message.get("result"),
                    "error": message.get("error"),
                }
            )
            self._pending.pop(request_id, None)
            return True

    async def send_connection_message(
        self,
        device_id: str,
        connection_id: str,
        message: dict[str, Any],
    ) -> None:
        async with self._lock:
            item = self._connections.get(str(device_id))
            if not isinstance(item, dict) or item.get("connection_id") != connection_id:
                raise DeviceBrokerError("device_connection_not_active")
            websocket = item.get("websocket")
            send_lock = item.get("send_lock")
            if websocket is None or not isinstance(send_lock, asyncio.Lock):
                raise DeviceBrokerError("device_connection_invalid")
        async with send_lock:
            await websocket.send_json(dict(message))

    def _public_connection(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"websocket", "send_lock"}
        }

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            connections = [
                self._public_connection(item)
                for _, item in sorted(self._connections.items())
                if isinstance(item, dict)
            ]
        return {
            "schema": self.SCHEMA,
            "enabled": self.enabled,
            "online_count": len(connections),
            "connections": connections,
            "challenge_ttl_seconds": self.challenge_ttl_seconds,
            "session_ttl_seconds": self.session_ttl_seconds,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
            "max_message_bytes": self.max_message_bytes,
        }
