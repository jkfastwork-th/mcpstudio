from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from mcp_studio.integrations import IntegrationManifest

_DEFAULT_BASE_URL = "http://127.0.0.1:8790"
_DEFAULT_TOKEN_FILE = Path("~/.config/social-hub/ingress-token").expanduser()
_MAX_ASSET_BYTES = 25 * 1024 * 1024  # 25 MiB
_ALLOWED_CHANNELS = {"facebook", "instagram"}
_ALLOWED_RUN_MODES = {"now", "scheduled"}
_FORCED_SOURCE_APP = "chatgpt_web"
_FORCED_REQUESTED_BY = "jack"
_FORCED_ASSIGNED_AGENT = "fern"


class SocialHubTaskAdapterError(RuntimeError):
    pass


def _descriptor_create() -> dict[str, Any]:
    return {
        "name": "social_task_create",
        "description": "Create a social media task in the Social Hub for Fern to execute.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instructions": {"type": "string", "minLength": 1, "maxLength": 8000},
                "title": {"type": "string", "maxLength": 240},
                "channels": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(_ALLOWED_CHANNELS)},
                    "uniqueItems": True,
                    "maxItems": 2,
                },
                "run_mode": {"type": "string", "enum": sorted(_ALLOWED_RUN_MODES), "default": "now"},
                "scheduled_at": {"type": "string"},
                "watch_after_publish": {"type": "boolean", "default": True},
                "auto_reply": {"type": "boolean", "default": True},
                "hold_for_asset": {"type": "boolean", "default": False},
                "correlation_id": {"type": ["string", "null"], "maxLength": 256},
            },
            "required": ["instructions"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    }


def _descriptor_attach() -> dict[str, Any]:
    return {
        "name": "social_task_attach_asset",
        "description": "Attach a base64-encoded asset (image/video) to an existing social task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "asset_base64": {"type": "string", "minLength": 1},
                "asset_name": {"type": "string", "minLength": 1, "maxLength": 256},
                "asset_channel": {"type": "string", "enum": sorted(_ALLOWED_CHANNELS)},
                "content_type": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["task_id", "asset_base64", "asset_name", "asset_channel", "content_type"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    }


def _descriptor_status() -> dict[str, Any]:
    return {
        "name": "social_task_status",
        "description": "Read the status of a social media task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


_TOOL_CATALOG = {
    "social_task_create": _descriptor_create(),
    "social_task_attach_asset": _descriptor_attach(),
    "social_task_status": _descriptor_status(),
}


def _deterministic_correlation_id(context: dict[str, Any], content: dict[str, Any]) -> str:
    """Generate a deterministic bounded correlation ID from context + content."""
    turn_id = str(context.get("turn_id") or "").strip()
    task_id = str(context.get("task_id") or "").strip()
    gateway_session = str(context.get("gateway_session_id") or "").strip()
    content_digest = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    context_part = f"{turn_id}:{task_id}:{gateway_session}"
    context_digest = hashlib.sha256(context_part.encode("utf-8")).hexdigest()[:16]
    return f"sht-{context_digest}-{content_digest}"


class SocialHubTaskIntegration:
    def __init__(self) -> None:
        self.base_url = str(os.getenv("HIRDA_SOCIAL_HUB_URL", _DEFAULT_BASE_URL) or _DEFAULT_BASE_URL).strip()
        configured_token = str(os.getenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", "") or "").strip()
        self.token_file = Path(configured_token).expanduser() if configured_token else _DEFAULT_TOKEN_FILE
        self.timeout_sec = max(1.0, float(os.getenv("HIRDA_SOCIAL_HUB_TIMEOUT_SEC", "8") or 8))
        self.manifest = IntegrationManifest.from_mapping({
            "id": "social-hub-task",
            "name": "Social Hub Task Connector",
            "capabilities": ["social_hub_task", "social_media_scheduling"],
            "runtime": {"type": "local-http", "authority": "hirda-gated-backend"},
            "tools": list(_TOOL_CATALOG),
            "permissions": {
                "social_task_create": "write",
                "social_task_attach_asset": "write",
                "social_task_status": "read",
            },
            "health": {"type": "adapter_probe"},
            "routing": {"mode": "communication-backend", "preferred_lanes": ["claude", "codex", "hermes"]},
            "metadata": {
                "backend_capability": True,
                "executor_identity": "fern",
                "owner_social_accounts": True,
                "runtime_authority": "hirda",
                "governance_authority": False,
                "external_action_authority": False,
            },
        })

    def _endpoint_ok(self) -> bool:
        parsed = urlsplit(self.base_url)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not parsed.username
            and not parsed.password
        )

    def _token(self) -> str:
        try:
            value = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise SocialHubTaskAdapterError("social_hub_token_unavailable") from error
        if not value:
            raise SocialHubTaskAdapterError("social_hub_token_invalid")
        return value

    def _headers(self) -> dict[str, str]:
        return {
            "X-Social-Hub-Ingress-Token": self._token(),
            "Accept": "application/json",
        }

    async def validate(self) -> dict[str, Any]:
        return {
            "ok": self._endpoint_ok() and self.token_file.is_file(),
            "endpoint_loopback": self._endpoint_ok(),
            "token_available": self.token_file.is_file(),
            "executor_identity": "fern",
            "runtime_authority": "hirda",
        }

    async def certify(self) -> dict[str, Any]:
        # No network call, no publish side effect — pure local validation.
        return {
            "ok": self._endpoint_ok() and self.token_file.is_file(),
            "network_call": False,
            "executor_identity": "fern",
        }

    async def status(self) -> dict[str, Any]:
        validation = await self.validate()
        return {
            **validation,
            "schema": "hirda-social-hub-task-status-v1",
            "base_url": self.base_url,
            "token_file": str(self.token_file),
            "governance_authority": False,
            "external_action_authority": False,
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        return [dict(_TOOL_CATALOG[name]) for name in ("social_task_create", "social_task_attach_asset", "social_task_status")]

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: dict[str, Any]) -> dict[str, Any]:
        if name == "social_task_create":
            return await self._create_task(arguments, context)
        if name == "social_task_attach_asset":
            return await self._attach_asset(arguments)
        if name == "social_task_status":
            return await self._task_status(arguments)
        raise SocialHubTaskAdapterError("social_hub_tool_unknown")

    async def _create_task(self, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if not self._endpoint_ok():
            raise SocialHubTaskAdapterError("social_hub_endpoint_not_loopback")

        instructions = str(arguments.get("instructions") or "").strip()
        if not instructions or len(instructions) > 8000:
            raise SocialHubTaskAdapterError("social_hub_instructions_invalid")

        title = str(arguments.get("title") or "").strip() or None
        if title and len(title) > 240:
            raise SocialHubTaskAdapterError("social_hub_title_invalid")

        channels = arguments.get("channels") or []
        if not isinstance(channels, list):
            raise SocialHubTaskAdapterError("social_hub_channels_invalid")
        channels = [str(c).strip().lower() for c in channels]
        if len(set(channels)) != len(channels) or any(c not in _ALLOWED_CHANNELS for c in channels):
            raise SocialHubTaskAdapterError("social_hub_channels_invalid")

        run_mode = str(arguments.get("run_mode") or "now").strip().lower()
        if run_mode not in _ALLOWED_RUN_MODES:
            raise SocialHubTaskAdapterError("social_hub_run_mode_invalid")

        scheduled_at = str(arguments.get("scheduled_at") or "").strip() or None
        if run_mode == "scheduled" and not scheduled_at:
            raise SocialHubTaskAdapterError("social_hub_scheduled_at_required")

        watch_after_publish = bool(arguments.get("watch_after_publish", True))
        auto_reply = bool(arguments.get("auto_reply", True))
        hold_for_asset = bool(arguments.get("hold_for_asset", False))

        correlation_id = str(arguments.get("correlation_id") or "").strip() or None
        if not correlation_id:
            content = {
                "instructions": instructions,
                "title": title,
                "channels": channels,
                "run_mode": run_mode,
                "scheduled_at": scheduled_at,
                "watch_after_publish": watch_after_publish,
                "auto_reply": auto_reply,
                "hold_for_asset": hold_for_asset,
            }
            correlation_id = _deterministic_correlation_id(context, content)

        payload = {
            "instructions": instructions,
            "title": title,
            "channels": channels,
            "run_mode": run_mode,
            "scheduled_at": scheduled_at,
            "watch_after_publish": watch_after_publish,
            "auto_reply": auto_reply,
            "hold_for_asset": hold_for_asset,
            "correlation_id": correlation_id,
            "source_app": _FORCED_SOURCE_APP,
            "requested_by": _FORCED_REQUESTED_BY,
            "assigned_agent": _FORCED_ASSIGNED_AGENT,
        }

        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.post(
                f"{self.base_url}/api/social/ingress/tasks",
                json=payload,
                headers=self._headers(),
            )
        try:
            result = response.json()
        except ValueError as error:
            raise SocialHubTaskAdapterError(f"social_hub_invalid_response:{response.status_code}") from error
        if response.status_code < 200 or response.status_code >= 300:
            detail = result.get("error") if isinstance(result, dict) else None
            raise SocialHubTaskAdapterError(f"social_hub_create_failed:{response.status_code}:{detail or 'unknown'}")
        if not isinstance(result, dict):
            raise SocialHubTaskAdapterError("social_hub_response_not_object")
        return {
            "schema": "hirda-social-hub-task-create-v1",
            "status": "created",
            "task_id": result.get("task_id"),
            "correlation_id": correlation_id,
            "source_app": _FORCED_SOURCE_APP,
            "requested_by": _FORCED_REQUESTED_BY,
            "assigned_agent": _FORCED_ASSIGNED_AGENT,
            "governance_authority": False,
            "external_action_authority": False,
        }

    async def _attach_asset(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._endpoint_ok():
            raise SocialHubTaskAdapterError("social_hub_endpoint_not_loopback")

        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id or len(task_id) > 128:
            raise SocialHubTaskAdapterError("social_hub_task_id_invalid")

        asset_b64 = str(arguments.get("asset_base64") or "").strip()
        if not asset_b64:
            raise SocialHubTaskAdapterError("social_hub_asset_invalid")

        asset_name = str(arguments.get("asset_name") or "").strip()
        if not asset_name or len(asset_name) > 256:
            raise SocialHubTaskAdapterError("social_hub_asset_name_invalid")

        asset_channel = str(arguments.get("asset_channel") or "").strip().lower()
        if asset_channel not in _ALLOWED_CHANNELS:
            raise SocialHubTaskAdapterError("social_hub_asset_channel_invalid")

        content_type = str(arguments.get("content_type") or "").strip()
        if not content_type or len(content_type) > 128:
            raise SocialHubTaskAdapterError("social_hub_content_type_invalid")

        try:
            raw_bytes = base64.b64decode(asset_b64, validate=True)
        except Exception as error:
            raise SocialHubTaskAdapterError("social_hub_asset_not_base64") from error

        if len(raw_bytes) > _MAX_ASSET_BYTES:
            raise SocialHubTaskAdapterError("social_hub_asset_too_large")

        headers = {
            **self._headers(),
            "X-Asset-Name": asset_name,
            "X-Asset-Channel": asset_channel,
            "Content-Type": content_type,
        }

        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.post(
                f"{self.base_url}/api/social/ingress/tasks/{task_id}/assets",
                content=raw_bytes,
                headers=headers,
            )
        try:
            result = response.json()
        except ValueError as error:
            raise SocialHubTaskAdapterError(f"social_hub_invalid_response:{response.status_code}") from error
        if response.status_code < 200 or response.status_code >= 300:
            detail = result.get("error") if isinstance(result, dict) else None
            raise SocialHubTaskAdapterError(f"social_hub_attach_failed:{response.status_code}:{detail or 'unknown'}")
        if not isinstance(result, dict):
            raise SocialHubTaskAdapterError("social_hub_response_not_object")
        return {
            "schema": "hirda-social-hub-task-attach-v1",
            "status": "attached",
            "task_id": task_id,
            "asset_name": asset_name,
            "asset_channel": asset_channel,
            "content_type": content_type,
            "size_bytes": len(raw_bytes),
            "governance_authority": False,
            "external_action_authority": False,
        }

    async def _task_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._endpoint_ok():
            raise SocialHubTaskAdapterError("social_hub_endpoint_not_loopback")

        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id or len(task_id) > 128:
            raise SocialHubTaskAdapterError("social_hub_task_id_invalid")

        async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
            response = await client.get(
                f"{self.base_url}/api/social/ingress/tasks/{task_id}",
                headers=self._headers(),
            )
        try:
            result = response.json()
        except ValueError as error:
            raise SocialHubTaskAdapterError(f"social_hub_invalid_response:{response.status_code}") from error
        if response.status_code < 200 or response.status_code >= 300:
            detail = result.get("error") if isinstance(result, dict) else None
            raise SocialHubTaskAdapterError(f"social_hub_status_failed:{response.status_code}:{detail or 'unknown'}")
        if not isinstance(result, dict):
            raise SocialHubTaskAdapterError("social_hub_response_not_object")
        return {
            "schema": "hirda-social-hub-task-status-v1",
            "status": "ok",
            "task_id": task_id,
            "task": result,
            "governance_authority": False,
            "external_action_authority": False,
        }
