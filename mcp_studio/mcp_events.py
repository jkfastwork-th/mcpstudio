from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .db import Database
from .settings import Settings


MCP_EVENTS_PROTOCOL_VERSION = "2026-07-28"
UPSTREAM_COMPAT_PROTOCOL_VERSION = "2025-11-25"
PEER_MESSAGE_EVENT = "peer.message.created"
_MAX_WEBHOOK_BODY_BYTES = 262_144
_DEFAULT_TTL_MS = 24 * 60 * 60 * 1000
_MAX_TTL_MS = 7 * 24 * 60 * 60 * 1000


class EventProtocolError(RuntimeError):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.data = dict(data or {})

    def as_jsonrpc(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True, slots=True)
class CallbackTarget:
    url: str
    hostname: str
    port: int
    request_target: str
    host_header: str
    addresses: tuple[str, ...]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        *,
        hostname: str,
        address: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._hirda_address = address
        self._hirda_hostname = hostname
        self._hirda_context = context

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._hirda_address, self.port),
            timeout=self.timeout,
        )
        self.sock = self._hirda_context.wrap_socket(
            sock,
            server_hostname=self._hirda_hostname,
        )


class McpEventService:
    """MCP 2.0 webhook events for durable HIRDA peer messages."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db

    @staticmethod
    def event_definition() -> dict[str, Any]:
        return {
            "name": PEER_MESSAGE_EVENT,
            "description": (
                "A new durable HIRDA peer message was addressed to the authenticated principal."
            ),
            "delivery": ["webhook"],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sender_principal_id": {
                        "type": "string",
                        "description": "Optional sender principal filter.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Optional peer thread filter.",
                    },
                    "correlation_id": {
                        "type": "string",
                        "description": "Optional peer correlation filter.",
                    },
                },
                "additionalProperties": False,
            },
            "payloadSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "correlation_id": {"type": "string"},
                    "sender_principal_id": {"type": "string"},
                    "recipient_principal_id": {"type": "string"},
                    "reply_to": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                    "created_at": {"type": "string"},
                },
                "required": [
                    "message_id",
                    "thread_id",
                    "correlation_id",
                    "sender_principal_id",
                    "recipient_principal_id",
                    "text",
                    "created_at",
                ],
                "additionalProperties": False,
            },
        }

    @classmethod
    def server_discover_result(cls) -> dict[str, Any]:
        return {
            "resultType": "complete",
            "supportedVersions": [MCP_EVENTS_PROTOCOL_VERSION],
            "capabilities": {
                "tools": {},
                "events": {},
            },
        }

    @classmethod
    def events_list_result(cls) -> dict[str, Any]:
        return {"events": [cls.event_definition()], "nextCursor": None}

    @staticmethod
    def _canonical_arguments(arguments: Any) -> tuple[dict[str, str], str]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise EventProtocolError(-32602, "Invalid params: arguments must be an object")
        allowed = {"sender_principal_id", "thread_id", "correlation_id"}
        extra = set(arguments) - allowed
        if extra:
            raise EventProtocolError(
                -32602,
                "Invalid params: unsupported subscription arguments",
                data={"fields": sorted(extra)},
            )
        normalized: dict[str, str] = {}
        for key in sorted(allowed):
            if key not in arguments:
                continue
            value = arguments[key]
            if not isinstance(value, str) or not value.strip():
                raise EventProtocolError(
                    -32602,
                    f"Invalid params: {key} must be a non-empty string",
                )
            normalized[key] = value.strip()[:512]
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return normalized, canonical

    @staticmethod
    def _decode_secret(secret: Any) -> bytes:
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            raise EventProtocolError(
                -32602,
                "Invalid params: webhook secret must use whsec_ prefix",
            )
        encoded = secret[len("whsec_") :]
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            raw = base64.b64decode(padded, validate=True)
        except Exception as exc:
            raise EventProtocolError(
                -32602,
                "Invalid params: webhook secret is not valid base64",
            ) from exc
        if not 24 <= len(raw) <= 64:
            raise EventProtocolError(
                -32602,
                "Invalid params: webhook secret must decode to 24-64 bytes",
            )
        return raw

    @staticmethod
    def _subscription_id(
        *,
        principal_id: str,
        peer_scope_id: str,
        event_name: str,
        callback_url: str,
        canonical_arguments: str,
    ) -> str:
        raw = "\0".join(
            (
                principal_id,
                peer_scope_id,
                event_name,
                callback_url,
                canonical_arguments,
            )
        )
        return "sub_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _iso_after(milliseconds: int) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(milliseconds=milliseconds)
        ).isoformat()

    async def _resolve_callback(self, url: Any) -> CallbackTarget:
        if not isinstance(url, str) or not url.strip():
            raise EventProtocolError(
                -32602, "Invalid params: webhook delivery URL is required"
            )
        normalized = url.strip()
        parsed = urlparse(normalized)
        if parsed.scheme.lower() != "https":
            raise EventProtocolError(
                -32602, "Invalid params: webhook delivery URL must use HTTPS"
            )
        if parsed.username or parsed.password or parsed.fragment:
            raise EventProtocolError(
                -32602,
                "Invalid params: webhook URL must not contain credentials or fragments",
            )
        hostname = str(parsed.hostname or "").strip().rstrip(".")
        if not hostname:
            raise EventProtocolError(-32602, "Invalid params: webhook host is required")
        try:
            port = int(parsed.port or 443)
        except ValueError as exc:
            raise EventProtocolError(-32602, "Invalid params: invalid webhook port") from exc
        if not 1 <= port <= 65535:
            raise EventProtocolError(-32602, "Invalid params: invalid webhook port")

        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise EventProtocolError(
                -32015,
                "Callback endpoint verification failed",
                data={"reason": "dns_failure"},
            ) from exc

        addresses: list[str] = []
        for info in infos:
            sockaddr = info[4]
            raw_ip = str(sockaddr[0]).split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(raw_ip)
            except ValueError as exc:
                raise EventProtocolError(
                    -32015,
                    "Callback endpoint verification failed",
                    data={"reason": "invalid_address"},
                ) from exc
            if not ip.is_global:
                raise EventProtocolError(
                    -32015,
                    "Callback endpoint verification failed",
                    data={"reason": "non_public_address"},
                )
            if raw_ip not in addresses:
                addresses.append(raw_ip)
        if not addresses:
            raise EventProtocolError(
                -32015,
                "Callback endpoint verification failed",
                data={"reason": "dns_failure"},
            )

        request_target = parsed.path or "/"
        if parsed.query:
            request_target += "?" + parsed.query
        host_header = hostname if port == 443 else f"{hostname}:{port}"
        return CallbackTarget(
            url=normalized,
            hostname=hostname,
            port=port,
            request_target=request_target,
            host_header=host_header,
            addresses=tuple(addresses),
        )

    @staticmethod
    def _sign_headers(
        *,
        secret: str,
        webhook_id: str,
        body: bytes,
        timestamp: int | None = None,
        subscription_id: str | None = None,
        previous_secret: str | None = None,
    ) -> dict[str, str]:
        stamp = int(timestamp if timestamp is not None else time.time())
        prefix = f"{webhook_id}.{stamp}.".encode("utf-8")

        def versioned_signature(value: str) -> str:
            key = McpEventService._decode_secret(value)
            digest = hmac.new(key, prefix + body, hashlib.sha256).digest()
            return "v1," + base64.b64encode(digest).decode("ascii")

        signatures = [versioned_signature(secret)]
        if previous_secret and previous_secret != secret:
            signatures.append(versioned_signature(previous_secret))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "webhook-id": webhook_id,
            "webhook-timestamp": str(stamp),
            "webhook-signature": " ".join(signatures),
        }
        if subscription_id:
            headers["X-MCP-Subscription-Id"] = subscription_id
        return headers

    @staticmethod
    def _post_pinned_https_sync(
        target: CallbackTarget,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        context = ssl.create_default_context()
        last_error: Exception | None = None
        for address in target.addresses:
            conn = _PinnedHTTPSConnection(
                hostname=target.hostname,
                address=address,
                port=target.port,
                timeout=timeout,
                context=context,
            )
            try:
                request_headers = {"Host": target.host_header, **headers}
                conn.request(
                    "POST",
                    target.request_target,
                    body=body,
                    headers=request_headers,
                )
                response = conn.getresponse()
                data = response.read(65_537)
                if len(data) > 65_536:
                    raise ValueError("webhook response exceeds 64 KiB")
                return int(response.status), data
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        if last_error is not None:
            raise last_error
        raise OSError("no validated callback addresses")

    async def _post_callback(
        self,
        url: str,
        *,
        body: bytes,
        headers: dict[str, str],
        timeout: float = 5.0,
    ) -> tuple[int, bytes]:
        target = await self._resolve_callback(url)
        return await asyncio.to_thread(
            self._post_pinned_https_sync,
            target,
            body=body,
            headers=headers,
            timeout=timeout,
        )

    async def _verify_callback(
        self,
        *,
        callback_url: str,
        secret: str,
        subscription_id: str,
    ) -> None:
        challenge = secrets.token_urlsafe(24)
        body = json.dumps(
            {"type": "verification", "challenge": challenge},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        webhook_id = "msg_verification_" + uuid.uuid4().hex
        headers = self._sign_headers(
            secret=secret,
            webhook_id=webhook_id,
            body=body,
            subscription_id=subscription_id,
        )
        try:
            status, response_body = await self._post_callback(
                callback_url,
                body=body,
                headers=headers,
            )
        except EventProtocolError:
            raise
        except Exception as exc:
            raise EventProtocolError(
                -32015,
                "Callback endpoint verification failed",
                data={"reason": "timeout" if isinstance(exc, TimeoutError) else "network_error"},
            ) from exc
        if not 200 <= status < 300:
            raise EventProtocolError(
                -32015,
                "Callback endpoint verification failed",
                data={"reason": "challenge_failed", "status": status},
            )
        try:
            response = json.loads(response_body.decode("utf-8"))
        except Exception as exc:
            raise EventProtocolError(
                -32015,
                "Callback endpoint verification failed",
                data={"reason": "challenge_failed"},
            ) from exc
        echoed = response.get("challenge") if isinstance(response, dict) else None
        if not isinstance(echoed, str) or not secrets.compare_digest(echoed, challenge):
            raise EventProtocolError(
                -32015,
                "Callback endpoint verification failed",
                data={"reason": "challenge_failed"},
            )

    async def subscribe(
        self,
        *,
        principal_id: str,
        peer_scope_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if name != PEER_MESSAGE_EVENT:
            raise EventProtocolError(-32602, "Invalid params: unsupported event name")
        arguments, canonical_arguments = self._canonical_arguments(params.get("arguments"))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise EventProtocolError(
                -32602, "Invalid params: only webhook delivery is supported"
            )
        callback_url = str(delivery.get("url") or "").strip()
        secret = str(delivery.get("secret") or "").strip()
        self._decode_secret(secret)
        await self._resolve_callback(callback_url)

        missing_ttl = object()
        ttl_value = params.get("ttlMs", missing_ttl)
        if ttl_value is None:
            expires_at = None
        else:
            if ttl_value is missing_ttl:
                ttl_ms = _DEFAULT_TTL_MS
            elif isinstance(ttl_value, bool) or not isinstance(ttl_value, int) or ttl_value <= 0:
                raise EventProtocolError(-32602, "Invalid params: ttlMs must be positive or null")
            else:
                ttl_ms = min(ttl_value, _MAX_TTL_MS)
            expires_at = self._iso_after(ttl_ms)

        subscription_id = self._subscription_id(
            principal_id=principal_id,
            peer_scope_id=peer_scope_id,
            event_name=name,
            callback_url=callback_url,
            canonical_arguments=canonical_arguments,
        )
        await self._verify_callback(
            callback_url=callback_url,
            secret=secret,
            subscription_id=subscription_id,
        )
        subscription = await self.db.upsert_mcp_event_subscription(
            subscription_id=subscription_id,
            principal_id=principal_id,
            peer_scope_id=peer_scope_id,
            event_name=name,
            arguments=arguments,
            callback_url=callback_url,
            delivery_secret=secret,
            expires_at=expires_at,
        )
        await self.db.add_event(
            "mcp.event.subscription.active",
            f"MCP event subscription {subscription_id} active.",
            data={
                "subscription_id": subscription_id,
                "principal_id": principal_id,
                "peer_scope_id": peer_scope_id,
                "event_name": name,
                "arguments": arguments,
                "callback_host": urlparse(callback_url).hostname,
                "expires_at": expires_at,
            },
            stream_id=f"mcp-event-sub:{subscription_id}",
            correlation_id=subscription_id,
        )
        return {
            "id": subscription_id,
            "refreshBefore": subscription.get("expires_at"),
            "cursor": None,
            "truncated": False,
        }

    async def unsubscribe(
        self,
        *,
        principal_id: str,
        peer_scope_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if name != PEER_MESSAGE_EVENT:
            return {}
        _arguments, canonical_arguments = self._canonical_arguments(params.get("arguments"))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise EventProtocolError(
                -32602, "Invalid params: only webhook delivery is supported"
            )
        callback_url = str(delivery.get("url") or "").strip()
        if not callback_url:
            raise EventProtocolError(-32602, "Invalid params: webhook URL is required")
        subscription_id = self._subscription_id(
            principal_id=principal_id,
            peer_scope_id=peer_scope_id,
            event_name=name,
            callback_url=callback_url,
            canonical_arguments=canonical_arguments,
        )
        await self.db.disable_mcp_event_subscription(
            subscription_id,
            principal_id=principal_id,
            peer_scope_id=peer_scope_id,
        )
        await self.db.add_event(
            "mcp.event.subscription.stopped",
            f"MCP event subscription {subscription_id} stopped.",
            data={
                "subscription_id": subscription_id,
                "principal_id": principal_id,
                "peer_scope_id": peer_scope_id,
                "event_name": name,
            },
            stream_id=f"mcp-event-sub:{subscription_id}",
            correlation_id=subscription_id,
        )
        return {}

    async def handle_rpc(
        self,
        *,
        principal_id: str,
        peer_scope_id: str,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not principal_id or not peer_scope_id:
            raise EventProtocolError(-32001, "Authenticated peer identity is required")
        payload = params if isinstance(params, dict) else {}
        if method == "server/discover":
            return self.server_discover_result()
        if method == "events/list":
            return self.events_list_result()
        if method == "events/subscribe":
            return await self.subscribe(
                principal_id=principal_id,
                peer_scope_id=peer_scope_id,
                params=payload,
            )
        if method == "events/unsubscribe":
            return await self.unsubscribe(
                principal_id=principal_id,
                peer_scope_id=peer_scope_id,
                params=payload,
            )
        raise EventProtocolError(-32601, "Method not found")

    @staticmethod
    def _subscription_matches(
        subscription: dict[str, Any],
        message: dict[str, Any],
    ) -> bool:
        arguments = subscription.get("arguments")
        if not isinstance(arguments, dict):
            return False
        mapping = {
            "sender_principal_id": "sender_principal_id",
            "thread_id": "thread_id",
            "correlation_id": "correlation_id",
        }
        for arg_key, message_key in mapping.items():
            expected = arguments.get(arg_key)
            if expected is not None and str(message.get(message_key) or "") != str(expected):
                return False
        return True

    @staticmethod
    def _event_payload(message: dict[str, Any], event_id: str) -> dict[str, Any]:
        return {
            "eventId": event_id,
            "name": PEER_MESSAGE_EVENT,
            "timestamp": str(message.get("created_at") or datetime.now(timezone.utc).isoformat()),
            "data": {
                "message_id": str(message.get("message_id") or ""),
                "thread_id": str(message.get("thread_id") or ""),
                "correlation_id": str(message.get("correlation_id") or ""),
                "sender_principal_id": str(message.get("sender_principal_id") or ""),
                "recipient_principal_id": str(message.get("recipient_principal_id") or ""),
                "reply_to": message.get("reply_to"),
                "text": str(message.get("text") or ""),
                "created_at": str(message.get("created_at") or ""),
            },
            "cursor": None,
        }

    async def _deliver_subscription(
        self,
        subscription: dict[str, Any],
        message: dict[str, Any],
    ) -> None:
        subscription_id = str(subscription.get("subscription_id") or "")
        message_id = str(message.get("message_id") or "")
        event_id = "evt_" + hashlib.sha256(
            f"{subscription_id}\0{message_id}".encode("utf-8")
        ).hexdigest()[:24]
        event = self._event_payload(message, event_id)
        body = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > _MAX_WEBHOOK_BODY_BYTES:
            await self.db.record_mcp_event_delivery(
                subscription_id,
                delivered=False,
                error="payload_too_large",
            )
            return

        callback_url = str(subscription.get("callback_url") or "")
        secret = str(subscription.get("delivery_secret") or "")
        previous_secret = str(subscription.get("previous_delivery_secret") or "")
        previous_expires = str(subscription.get("previous_secret_expires_at") or "")
        if previous_secret and previous_expires:
            try:
                expires = datetime.fromisoformat(previous_expires.replace("Z", "+00:00"))
                if expires <= datetime.now(timezone.utc):
                    previous_secret = ""
            except ValueError:
                previous_secret = ""
        else:
            previous_secret = ""

        delays = (0.0, 0.25, 1.0)
        last_error = ""
        last_status: int | None = None
        attempts_made = 0

        for attempt, delay in enumerate(delays, start=1):
            attempts_made = attempt
            if delay:
                await asyncio.sleep(delay)
            headers = self._sign_headers(
                secret=secret,
                previous_secret=previous_secret or None,
                webhook_id=event_id,
                body=body,
                subscription_id=subscription_id,
            )
            try:
                status, _ = await self._post_callback(
                    callback_url,
                    body=body,
                    headers=headers,
                )
                last_status = status
                if 200 <= status < 300:
                    await self.db.record_mcp_event_delivery(
                        subscription_id,
                        delivered=True,
                        error=None,
                    )
                    await self.db.add_event(
                        "peer.event.delivered",
                        f"Peer event {event_id} delivered.",
                        data={
                            "event_id": event_id,
                            "message_id": message_id,
                            "subscription_id": subscription_id,
                            "attempt": attempt,
                            "status": status,
                            "recipient_principal_id": message.get("recipient_principal_id"),
                        },
                        stream_id=f"peer:{message.get('thread_id') or ''}",
                        correlation_id=str(message.get("correlation_id") or ""),
                    )
                    return
                last_error = f"http_{status}"
                if status in {410, 413} or (400 <= status < 500 and status not in {408, 425, 429}):
                    if status == 410:
                        await self.db.disable_mcp_event_subscription(
                            subscription_id,
                            principal_id=str(subscription.get("principal_id") or ""),
                            peer_scope_id=str(subscription.get("peer_scope_id") or ""),
                        )
                    break
            except EventProtocolError as exc:
                last_error = str((exc.data or {}).get("reason") or exc.message)
                break
            except Exception as exc:
                last_error = type(exc).__name__

        await self.db.record_mcp_event_delivery(
            subscription_id,
            delivered=False,
            error=last_error or (
                f"http_{last_status}" if last_status is not None else "delivery_failed"
            ),
        )
        await self.db.add_event(
            "peer.event.failed",
            f"Peer event {event_id} delivery failed.",
            severity="warning",
            data={
                "event_id": event_id,
                "message_id": message_id,
                "subscription_id": subscription_id,
                "attempts": attempts_made,
                "status": last_status,
                "error": last_error,
                "recipient_principal_id": message.get("recipient_principal_id"),
            },
            stream_id=f"peer:{message.get('thread_id') or ''}",
            correlation_id=str(message.get("correlation_id") or ""),
        )

    async def dispatch_peer_message(self, message: dict[str, Any]) -> int:
        recipient = str(message.get("recipient_principal_id") or "").strip()
        peer_scope = str(message.get("peer_scope_id") or "").strip()
        if not recipient or not peer_scope:
            return 0
        subscriptions = await self.db.list_mcp_event_subscriptions(
            principal_id=recipient,
            peer_scope_id=peer_scope,
            event_name=PEER_MESSAGE_EVENT,
        )
        matching = [
            subscription
            for subscription in subscriptions
            if self._subscription_matches(subscription, message)
        ]
        if matching:
            await asyncio.gather(
                *(self._deliver_subscription(subscription, message) for subscription in matching)
            )
        return len(matching)
