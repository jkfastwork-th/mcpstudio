from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from mcp_studio.integrations import IntegrationManifest

_ALLOWED_TYPES = {"message", "approval", "question", "review", "alert", "note"}
_ALLOWED_ACTIONS = {"approve", "reject", "discuss"}
_ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}
_DEFAULT_URL = "http://127.0.0.1:8765/api/inbox/fern/send"
_DEFAULT_SECRET_FILE = Path("~/.config/nova/inbox-bridge.key").expanduser()


class NovaInboxAdapterError(RuntimeError):
    pass


def _descriptor() -> dict[str, Any]:
    return {
        "name": "nova_inbox_send",
        "description": "Send one verified Fern message, review, question, alert, note, or approval request to Jack's Nova Desktop Inbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": sorted(_ALLOWED_TYPES), "default": "message"},
                "subject": {"type": "string", "minLength": 1, "maxLength": 240},
                "body": {"type": "string", "minLength": 1, "maxLength": 8000},
                "priority": {"type": "string", "enum": sorted(_ALLOWED_PRIORITIES), "default": "normal"},
                "actions": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS)},
                    "uniqueItems": True,
                    "maxItems": 3,
                    "default": [],
                },
                "correlation_id": {"type": ["string", "null"], "maxLength": 256, "default": None},
                "context": {"type": "object", "default": {}, "additionalProperties": True},
            },
            "required": ["subject", "body"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }


_TOOL_CATALOG = {"nova_inbox_send": _descriptor()}


class NovaInboxIntegration:
    def __init__(self) -> None:
        self.url = str(os.getenv("HIRDA_NOVA_INBOX_URL", _DEFAULT_URL) or _DEFAULT_URL).strip()
        configured_secret = str(os.getenv("HIRDA_NOVA_INBOX_SECRET_FILE", "") or "").strip()
        self.secret_file = Path(configured_secret).expanduser() if configured_secret else _DEFAULT_SECRET_FILE
        self.timeout_sec = max(1.0, float(os.getenv("HIRDA_NOVA_INBOX_TIMEOUT_SEC", "8") or 8))
        self.manifest = IntegrationManifest.from_mapping({
            "id": "nova-inbox",
            "name": "Nova Desktop Inbox",
            "capabilities": ["nova_inbox", "asynchronous_human_notification", "approval_request"],
            "runtime": {"type": "local-http", "authority": "hirda-gated-backend"},
            "tools": list(_TOOL_CATALOG),
            "permissions": {"nova_inbox_send": "write"},
            "health": {"type": "adapter_probe"},
            "routing": {"mode": "communication-backend", "preferred_lanes": ["claude", "codex", "hermes"]},
            "metadata": {
                "backend_capability": True,
                "runtime_authority": "hirda",
                "sender_identity": "fern",
                "governance_authority": False,
                "external_action_authority": False,
            },
        })

    def _endpoint_ok(self) -> bool:
        parsed = urlsplit(self.url)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and parsed.path == "/api/inbox/fern/send"
            and not parsed.username
            and not parsed.password
        )

    def _secret(self) -> bytes:
        try:
            value = self.secret_file.read_bytes().strip()
        except OSError as error:
            raise NovaInboxAdapterError("nova_inbox_secret_unavailable") from error
        if len(value) < 32:
            raise NovaInboxAdapterError("nova_inbox_secret_invalid")
        return value

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _signature(self, payload: dict[str, Any], timestamp: str) -> str:
        message = timestamp.encode("ascii") + b"\n" + self._canonical(payload)
        return hmac.new(self._secret(), message, hashlib.sha256).hexdigest()

    async def validate(self) -> dict[str, Any]:
        return {
            "ok": self._endpoint_ok() and self.secret_file.is_file(),
            "endpoint_loopback": self._endpoint_ok(),
            "secret_available": self.secret_file.is_file(),
            "sender_identity": "fern",
            "runtime_authority": "hirda",
        }

    async def certify(self) -> dict[str, Any]:
        sample = {"type": "note", "subject": "certify", "body": "certify", "priority": "low", "actions": [], "context": {}}
        stamp = "1"
        first = self._signature(sample, stamp)
        second = self._signature(sample, stamp)
        return {
            "ok": first == second and len(first) == 64 and self._endpoint_ok(),
            "signing": "hmac-sha256",
            "network_call": False,
            "sender_identity": "fern",
        }

    async def status(self) -> dict[str, Any]:
        validation = await self.validate()
        return {
            **validation,
            "schema": "hirda-nova-inbox-status-v1",
            "url": self.url,
            "secret_file": str(self.secret_file),
            "governance_authority": False,
            "external_action_authority": False,
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        return [dict(_TOOL_CATALOG["nova_inbox_send"])]

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: dict[str, Any]) -> dict[str, Any]:
        if name != "nova_inbox_send":
            raise NovaInboxAdapterError("nova_inbox_tool_unknown")
        if not self._endpoint_ok():
            raise NovaInboxAdapterError("nova_inbox_endpoint_not_loopback")

        message_type = str(arguments.get("type") or "message").strip().casefold()
        subject = str(arguments.get("subject") or "").strip()
        body = str(arguments.get("body") or "").strip()
        priority = str(arguments.get("priority") or "normal").strip().casefold()
        actions = [str(value).strip().casefold() for value in (arguments.get("actions") or [])]
        if message_type not in _ALLOWED_TYPES:
            raise NovaInboxAdapterError("nova_inbox_type_invalid")
        if not subject or len(subject) > 240 or not body or len(body) > 8000:
            raise NovaInboxAdapterError("nova_inbox_content_invalid")
        if priority not in _ALLOWED_PRIORITIES:
            raise NovaInboxAdapterError("nova_inbox_priority_invalid")
        if len(set(actions)) != len(actions) or any(value not in _ALLOWED_ACTIONS for value in actions):
            raise NovaInboxAdapterError("nova_inbox_actions_invalid")

        correlation_id = str(arguments.get("correlation_id") or context.get("turn_id") or context.get("task_id") or "").strip() or None
        supplied_context = arguments.get("context") if isinstance(arguments.get("context"), dict) else {}
        payload = {
            "type": message_type,
            "subject": subject,
            "body": body,
            "priority": priority,
            "actions": actions,
            "correlation_id": correlation_id,
            "context": dict(supplied_context),
            "workspace": context.get("workspace") or context.get("workspace_key"),
            "managed_session_id": context.get("managed_session_id"),
        }
        timestamp = str(int(time.time()))
        signature = self._signature(payload, timestamp)
        headers = {
            "X-Nova-Inbox-Timestamp": timestamp,
            "X-Nova-Inbox-Signature": signature,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.post(self.url, json=payload, headers=headers)
        try:
            result = response.json()
        except ValueError as error:
            raise NovaInboxAdapterError(f"nova_inbox_invalid_response:{response.status_code}") from error
        if response.status_code < 200 or response.status_code >= 300:
            detail = result.get("error") if isinstance(result, dict) else None
            raise NovaInboxAdapterError(f"nova_inbox_send_failed:{response.status_code}:{detail or 'unknown'}")
        if not isinstance(result, dict):
            raise NovaInboxAdapterError("nova_inbox_response_not_object")
        return {
            "schema": "hirda-nova-inbox-send-v1",
            "status": "sent",
            "message_id": result.get("message_id"),
            "sender_id": result.get("sender_id"),
            "recipient_id": result.get("recipient_id"),
            "message_type": result.get("type"),
            "correlation_id": result.get("correlation_id"),
            "sender_verified": True,
            "governance_authority": False,
            "external_action_authority": False,
        }
