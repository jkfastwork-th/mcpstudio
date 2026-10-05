from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from mcp_studio.integrations import IntegrationManifest

_TOOLS = ("social_task_create", "social_task_attach_asset", "social_task_status")
_CHANNELS = ("facebook", "instagram")
_MAX_ASSET_BYTES = 25 * 1024 * 1024
_MAX_ASSET_BASE64_CHARS = 4 * ((_MAX_ASSET_BYTES + 2) // 3)
_DEFAULT_BASE_URL = "http://127.0.0.1:8790"
_DEFAULT_TOKEN_FILE = Path("~/.config/social-hub/ingress-token").expanduser()
_TASK_ID_PATTERN = r"^task_[0-9a-f]{16}$"
_SAFE_TASK_ID = re.compile(_TASK_ID_PATTERN)
_ASSET_NAME_RE = re.compile(
    r"^[A-Za-z0-9._-](?:[A-Za-z0-9._ -]{0,253}[A-Za-z0-9._-])?$"
)
_ALLOWED_PUBLISH_POLICIES = {"auto", "review", "draft"}
_ALLOWED_COMMENT_POLICIES = {"off", "track", "auto"}


class SocialHubTaskError(RuntimeError):
    """Sanitized errors from local Social Hub task ingress."""


SocialHubTaskAdapterError = SocialHubTaskError


def _tool_descriptors() -> list[dict[str, Any]]:
    common = {"openWorldHint": True, "destructiveHint": False}
    return [
        {
            "name": "social_task_create",
            "description": "Create a Social Hub publishing task for Jack, executed by Fern.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "instructions": {"type": "string", "minLength": 1, "maxLength": 12000},
                    "title": {"type": "string", "maxLength": 240},
                    "facebook_text": {"type": ["string", "null"], "maxLength": 20000},
                    "channels": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_CHANNELS)},
                        "uniqueItems": True,
                        "minItems": 1,
                        "maxItems": 2,
                    },
                    "run_mode": {"type": "string", "enum": ["now", "scheduled"], "default": "now"},
                    "scheduled_at": {"type": ["string", "null"], "maxLength": 100},
                    "publish_policy": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_PUBLISH_POLICIES),
                        "default": "review",
                    },
                    "comment_policy": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_COMMENT_POLICIES),
                        "default": "track",
                    },
                    "watch_after_publish": {"type": "boolean"},
                    "auto_reply": {"type": "boolean"},
                    "asset_policy": {
                        "type": "string",
                        "enum": ["ask_if_unspecified"],
                        "default": "ask_if_unspecified",
                    },
                    "asset_intent": {
                        "type": "string",
                        "enum": ["generate", "none", "upload", "unspecified"],
                        "default": "none",
                    },
                    "asset_spec": {
                        "type": ["object", "null"],
                        "additionalProperties": True,
                    },
                    "hold_for_asset": {"type": "boolean", "default": False},
                    "correlation_id": {"type": ["string", "null"], "maxLength": 160},
                },
                "required": ["instructions", "channels"],
                "additionalProperties": False,
            },
            "annotations": {**common, "readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "social_task_attach_asset",
            "description": "Attach base64-encoded asset bytes to a Social Hub task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "minLength": 21,
                        "maxLength": 21,
                        "pattern": _TASK_ID_PATTERN,
                    },
                    "asset_base64": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_ASSET_BASE64_CHARS,
                    },
                    "asset_name": {"type": "string", "minLength": 1, "maxLength": 255},
                    "asset_channel": {"type": "string", "enum": list(_CHANNELS)},
                    "content_type": {"type": "string", "minLength": 1, "maxLength": 255},
                },
                "required": ["task_id", "asset_base64", "asset_name", "asset_channel", "content_type"],
                "additionalProperties": False,
            },
            "annotations": {**common, "readOnlyHint": False, "idempotentHint": False},
        },
        {
            "name": "social_task_status",
            "description": "Read the status of a Social Hub task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "minLength": 21,
                        "maxLength": 21,
                        "pattern": _TASK_ID_PATTERN,
                    }
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        },
    ]


_DESCRIPTORS = {item["name"]: item for item in _tool_descriptors()}


class SocialHubTaskIntegration:
    def __init__(self) -> None:
        self.base_url = str(
            os.getenv("HIRDA_SOCIAL_HUB_BASE_URL")
            or os.getenv("HIRDA_SOCIAL_HUB_URL")
            or _DEFAULT_BASE_URL
        ).strip().rstrip("/")
        token_path = str(os.getenv("HIRDA_SOCIAL_HUB_TOKEN_FILE", "") or "").strip()
        self.token_file = Path(token_path).expanduser() if token_path else _DEFAULT_TOKEN_FILE
        try:
            self.timeout_sec = min(30.0, max(1.0, float(os.getenv("HIRDA_SOCIAL_HUB_TIMEOUT_SEC", "8"))))
        except (TypeError, ValueError):
            self.timeout_sec = 8.0
        self.manifest = IntegrationManifest.from_mapping({
            "id": "social-hub-task",
            "name": "Social Hub Task Ingress",
            "capabilities": ["social_hub_task_ingress"],
            "runtime": {"type": "local-http", "authority": "hirda"},
            "tools": list(_TOOLS),
            "permissions": {"social_task_create": "write", "social_task_attach_asset": "write", "social_task_status": "read"},
            "health": {"type": "adapter_probe"},
            "routing": {"mode": "local-http-backend", "preferred_lanes": ["chatgpt"]},
            "metadata": {"executor_identity": "fern", "owner_social_accounts": True, "runtime_authority": "hirda", "governance_authority": False},
        })

    def _endpoint_ok(self) -> bool:
        try:
            parsed = urlsplit(self.base_url)
            return (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and parsed.path in {"", "/"}
                    and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise SocialHubTaskError("social_hub_token_unavailable") from exc
        if not token or len(token) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise SocialHubTaskError("social_hub_token_invalid")
        return token

    async def validate(self) -> dict[str, Any]:
        loopback = self._endpoint_ok()
        try:
            token_available = bool(self._token())
        except SocialHubTaskError:
            token_available = False
        return {
            "ok": loopback and token_available,
            "endpoint_loopback": loopback,
            "token_available": token_available,
            "runtime_authority": "hirda",
            "governance_authority": False,
        }

    async def certify(self) -> dict[str, Any]:
        valid = await self.validate()
        return {"ok": valid["ok"], "endpoint_loopback": valid["endpoint_loopback"],
                "token_available": valid["token_available"], "network_call": False,
                "external_publication": False}

    async def status(self) -> dict[str, Any]:
        return {"schema": "hirda-social-hub-task-status-v1", **(await self.validate()),
                "executor_identity": "fern", "owner_social_accounts": True}

    async def list_tools(self) -> list[dict[str, Any]]:
        return [dict(_DESCRIPTORS[name]) for name in _TOOLS]

    @staticmethod
    def _bounded_result(
        response: httpx.Response,
        *,
        default_status: str,
        operation: str = "request",
    ) -> dict[str, Any]:
        if response.status_code < 200 or response.status_code >= 300:
            raise SocialHubTaskError(
                f"social_hub_{operation}_failed:{response.status_code}"
            )
        if len(response.content) > 1_048_576:
            raise SocialHubTaskError("social_hub_response_too_large")
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise SocialHubTaskError("social_hub_invalid_response") from exc
        if not isinstance(data, dict):
            raise SocialHubTaskError("social_hub_response_not_object")

        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        result: dict[str, Any] = {"status": default_status}
        for key in (
            "task_id",
            "correlation_id",
            "asset_id",
            "created_at",
            "updated_at",
            "run_mode",
        ):
            value = data.get(key)
            if value is None:
                value = task.get(key)
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 512:
                result[key] = value
        if isinstance(data.get("status"), (str, int, float, bool)):
            result["status"] = data["status"]
        if task:
            result["task"] = dict(task)
        if isinstance(data.get("duplicate_suppressed"), bool):
            result["duplicate_suppressed"] = data["duplicate_suppressed"]
        result["schema"] = "hirda-social-hub-task-result-v1"
        return result

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        operation: str = "request",
    ) -> dict[str, Any]:
        if not self._endpoint_ok():
            raise SocialHubTaskError("social_hub_endpoint_not_loopback")
        request_headers = {"X-Social-Hub-Ingress-Token": self._token(), "Accept": "application/json"}
        request_headers.update(headers or {})
        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec, follow_redirects=False) as client:
                response = await client.request(method, self.base_url + path, json=json_body,
                                                content=content, headers=request_headers)
        except httpx.HTTPError as exc:
            raise SocialHubTaskError("social_hub_request_failed") from exc
        return self._bounded_result(
            response,
            default_status="ok",
            operation=operation,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: dict[str, Any]) -> dict[str, Any]:
        if not self._endpoint_ok():
            raise SocialHubTaskError("social_hub_endpoint_not_loopback")
        if name not in _DESCRIPTORS:
            raise SocialHubTaskError("social_hub_tool_unknown")
        if not isinstance(arguments, dict):
            raise SocialHubTaskError("social_hub_arguments_invalid")
        if name == "social_task_create":
            instructions = arguments.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 12000:
                raise SocialHubTaskError("social_hub_instructions_invalid")
            title = arguments.get("title")
            if title is not None and (not isinstance(title, str) or len(title) > 240):
                raise SocialHubTaskError("social_hub_title_invalid")
            facebook_text = arguments.get("facebook_text")
            if facebook_text is not None and (
                not isinstance(facebook_text, str)
                or not facebook_text.strip()
                or len(facebook_text) > 20000
            ):
                raise SocialHubTaskError("social_hub_facebook_text_invalid")
            channels = arguments.get("channels", [])
            if (
                not isinstance(channels, list)
                or not channels
                or len(channels) > 2
                or len(set(channels)) != len(channels)
                or any(c not in _CHANNELS for c in channels)
            ):
                raise SocialHubTaskError("social_hub_channels_invalid")
            run_mode = arguments.get("run_mode", "now")
            scheduled_at = arguments.get("scheduled_at")
            if run_mode not in {"now", "scheduled"}:
                raise SocialHubTaskError("social_hub_run_mode_invalid")
            if facebook_text is not None and "facebook" not in channels:
                raise SocialHubTaskError("social_hub_facebook_text_channel_mismatch")
            if run_mode == "scheduled" and (
                not isinstance(scheduled_at, str) or not scheduled_at.strip()
            ):
                raise SocialHubTaskError("social_hub_scheduled_at_required")
            if run_mode == "now" and scheduled_at not in (None, ""):
                raise SocialHubTaskError("social_hub_schedule_invalid")
            for key in ("watch_after_publish", "auto_reply", "hold_for_asset"):
                if key in arguments and not isinstance(arguments[key], bool):
                    raise SocialHubTaskError("social_hub_boolean_invalid")

            asset_policy = str(
                arguments.get("asset_policy") or "ask_if_unspecified"
            ).strip().casefold()
            if asset_policy != "ask_if_unspecified":
                raise SocialHubTaskError("social_hub_asset_policy_invalid")
            asset_intent = str(
                arguments.get("asset_intent")
                or ("upload" if arguments.get("hold_for_asset") else "none")
            ).strip().casefold()
            if asset_intent not in {"generate", "none", "upload", "unspecified"}:
                raise SocialHubTaskError("social_hub_asset_intent_invalid")
            asset_spec = arguments.get("asset_spec")
            if asset_spec is not None and not isinstance(asset_spec, dict):
                raise SocialHubTaskError("social_hub_asset_spec_invalid")
            if asset_intent == "none" and asset_spec:
                raise SocialHubTaskError("social_hub_asset_spec_not_allowed_for_none")

            skill_id = str(context.get("skill_id") or "").strip()
            source_app = "hirda_skill" if skill_id else "chatgpt_web"
            publish_policy = str(
                arguments.get("publish_policy") or "review"
            ).strip().casefold()
            if publish_policy not in _ALLOWED_PUBLISH_POLICIES:
                raise SocialHubTaskError("social_hub_publish_policy_invalid")

            explicit_comment_policy = str(
                arguments.get("comment_policy") or ""
            ).strip().casefold()
            if explicit_comment_policy:
                comment_policy = explicit_comment_policy
            elif "watch_after_publish" in arguments or "auto_reply" in arguments:
                watch_requested = bool(arguments.get("watch_after_publish", True))
                auto_requested = bool(arguments.get("auto_reply", False))
                comment_policy = (
                    "off"
                    if not watch_requested
                    else ("auto" if auto_requested else "track")
                )
            else:
                comment_policy = "track"
            if comment_policy not in _ALLOWED_COMMENT_POLICIES:
                raise SocialHubTaskError("social_hub_comment_policy_invalid")
            watch_after_publish = comment_policy != "off"
            auto_reply = comment_policy == "auto"

            supplied_id = arguments.get("correlation_id")
            if supplied_id is not None and (not isinstance(supplied_id, str) or not supplied_id.strip() or len(supplied_id) > 160):
                raise SocialHubTaskError("social_hub_correlation_id_invalid")
            if supplied_id:
                correlation_id = supplied_id.strip()
            else:
                stable_context = next((str(context.get(key)).strip() for key in ("turn_id", "task_id", "gateway_session_id", "session_id", "managed_session_id") if context.get(key)), "no-context")
                task_content = {
                    "instructions": instructions.strip(),
                    "title": title,
                    "facebook_text": facebook_text,
                    "channels": channels,
                    "run_mode": run_mode,
                    "scheduled_at": scheduled_at,
                    "publish_policy": publish_policy,
                    "comment_policy": comment_policy,
                    "asset_policy": asset_policy,
                    "asset_intent": asset_intent,
                    "asset_spec": asset_spec,
                    "hold_for_asset": arguments.get("hold_for_asset", False),
                }
                content_digest = hashlib.sha256(json.dumps(task_content, sort_keys=True,
                    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:20]
                context_digest = hashlib.sha256(stable_context.encode()).hexdigest()[:20]
                correlation_id = f"sht-{context_digest}-{content_digest}"
            payload = {
                "instructions": instructions.strip(),
                "facebook_text": facebook_text,
                "channels": channels,
                "run_mode": run_mode,
                "scheduled_at": scheduled_at if run_mode == "scheduled" else None,
                "task_type": "publish",
                "publish_policy": publish_policy,
                "target_identity": "owner_social",
                "comment_policy": comment_policy,
                "watch_after_publish": watch_after_publish,
                "auto_reply": auto_reply,
                "asset_policy": asset_policy,
                "asset_intent": asset_intent,
                "asset_spec": asset_spec,
                "hold_for_asset": arguments.get("hold_for_asset", False),
                "correlation_id": correlation_id,
                "source_app": source_app,
                "requested_by": "jack",
                "assigned_agent": "fern",
                "skill_id": skill_id,
                "conversation_id": str(context.get("conversation_id") or "")[:160],
                "parent_request_id": str(context.get("parent_request_id") or "")[:160],
            }
            if title is not None:
                payload["title"] = title
            result = await self._request(
                "POST",
                "/api/social/ingress/tasks",
                json_body=payload,
                operation="create",
            )
            task_result = result.get("task") if isinstance(result.get("task"), dict) else {}
            task_status = str(
                task_result.get("status") or result.get("status") or "created"
            )
            return {
                **result,
                "status": task_status,
                "correlation_id": correlation_id,
                "source_app": source_app,
                "requested_by": "jack",
                "assigned_agent": "fern",
                "skill_id": skill_id,
                "publish_policy": publish_policy,
                "comment_policy": comment_policy,
            }
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str) or not _SAFE_TASK_ID.fullmatch(task_id):
            raise SocialHubTaskError("social_hub_task_id_invalid")
        encoded_id = quote(task_id, safe="")
        if name == "social_task_status":
            result = await self._request(
                "GET",
                f"/api/social/ingress/tasks/{encoded_id}",
                operation="status",
            )
            result["status"] = "ok"
            result["task_id"] = task_id
            return result
        encoded = arguments.get("asset_base64")
        filename = arguments.get("asset_name")
        channel = arguments.get("asset_channel")
        content_type = arguments.get("content_type")
        if not isinstance(encoded, str) or not encoded:
            raise SocialHubTaskError("social_hub_asset_invalid")
        if len(encoded) > 4 * ((_MAX_ASSET_BYTES + 2) // 3):
            raise SocialHubTaskError("social_hub_asset_too_large")
        try:
            asset = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SocialHubTaskError("social_hub_asset_not_base64") from exc
        if not asset or len(asset) > _MAX_ASSET_BYTES:
            raise SocialHubTaskError("social_hub_asset_too_large")
        if (
            not isinstance(filename, str)
            or not _ASSET_NAME_RE.fullmatch(filename)
            or filename in {".", ".."}
        ):
            raise SocialHubTaskError("social_hub_asset_name_invalid")
        if channel not in _CHANNELS:
            raise SocialHubTaskError("social_hub_asset_channel_invalid")
        if (not isinstance(content_type, str) or not content_type.strip() or len(content_type) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in content_type)):
            raise SocialHubTaskError("social_hub_asset_content_type_invalid")
        result = await self._request(
            "POST",
            f"/api/social/ingress/tasks/{encoded_id}/assets",
            content=asset,
            headers={
                "X-Asset-Name": filename.strip(),
                "X-Asset-Channel": channel,
                "Content-Type": content_type.strip(),
            },
            operation="attach",
        )
        result["status"] = "attached"
        result["task_id"] = task_id
        result["asset_name"] = filename.strip()
        result["asset_channel"] = channel
        result["content_type"] = content_type.strip()
        result["size_bytes"] = len(asset)
        return result
