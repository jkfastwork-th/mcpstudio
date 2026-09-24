from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .db import Database
from .settings import Settings
from .oauth import OAuthManager
from .managed_sessions import ManagedSessionManager, ManagedSessionError, ManagedSessionConflict, WorkspaceNotAllowed
from .tool_permissions import decide_tool_call
from .jev_decision import evaluate_tool_call as evaluate_jev_tool_call
from .reflex import (
    append_dataset_record,
    decision_fingerprint,
    evaluate_tool_call as evaluate_reflex_tool_call,
)
from .graft import GraftManager, GraftError
from .capsules import CapsuleService, CapsuleNotFound
from .world_authoring import WorldAuthoringManager, WorldAuthoringError
from .integrations import IntegrationManager, IntegrationError


CONTROL_TOOL_NAMES = (
    "mcpstudio_use_workspace",
    "mcpstudio_create_session",
    "mcpstudio_use_session",
    "mcpstudio_lane_status",
    "mcpstudio_set_lane_state",
    "mcpstudio_list_capsules",
    "mcpstudio_get_capsule",
    "mcpstudio_set_capsule_contract",
    "mcpstudio_approve_capsule_handoff",
    "mcpstudio_context_status",
    "mcpstudio_report_context_usage",
    "mcpstudio_handoff_session",
    "mcpstudio_accept_handoff",
)


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class GatewaySessionManager:
    """M5.3 transport-session virtualization for Streamable HTTP MCP.

    The client receives a stable gateway session id (gws-...). The upstream
    Serena session id remains private and can be replaced after a deterministic
    unknown-session 404 without changing the client-visible id.
    """

    def __init__(
        self, settings: Settings, db: Database, oauth: OAuthManager | None = None,
        managed_sessions: ManagedSessionManager | None = None,
        graft: GraftManager | None = None,
        capsules: CapsuleService | None = None,
        world_authoring: WorldAuthoringManager | None = None,
        integrations: IntegrationManager | None = None,
    ):
        self.settings = settings
        self.db = db
        self.oauth = oauth
        self.managed_sessions = managed_sessions
        self.graft = graft
        self.capsules = capsules
        self.world_authoring = world_authoring
        self.integrations = integrations

    def authenticate(self, request: Request) -> tuple[str, str, str, str, str]:
        """Authenticate and derive a stable client identity.

        Returns (token, client_id, client_type, identity_scope, identity_source).
        Raw conversation identifiers are never persisted. When ChatGPT supplies
        x-openai-session we hash it together with the authenticated connector
        namespace so logical-session reclaim is conversation-scoped instead of
        connector-wide.
        """
        expected = os.environ.get(self.settings.studio.gateway_token_env, "")
        supplied = request.headers.get("authorization", "")
        prefix = "Bearer "
        token = supplied[len(prefix):] if supplied.startswith(prefix) else ""
        static_ok = bool(expected and token and secrets.compare_digest(token, expected))
        oauth_claims = None
        if token and not static_ok and self.oauth is not None and self.oauth.enabled:
            try:
                oauth_claims = self.oauth.verify_access_token(token)
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"OAuth gateway authentication is not ready: {exc}")
        if self.settings.studio.gateway_auth_mode == "bearer":
            if not token:
                headers = {}
                if self.oauth is not None and self.oauth.enabled:
                    headers["WWW-Authenticate"] = (
                        f'Bearer resource_metadata="{self.oauth.protected_resource_metadata_url}", '
                        'scope="mcp:serena"'
                    )
                raise HTTPException(status_code=401, detail="Bearer authentication required", headers=headers)
            if not static_ok and oauth_claims is None:
                headers = {}
                if self.oauth is not None and self.oauth.enabled:
                    headers["WWW-Authenticate"] = (
                        f'Bearer error="invalid_token", resource_metadata="{self.oauth.protected_resource_metadata_url}", '
                        'scope="mcp:serena"'
                    )
                raise HTTPException(status_code=401, detail="Invalid gateway bearer token", headers=headers)
            if not expected and (self.oauth is None or not self.oauth.enabled):
                raise HTTPException(status_code=503, detail="Gateway authentication is not configured")

        explicit = (request.headers.get("x-mcp-studio-client-id") or "").strip()
        openai_session = (request.headers.get("x-openai-session") or "").strip()
        if explicit:
            client_id = explicit[:240]
            identity_scope = "explicit"
            identity_source = "x-mcp-studio-client-id"
        elif openai_session:
            if oauth_claims is not None:
                connector_namespace = f"oauth:{oauth_claims.client_id}"
            elif token:
                connector_namespace = "bearer:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
            else:
                ua = request.headers.get("user-agent", "anonymous")
                connector_namespace = "anonymous:" + hashlib.sha256(ua.encode("utf-8")).hexdigest()
            digest = hashlib.sha256(
                (connector_namespace + "\0" + openai_session).encode("utf-8")
            ).hexdigest()[:24]
            client_id = "conversation-" + digest
            identity_scope = "conversation"
            identity_source = "x-openai-session-fingerprint"
        elif oauth_claims is not None:
            client_id = "oauth-" + hashlib.sha256(oauth_claims.client_id.encode("utf-8")).hexdigest()[:24]
            identity_scope = "connector"
            identity_source = "oauth-client-id"
        elif token:
            client_id = "bearer-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
            identity_scope = "connector"
            identity_source = "bearer-fingerprint"
        else:
            ua = request.headers.get("user-agent", "anonymous")
            client_id = "anon-" + hashlib.sha256(ua.encode("utf-8")).hexdigest()[:24]
            identity_scope = "anonymous"
            identity_source = "user-agent-fingerprint"
        client_type = (request.headers.get("x-mcp-studio-client-type") or "mcp-http").strip()[:80] or "mcp-http"
        return token, client_id, client_type, identity_scope, identity_source

    def client_observation(
        self,
        request: Request,
        jsonrpc: dict[str, Any] | None,
        *,
        identity_scope: str,
        identity_source: str,
    ) -> dict[str, Any]:
        params = jsonrpc.get("params") if isinstance(jsonrpc, dict) and isinstance(jsonrpc.get("params"), dict) else {}
        client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        name = str(client_info.get("name") or "")[:160]
        version = str(client_info.get("version") or "")[:80]
        ua = (request.headers.get("user-agent") or "")[:300]
        haystack = f"{name} {ua}".lower()
        openai_like = any(x in haystack for x in ("openai", "chatgpt"))
        prefixes = tuple(x.lower() for x in self.settings.studio.openai_observation_header_prefixes)
        observed_header_names = sorted({
            key.lower() for key in request.headers.keys()
            if key.lower().startswith(prefixes)
        })[:64]
        # Store header *names only*, never values. Authorization is never stored.
        return {
            "compatibility_version": "m6.2.4",
            "openai_like": openai_like,
            "client_info_name": name,
            "client_info_version": version,
            "user_agent": ua,
            "identity_scope": identity_scope,
            "identity_source": identity_source,
            "conversation_identity_available": identity_scope in {"explicit", "conversation"},
            "observed_header_names": observed_header_names,
        }

    @staticmethod
    def context_usage_from_request(request: Request) -> tuple[float, str] | None:
        """Read explicit context telemetry headers without inferring missing usage."""
        for header in (
            "x-openai-context-usage-percent",
            "x-chatgpt-context-usage-percent",
            "x-mcp-context-usage-percent",
        ):
            raw = request.headers.get(header)
            if raw is None:
                continue
            try:
                pct = float(str(raw).strip())
            except (TypeError, ValueError):
                return None
            if 0.0 <= pct <= 100.0:
                return pct, f"header:{header}"
            return None
        return None

    @staticmethod
    def _norm_path(value: str | None) -> str:
        path = str(value or "/").strip() or "/"
        if not path.startswith("/"):
            path = "/" + path
        return path.rstrip("/") or "/"

    async def resolve_ingress(self, request: Request, server_id: str) -> dict[str, Any]:
        """Attribute a request to registered ingress inventory.

        This is observability metadata, never an authentication/security decision.
        Exact endpoint host+path wins. Ambiguity remains unattributed rather than
        guessing. Cloudflare header *presence* may disambiguate Cloudflare from a
        direct endpoint, but header values are never persisted.
        """
        host = (request.url.hostname or "").lower()
        path = self._norm_path(request.url.path)
        cf_present = any(h in request.headers for h in ("cf-ray", "cf-connecting-ip", "cf-visitor"))
        candidates = []
        for tunnel in await self.db.list_tunnels():
            endpoint = tunnel.get("endpoint")
            if not endpoint:
                continue
            try:
                parsed = urlparse(endpoint)
            except Exception:
                continue
            endpoint_host = (parsed.hostname or "").lower()
            endpoint_path = self._norm_path(parsed.path)
            meta = tunnel.get("metadata") or {}
            tunnel_server = str(meta.get("server_id") or "")
            if tunnel_server and tunnel_server != server_id:
                continue
            if endpoint_host == host and endpoint_path == path:
                candidates.append(tunnel)

        selected = None
        method = "unattributed"
        confidence = "none"
        if len(candidates) == 1:
            selected = candidates[0]
            method = "endpoint_host_path"
            confidence = "high" if selected.get("provider") != "cloudflare" or cf_present else "medium"
        elif len(candidates) > 1 and cf_present:
            cf = [x for x in candidates if x.get("provider") == "cloudflare"]
            if len(cf) == 1:
                selected = cf[0]
                method = "endpoint_host_path+cloudflare_evidence"
                confidence = "high"
            else:
                method = "ambiguous_endpoint"
                confidence = "low"
        elif len(candidates) > 1:
            method = "ambiguous_endpoint"
            confidence = "low"

        provider = selected.get("provider") if selected else ("cloudflare" if cf_present else "direct/unattributed")
        return {
            "tunnel_id": selected.get("id") if selected else None,
            "host": host,
            "path": path,
            "provider": provider,
            "method": method,
            "confidence": confidence,
            "cloudflare_evidence": cf_present,
            "candidate_count": len(candidates),
        }

    @staticmethod
    def parse_jsonrpc(body: bytes) -> dict[str, Any] | None:
        if not body:
            return None
        try:
            value = json.loads(body)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _management_tools(self) -> list[dict[str, Any]]:
        if not (self.managed_sessions and self.managed_sessions.enabled):
            return []
        tools = [
            {
                "name": "mcpstudio_list_workspaces",
                "description": "List project workspaces approved for isolated MCP Studio managed sessions.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "mcpstudio_register_workspace",
                "description": "Register an existing project directory under an approved workspace root.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"}, "project_path": {"type": "string"}, "name": {"type": "string"},
                    },
                    "required": ["key", "project_path"], "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_list_sessions",
                "description": "List durable isolated Serena sessions and their pinned projects.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "mcpstudio_get_session",
                "description": "Get one managed session by id or name.",
                "inputSchema": {
                    "type": "object", "properties": {"session": {"type": "string"}},
                    "required": ["session"], "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_rename_session",
                "description": "Rename a managed session without changing its pinned workspace or Serena instance.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"session": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["session", "name"], "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_set_permissions",
                "description": "Set READ/WRITE/EXECUTE/DESTRUCTIVE permissions for one managed session. Omitted fields keep their current value.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session": {"type": "string"},
                        "read": {"type": "boolean"},
                        "write": {"type": "boolean"},
                        "execute": {"type": "boolean"},
                        "destructive": {"type": "boolean"},
                        "scope": {"type": "string", "enum": ["workspace", "unrestricted"]},
                        "fail_closed_unknown": {"type": "boolean"},
                    },
                    "required": ["session"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_session_history",
                "description": "Show lifecycle/audit history and transport history for a managed session.",
                "inputSchema": {
                    "type": "object", "properties": {"session": {"type": "string"}},
                    "required": ["session"], "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_create_session",
                "description": "Create a dedicated Serena process pinned to one registered workspace and attach this MCP transport by default.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "workspace": {"type": "string"},
                        "attach": {"type": "boolean", "default": True},
                    },
                    "required": ["name", "workspace"], "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_use_workspace",
                "description": "Production-safe shortcut: use a registered workspace key or an approved absolute project path, create/resume its isolated session, and attach this transport.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "name": {"type": "string"},
                    },
                    "required": ["workspace"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_use_session",
                "description": "Attach this MCP transport to an existing managed session. Accepts an id/name or plain-language request such as 'continue HIRDA', 'go back to Nova', or Thai equivalents. Conversation-local binding and explicit names take precedence; ambiguous cross-session requests fail closed and return candidates.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session": {"type": "string"},
                        "request": {"type": "string"},
                        "record_handoff": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_lane_status",
                "description": "Show HIRDA routing state for Claude, Codex, and Hermes lanes. Only normal lanes accept new work.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "mcpstudio_set_lane_state",
                "description": "Set one agent lane to normal, draining, disabled, or emergency. Disabled/emergency can auto-handoff active portable capsules; Guarded waits for approval and Pinned never moves automatically.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "enum": ["claude", "codex", "hermes"]},
                        "state": {"type": "string", "enum": ["normal", "draining", "disabled", "emergency"]},
                        "reason": {"type": "string", "maxLength": 500},
                        "auto_handoff": {"type": "boolean", "default": True},
                    },
                    "required": ["agent", "state"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_list_capsules",
                "description": "List current HIRDA capsules, their owner lane, Capsule Contract portability, and handoff state.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_get_capsule",
                "description": "Get one HIRDA capsule by capsule id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"capsule_id": {"type": "string"}},
                    "required": ["capsule_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_set_capsule_contract",
                "description": "Set the model-independent Capsule Contract used to preserve task semantics across lane handoff. Safe/Guarded contracts should define handoff checks and target capabilities.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "capsule_id": {"type": "string"},
                        "contract": {"type": "object"},
                    },
                    "required": ["capsule_id", "contract"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_approve_capsule_handoff",
                "description": "Approve a Guarded Capsule handoff after the target lane has acknowledged receipt and passed contract validation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "capsule_id": {"type": "string"},
                        "handoff_id": {"type": "string"},
                        "approved_by": {"type": "string", "default": "chatgpt/mcp"},
                    },
                    "required": ["capsule_id", "handoff_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_context_status",
                "description": "Show exact context-usage telemetry for this ChatGPT/MCP conversation when a product/caller has reported it. HIRDA never estimates missing context usage.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "mcpstudio_report_context_usage",
                "description": "Report authoritative context-window usage for the currently attached conversation/session. Use only a measured product/caller percentage; never estimate. HIRDA opens rollover alerts at 80% and marks them critical at 90%.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "context_usage_percent": {"type": "number", "minimum": 0, "maximum": 100},
                        "source": {"type": "string", "minLength": 1, "maxLength": 120, "default": "product_surface"},
                    },
                    "required": ["context_usage_percent"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_handoff_session",
                "description": "Prepare a single-use rollover token for handing the currently attached managed session to a new ChatGPT/MCP conversation. Ownership does not transfer until the new conversation claims the token.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "minLength": 1, "maxLength": 12000},
                        "reason": {"type": "string", "maxLength": 500},
                        "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": 1800},
                        "context_usage_percent": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_accept_handoff",
                "description": "Claim a single-use session handoff token from another conversation, bind this transport to that managed session, then detach the source transport only after the claim succeeds.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "minLength": 16, "maxLength": 512},
                    },
                    "required": ["token"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_current_session",
                "description": "Show the managed session currently attached to this MCP transport.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "mcpstudio_detach_session",
                "description": "Detach this transport from its managed project and return to the neutral Studio upstream.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "mcpstudio_graft_status",
                "description": "Show HIRDA Graft read-only context status for a workspace/session. Defaults to the currently attached managed session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "session": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_graft_configure",
                "description": "Enable/disable HIRDA Graft read-only sidecar context for a workspace and set deterministic canary rollout. Serena keeps its existing managed-session write/lease authority.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "session": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "rollout_percent": {"type": "number", "minimum": 0, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_graft_query",
                "description": "Run one read-only Graft context query under HIRDA's circuit breaker. On failure/rejection it returns Serena-only fallback_required and opens the workspace circuit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "session": {"type": "string"},
                        "question": {"type": "string"},
                        "tool": {"type": "string", "enum": ["graft_find_code", "graft_file_api", "graft_check_freshness", "graft_trace_calls", "graft_find_all", "graft_repo_map"]},
                        "arguments": {"type": "object"},
                        "request_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_graft_rollback",
                "description": "Open the workspace Graft circuit immediately. Subsequent context requests must use Serena-only until explicitly rearmed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "session": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_graft_rearm",
                "description": "Explicitly rearm the workspace Graft circuit after operator review.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workspace": {"type": "string"},
                        "session": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "mcpstudio_close_session",
                "description": "Stop a managed Serena session. If this transport is attached to it, Studio detaches first.",
                "inputSchema": {
                    "type": "object", "properties": {"session": {"type": "string"}},
                    "required": ["session"], "additionalProperties": False,
                },
            },
        ]
        if self.world_authoring is not None:
            tools.append(
                {
                    "name": "mcpstudio_world_authoring_preview",
                    "description": (
                        "Validate a bounded Earth-616 NovaWorldProposal through a registered "
                        "Pixi world-authoring workspace. Preview-only: does not mutate Earth state "
                        "or promote assets."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "workspace": {"type": "string", "minLength": 1},
                            "proposal": {"type": "object"},
                            "providers": {
                                "type": "array",
                                "items": {"type": "object"},
                                "default": [],
                            },
                        },
                        "required": ["workspace", "proposal"],
                        "additionalProperties": False,
                    },
                }
            )
            tools.append(
                {
                    "name": "mcpstudio_world_authoring_audit",
                    "description": (
                        "Persist bounded Earth-616 visual-authoring lifecycle evidence into the "
                        "HIRDA audit log. Audit-only: does not mutate Earth state or promote assets."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "workspace": {"type": "string", "minLength": 1},
                            "events": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {"type": "object"},
                            },
                        },
                        "required": ["workspace", "events"],
                        "additionalProperties": False,
                    },
                }
            )
        return tools

    @staticmethod
    def _jsonrpc_tool_response(request_id: Any, payload: Any, *, is_error: bool = False) -> Response:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": bool(is_error),
            },
        }
        return Response(
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @staticmethod
    def _jsonrpc_raw_tool_response(
        request_id: Any, result: dict[str, Any], *, is_error: bool = False
    ) -> Response:
        payload = dict(result or {})
        if is_error:
            payload["isError"] = True
        body = {"jsonrpc": "2.0", "id": request_id, "result": payload}
        return Response(
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    def _ordered_management_tools(self) -> list[dict[str, Any]]:
        """Return Studio tools with project-selection controls first.

        ChatGPT and other MCP clients may cap or prioritize the first portion of
        a large tool catalog.  The three tools required to bind an unbound
        transport therefore must never be buried behind the Serena catalog.
        """
        tools = self._management_tools()
        priority = {name: index for index, name in enumerate(CONTROL_TOOL_NAMES)}
        return sorted(
            tools,
            key=lambda tool: (priority.get(str(tool.get("name")), len(priority)), str(tool.get("name"))),
        )

    def _ordered_backend_tools(self) -> list[dict[str, Any]]:
        if self.integrations is None:
            return []
        return self.integrations.backend_tools()

    def _all_local_tools(self) -> list[dict[str, Any]]:
        return self._ordered_management_tools() + self._ordered_backend_tools()

    def _augment_tools_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result") if isinstance(payload, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return payload

        studio_tools = self._all_local_tools()
        studio_names = {str(t.get("name")) for t in studio_tools if isinstance(t, dict)}
        # Studio control tools are intentionally prepended.  Besides making the
        # control plane discoverable on an unbound transport, this protects the
        # critical controls from client-side catalog limits/truncation.
        upstream_tools = [
            tool for tool in tools
            if not (isinstance(tool, dict) and str(tool.get("name")) in studio_names)
        ]
        result["tools"] = studio_tools + upstream_tools
        return payload

    def _augment_tools_content(self, content: bytes, content_type: str) -> bytes:
        if not self._all_local_tools():
            return content
        try:
            if "text/event-stream" in content_type:
                out: list[str] = []
                # A Streamable HTTP response can contain progress/notification
                # events before the JSON-RPC tools/list result.  Inspect every
                # data event and only rewrite the event that actually contains
                # result.tools.  The previous implementation stopped after the
                # first JSON data event, which could leave ChatGPT without the
                # Studio controls.
                for line in content.decode("utf-8").splitlines(keepends=True):
                    core = line.rstrip("\r\n")
                    ending = line[len(core):]
                    if core.startswith("data:"):
                        raw = core[5:].strip()
                        try:
                            payload = json.loads(raw)
                            result = payload.get("result") if isinstance(payload, dict) else None
                            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                                payload = self._augment_tools_payload(payload)
                                core = "data: " + json.dumps(
                                    payload, ensure_ascii=False, separators=(",", ":")
                                )
                        except Exception:
                            pass
                    out.append(core + ending)
                return "".join(out).encode("utf-8")
            payload = json.loads(content.decode("utf-8"))
            return json.dumps(
                self._augment_tools_payload(payload), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except Exception:
            return content

    @staticmethod
    def _context_transition_notice(report: dict[str, Any] | None) -> dict[str, Any] | None:
        """Build one model-visible notice only when context crosses a rollover threshold."""
        if not isinstance(report, dict) or not report.get("transitioned"):
            return None
        rollover = report.get("rollover") if isinstance(report.get("rollover"), dict) else {}
        urgency = str(rollover.get("urgency") or "unknown")
        if urgency not in {"recommended", "critical"}:
            return None
        pct = rollover.get("context_usage_percent")
        action = "Open a fresh conversation and roll over now." if urgency == "critical" else "Prepare a fresh-conversation rollover."
        return {
            "type": "hirda.context_rollover",
            "severity": "critical" if urgency == "critical" else "warning",
            "context_usage_percent": pct,
            "urgency": urgency,
            "message": f"HIRDA context warning: conversation context is {float(pct):.1f}% full. {action}",
            "next_action": report.get("next_action"),
            "handoff_tool": report.get("handoff_tool"),
        }

    @staticmethod
    def _augment_tool_result_context_notice(
        content: bytes,
        content_type: str,
        notice: dict[str, Any] | None,
    ) -> bytes:
        """Append a one-shot rollover warning to an MCP tool result without replacing it."""
        if not notice:
            return content
        notice_text = json.dumps({"HIRDA_CONTEXT_WARNING": notice}, ensure_ascii=False, separators=(",", ":"))

        def inject(payload: Any) -> Any:
            if not isinstance(payload, dict):
                return payload
            result = payload.get("result")
            if not isinstance(result, dict):
                return payload
            blocks = result.get("content")
            if not isinstance(blocks, list):
                return payload
            blocks.append({"type": "text", "text": notice_text})
            return payload

        try:
            if "text/event-stream" in content_type:
                out: list[str] = []
                for line in content.decode("utf-8").splitlines(keepends=True):
                    core = line.rstrip("\r\n")
                    ending = line[len(core):]
                    if core.startswith("data:"):
                        raw = core[5:].strip()
                        try:
                            payload = inject(json.loads(raw))
                            core = "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                        except Exception:
                            pass
                    out.append(core + ending)
                return "".join(out).encode("utf-8")
            payload = inject(json.loads(content.decode("utf-8")))
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception:
            return content

    def _control_tools_payload(self, request_id: Any) -> dict[str, Any]:
        """Minimal valid tools/list response that never depends on Serena.

        This is a recovery path only: if the neutral or selected Serena process
        is temporarily unavailable, ChatGPT still receives MCP Studio controls
        and can select/resume another workspace/session instead of being trapped
        in NO_MANAGED_SESSION_BOUND.
        """
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": self._all_local_tools()},
        }

    async def _resolve_managed_session(self, value: str) -> dict[str, Any]:
        if not self.managed_sessions:
            raise ManagedSessionError("managed sessions are unavailable")
        value = str(value or "").strip()
        try:
            return await self.managed_sessions.get_session(value)
        except KeyError:
            matches = [x for x in await self.managed_sessions.list_sessions() if x.get("name") == value]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ManagedSessionConflict(f"managed session name is ambiguous: {value}")
            raise ManagedSessionError(f"unknown managed session: {value}")

    async def resolve_session_natural(self, gateway_session_id: str, text: str) -> dict[str, Any]:
        """Resolve plain-language session references into a managed session safely.

        Resolution is evidence-first:
        1. exact managed-session id;
        2. unique exact name/workspace/path;
        3. explicit lexical match in the request;
        4. the session already bound to this conversation, but only for continuity language.

        Recency/use-count are presentation hints only. They never authorize an automatic
        cross-session switch. If a fresh conversation asks for "the previous session" while
        multiple candidates exist and no lineage/name is available, fail closed and return
        candidates instead of guessing.
        """
        if not self.managed_sessions:
            raise ManagedSessionError("managed sessions are unavailable")
        query = str(text or "").strip()
        if not query:
            raise ManagedSessionError("session request is required")

        sessions = await self.managed_sessions.list_sessions()
        if not sessions:
            raise ManagedSessionError("no managed sessions are available")

        lowered = query.casefold()
        norm = re.sub(r"[^a-z0-9ก-๙_/.-]+", " ", lowered).strip()
        tokens = {t for t in norm.split() if len(t) >= 2}

        continuity_words = (
            "continue", "resume", "same", "previous", "last", "before", "เดิม", "ต่อ", "ต่อจาก",
            "เมื่อกี้", "ก่อน", "แชตก่อน", "งานเดิม", "ไปต่อ", "กลับไป",
        )
        wants_continuity = any(word in lowered for word in continuity_words)

        current_gateway = await self.db.get_gateway_session(gateway_session_id)
        current_managed = str(current_gateway.get("managed_session_id") or "").strip()

        def recent_key(item: dict[str, Any]) -> tuple[str, int]:
            return (
                str(item.get("last_used_at") or ""),
                int(item.get("use_count") or 0),
            )

        def ambiguous(reason: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
            ordered = sorted(candidates, key=recent_key, reverse=True)
            return {
                "session": None,
                "confidence": 0.0,
                "reason": reason,
                "candidates": ordered[:5],
            }

        # Managed-session id is globally unique and is the strongest selector.
        id_matches = [item for item in sessions if lowered == str(item.get("id") or "").casefold()]
        if id_matches:
            return {
                "session": id_matches[0],
                "confidence": 1.0,
                "reason": "exact-id",
                "candidates": id_matches,
            }

        # Exact human-readable selectors are safe only when they identify one session.
        # Workspace/path can legitimately have several durable sessions, so never pick
        # the first row returned by the database.
        exact_groups = (
            ("name", "exact-name"),
            ("workspace_key", "exact-workspace"),
            ("project_path", "exact-project-path"),
        )
        for field, reason in exact_groups:
            matches = [
                item for item in sessions
                if lowered == str(item.get(field) or "").casefold()
            ]
            if len(matches) == 1:
                return {
                    "session": matches[0],
                    "confidence": 0.99,
                    "reason": reason,
                    "candidates": matches,
                }
            if len(matches) > 1:
                if wants_continuity and current_managed:
                    current_match = next(
                        (item for item in matches if str(item.get("id") or "") == current_managed),
                        None,
                    )
                    if current_match is not None:
                        return {
                            "session": current_match,
                            "confidence": 0.99,
                            "reason": f"{reason};current-conversation-binding",
                            "candidates": matches,
                        }
                return ambiguous(f"ambiguous-{reason.removeprefix('exact-')}", matches)

        # Natural-language topic/name evidence. Only lexical evidence contributes to
        # selection score. Recency is deliberately excluded from the score.
        scored: list[tuple[float, dict[str, Any], list[str]]] = []
        for item in sessions:
            values = [
                str(item.get("name") or ""),
                str(item.get("workspace_key") or ""),
                str(item.get("project_path") or ""),
            ]
            item_tokens: set[str] = set()
            for value in values:
                item_tokens.update(
                    t for t in re.sub(r"[^a-z0-9ก-๙_/.-]+", " ", value.casefold()).split() if len(t) >= 2
                )
            overlap = sorted(tokens & item_tokens)
            score = float(len(overlap) * 10)
            reasons: list[str] = []
            if overlap:
                reasons.append("token:" + ",".join(overlap[:6]))
            if any(lowered and lowered in value.casefold() for value in values):
                score += 35.0
                reasons.append("substring")
            scored.append((score, item, reasons))

        scored.sort(
            key=lambda row: (row[0], recent_key(row[1])),
            reverse=True,
        )
        viable = [row for row in scored if row[0] > 0]

        if viable:
            top_score = viable[0][0]
            top_rows = [row for row in viable if row[0] == top_score]

            # If explicit lexical evidence ties across several sessions, the current
            # conversation binding is a valid lineage tiebreaker for continuity requests.
            if len(top_rows) > 1:
                if wants_continuity and current_managed:
                    current_row = next(
                        (row for row in top_rows if str(row[1].get("id") or "") == current_managed),
                        None,
                    )
                    if current_row is not None:
                        return {
                            "session": current_row[1],
                            "confidence": 0.90,
                            "reason": ";".join(current_row[2] + ["current-conversation-binding"]),
                            "candidates": [row[1] for row in viable[:5]],
                        }
                return ambiguous("ambiguous-lexical-match", [row[1] for row in top_rows])

            top_score, top, top_reasons = top_rows[0]
            second_score = viable[1][0] if len(viable) > 1 else -1.0
            if len(viable) > 1 and top_score - second_score < 5.0:
                return ambiguous("ambiguous-lexical-match", [row[1] for row in viable])

            confidence = min(0.95, 0.65 + min(top_score, 30.0) / 100.0)
            return {
                "session": top,
                "confidence": confidence,
                "reason": ";".join(top_reasons) or "lexical-match",
                "candidates": [row[1] for row in viable[:5]],
            }

        if wants_continuity:
            # A binding already attached to this gateway is hard conversation-local
            # evidence. This must beat every global recency/use-count signal.
            if current_managed:
                current = next(
                    (item for item in sessions if str(item.get("id") or "") == current_managed),
                    None,
                )
                if current is not None:
                    return {
                        "session": current,
                        "confidence": 0.99,
                        "reason": "current-conversation-binding",
                        "candidates": [current],
                    }

            # A fresh/unbound conversation may safely continue the sole available
            # session. With 2+ sessions, there is no lineage evidence: fail closed.
            if len(sessions) == 1:
                return {
                    "session": sessions[0],
                    "confidence": 0.85,
                    "reason": "sole-session-continuity",
                    "candidates": sessions,
                }
            return ambiguous("ambiguous-continuity-no-lineage", sessions)

        raise ManagedSessionError(f"could not resolve session from natural language: {query}")

    async def _record_observed_cross_chat_handoff(
        self,
        gateway_session_id: str,
        managed_session: dict[str, Any],
        *,
        summary: str,
        reason: str,
        actor: str = "chatgpt/mcp",
    ) -> dict[str, Any] | None:
        """Record cross-conversation continuation without forcing ownership transfer."""
        target = await self.db.get_gateway_session(gateway_session_id)
        target_client = str(target.get("client_id") or "")
        gateways = await self.db.list_gateway_sessions_for_managed_session(managed_session["id"])
        source = next(
            (
                item for item in gateways
                if item.get("id") != gateway_session_id
                and item.get("status") == "connected"
                and str(item.get("client_id") or "") != target_client
            ),
            None,
        )
        if source is None:
            source = next(
                (
                    item for item in gateways
                    if item.get("id") != gateway_session_id
                    and str(item.get("client_id") or "") != target_client
                ),
                None,
            )
        if source is None:
            return None
        event = await self.db.record_session_handoff_event(
            managed_session_id=managed_session["id"],
            source_gateway_session_id=str(source["id"]),
            source_studio_session_id=str(source.get("studio_session_id") or "") or None,
            source_client_id=str(source.get("client_id") or "") or None,
            target_gateway_session_id=gateway_session_id,
            target_studio_session_id=str(target.get("studio_session_id") or "") or None,
            target_client_id=target_client or None,
            summary=summary,
            reason=reason,
            state="observed",
        )
        await self.db.add_audit(
            "managed.session.handoff.observed",
            actor=actor,
            target_type="managed_session",
            target_id=managed_session["id"],
            data={
                "handoff_id": event["id"],
                "source_gateway_session_id": source["id"],
                "target_gateway_session_id": gateway_session_id,
                "workspace_key": managed_session.get("workspace_key"),
                "reason": reason,
            },
        )
        return event

    async def _open_upstream_session(self, session: dict[str, Any], server_url: str) -> str | None:
        init_payload = session.get("init_payload") or {}
        if init_payload.get("method") != "initialize":
            raise RuntimeError("gateway session has no reusable initialize payload")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "mcp-studio-gateway/0.9.10",
        }
        async with httpx.AsyncClient(timeout=self.settings.studio.request_timeout_seconds, follow_redirects=False) as client:
            init = await client.post(server_url, headers=headers, json=init_payload)
            if init.status_code >= 400:
                raise RuntimeError(f"upstream reinitialize failed HTTP {init.status_code}")
            upstream_id = init.headers.get("mcp-session-id")
            if upstream_id:
                headers["Mcp-Session-Id"] = upstream_id
            initialized = await client.post(
                server_url, headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            if initialized.status_code not in (200, 202, 204):
                raise RuntimeError(f"upstream initialized notification failed HTTP {initialized.status_code}")
        return upstream_id

    @staticmethod
    def _initialize_response_cache(
        content: bytes,
        *,
        status_code: int,
        content_type: str,
    ) -> dict[str, Any]:
        return {
            "body_b64": base64.b64encode(content).decode("ascii"),
            "status_code": int(status_code),
            "content_type": str(content_type or "application/json"),
        }

    @staticmethod
    def _cached_initialize_response(
        session: dict[str, Any],
        *,
        request_id: Any,
    ) -> tuple[bytes, int, str] | None:
        cache = (session.get("metadata") or {}).get("initialize_response")
        if not isinstance(cache, dict):
            return None
        encoded = cache.get("body_b64")
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
            status_code = int(cache.get("status_code") or 200)
        except (ValueError, TypeError):
            return None
        content_type = str(cache.get("content_type") or "application/json")

        # A reclaimed external transport may choose a different JSON-RPC request
        # id for initialize. Reuse the upstream result/capabilities while making
        # the replay a valid response to the new request.
        try:
            if "text/event-stream" in content_type:
                rewritten: list[str] = []
                for line in content.decode("utf-8").splitlines(keepends=True):
                    if line.startswith("data:"):
                        suffix = "\n" if line.endswith("\n") else ""
                        payload = line[5:].strip()
                        item = json.loads(payload)
                        if isinstance(item, dict) and "id" in item:
                            item["id"] = request_id
                            line = "data: " + json.dumps(
                                item, ensure_ascii=False, separators=(",", ":")
                            ) + suffix
                    rewritten.append(line)
                content = "".join(rewritten).encode("utf-8")
            else:
                item = json.loads(content)
                if isinstance(item, dict) and "id" in item:
                    item["id"] = request_id
                    content = json.dumps(
                        item, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            # If an older/nonstandard upstream response cannot be rewritten,
            # do not risk replaying an invalid initialize response.
            return None
        return content, status_code, content_type

    async def _close_upstream_session(self, server_url: str | None, upstream_id: str | None) -> None:
        if not server_url or not upstream_id:
            return
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": upstream_id,
        }
        try:
            async with httpx.AsyncClient(timeout=min(4.0, self.settings.studio.request_timeout_seconds)) as client:
                await client.delete(server_url, headers=headers)
        except Exception:
            pass

    async def bind_managed_session(self, gateway_session_id: str, managed_session_id: str, *, actor: str = "mcp") -> dict[str, Any]:
        if not (self.managed_sessions and self.managed_sessions.enabled):
            raise ManagedSessionError("managed sessions are disabled")
        gateway = await self.db.get_gateway_session(gateway_session_id)
        managed = await self.managed_sessions.ensure_running(managed_session_id)
        endpoint = managed.get("endpoint") or f"http://127.0.0.1:{managed['port']}/mcp"
        new_upstream_id = await self._open_upstream_session(gateway, endpoint)
        old_url = gateway.get("upstream_url") or next(
            (s.url for s in self.settings.servers if s.id == gateway["server_id"]), None
        )
        old_id = gateway.get("upstream_session_id")
        updated = await self.db.bind_gateway_managed_session(
            gateway_session_id, managed_session_id=managed["id"], upstream_url=endpoint,
            upstream_session_id=new_upstream_id,
        )
        try:
            managed = await self.db.touch_managed_session(managed["id"])
        except KeyError:
            # Compatibility for externally supplied/fake managed pools; routing
            # remains authoritative even if lifecycle telemetry is unavailable.
            pass
        await self._close_upstream_session(old_url, old_id)
        await self.db.add_audit(
            "managed.gateway.attach", actor=actor, target_type="gateway_session", target_id=gateway_session_id,
            data={"managed_session_id": managed["id"], "workspace_key": managed["workspace_key"]},
        )
        return {"gateway_session": updated, "managed_session": managed}

    async def detach_managed_session(self, gateway_session_id: str, *, actor: str = "mcp") -> dict[str, Any]:
        gateway = await self.db.get_gateway_session(gateway_session_id)
        server = next((s for s in self.settings.servers if s.id == gateway["server_id"] and s.enabled), None)
        if server is None:
            raise ManagedSessionError("base Serena server is unavailable")
        new_upstream_id = await self._open_upstream_session(gateway, server.url)
        old_url = gateway.get("upstream_url") or server.url
        old_id = gateway.get("upstream_session_id")
        updated = await self.db.bind_gateway_managed_session(
            gateway_session_id, managed_session_id=None, upstream_url=server.url,
            upstream_session_id=new_upstream_id,
        )
        if old_url != server.url or old_id != new_upstream_id:
            await self._close_upstream_session(old_url, old_id)
        await self.db.add_audit(
            "managed.gateway.detach", actor=actor, target_type="gateway_session", target_id=gateway_session_id,
        )
        return {"gateway_session": updated, "managed_session": None}

    async def _reconcile_gateway_managed_binding(
        self, gateway_session_id: str, session: dict[str, Any]
    ) -> dict[str, Any]:
        """Make the durable Studio-session project pin authoritative.

        Gateway transports are ephemeral. A reconnected/reclaimed transport may
        be created after the logical session was pinned, or a sibling transport
        may still carry an older binding. Reconcile lazily before forwarding any
        request so all transports converge on the logical session's pin.
        """
        logical = await self.db.get_session(session["studio_session_id"])
        desired = logical.get("managed_session_id")
        current = session.get("managed_session_id")
        if desired == current:
            return session
        if desired and not (self.managed_sessions and self.managed_sessions.enabled):
            raise HTTPException(
                status_code=503,
                detail="Managed project pin exists but managed sessions are disabled",
            )
        if desired:
            result = await self.bind_managed_session(
                gateway_session_id, str(desired), actor="system/reconcile"
            )
            return result["gateway_session"]
        if current:
            result = await self.detach_managed_session(
                gateway_session_id, actor="system/reconcile"
            )
            return result["gateway_session"]
        return session

    async def _resolve_graft_workspace(
        self, gateway_session_id: str, args: dict[str, Any]
    ) -> str:
        if args.get("workspace"):
            key = str(args.get("workspace") or "").strip()
            await self.db.get_managed_workspace(key)
            return key
        if args.get("session"):
            session = await self._resolve_managed_session(str(args.get("session") or ""))
            return str(session["workspace_key"])
        gateway = await self.db.get_gateway_session(gateway_session_id)
        managed_id = gateway.get("managed_session_id")
        if not managed_id:
            raise GraftError(
                "Graft workspace is required when no managed session is attached"
            )
        session = await self.managed_sessions.get_session(str(managed_id))
        return str(session["workspace_key"])


    @staticmethod
    def _public_session_handoff(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "id",
                "managed_session_id",
                "source_gateway_session_id",
                "target_gateway_session_id",
                "state",
                "summary",
                "reason",
                "context_usage_percent",
                "created_at",
                "expires_at",
                "claimed_at",
                "error",
            )
        }

    @staticmethod
    def _rollover_advice(context_usage_percent: float | None) -> dict[str, Any]:
        if context_usage_percent is None:
            return {
                "context_usage_percent": None,
                "recommended": None,
                "urgency": "unknown",
                "recommended_threshold_percent": 80.0,
                "critical_threshold_percent": 90.0,
            }
        pct = max(0.0, min(float(context_usage_percent), 100.0))
        urgency = "critical" if pct >= 90.0 else "recommended" if pct >= 80.0 else "optional"
        return {
            "context_usage_percent": pct,
            "recommended": pct >= 80.0,
            "urgency": urgency,
            "recommended_threshold_percent": 80.0,
            "critical_threshold_percent": 90.0,
        }


    def context_status_from_gateway(self, gateway: dict[str, Any]) -> dict[str, Any]:
        metadata = gateway.get("metadata") if isinstance(gateway.get("metadata"), dict) else {}
        reading = metadata.get("context_usage") if isinstance(metadata, dict) else None
        if not isinstance(reading, dict) or reading.get("context_usage_percent") is None:
            return {
                "telemetry_available": False,
                "context_usage_percent": None,
                "source": None,
                "observed_at": None,
                "rollover": self._rollover_advice(None),
                "message": "Context usage telemetry is unavailable; HIRDA will not estimate it.",
            }
        pct = float(reading.get("context_usage_percent"))
        advice = self._rollover_advice(pct)
        return {
            "telemetry_available": True,
            "context_usage_percent": advice["context_usage_percent"],
            "source": str(reading.get("source") or "unknown"),
            "observed_at": reading.get("observed_at"),
            "rollover": advice,
            "message": (
                "Context is critical; prepare rollover now."
                if advice["urgency"] == "critical"
                else "Context is near full; prepare rollover."
                if advice["urgency"] == "recommended"
                else "Context usage is below the rollover threshold."
            ),
        }

    async def context_status(self, gateway_session_id: str) -> dict[str, Any]:
        gateway = await self.db.get_gateway_session(gateway_session_id)
        status = self.context_status_from_gateway(gateway)
        status["gateway_session_id"] = gateway_session_id
        status["managed_session_id"] = gateway.get("managed_session_id")
        return status

    async def report_context_usage(
        self,
        gateway_session_id: str,
        *,
        context_usage_percent: float,
        source: str = "product_surface",
        actor: str = "chatgpt/mcp",
    ) -> dict[str, Any]:
        gateway = await self.db.get_gateway_session(gateway_session_id)
        managed_id = str(gateway.get("managed_session_id") or "").strip()
        if not managed_id:
            raise ManagedSessionError("CONTEXT_USAGE_REQUIRES_BOUND_SESSION")
        pct = float(context_usage_percent)
        if pct < 0.0 or pct > 100.0:
            raise ManagedSessionError("context_usage_percent must be between 0 and 100")
        clean_source = str(source or "product_surface").strip()[:120] or "product_surface"
        previous = self.context_status_from_gateway(gateway)
        advice = self._rollover_advice(pct)
        observed_at = time.time()
        updated = await self.db.update_gateway_session_metadata(
            gateway_session_id,
            {
                "context_usage": {
                    "context_usage_percent": pct,
                    "source": clean_source,
                    "observed_at": observed_at,
                }
            },
        )
        managed = await self.managed_sessions.get_session(managed_id)
        dedupe_key = f"session-context-rollover:{gateway_session_id}"
        urgency = str(advice["urgency"])
        previous_urgency = str((previous.get("rollover") or {}).get("urgency") or "unknown")
        if advice["recommended"]:
            severity = "critical" if urgency == "critical" else "warning"
            kind = "managed.session.context_critical" if urgency == "critical" else "managed.session.context_near_full"
            action_text = "Open a new conversation and roll over now." if urgency == "critical" else "Prepare a conversation rollover."
            await self.db.open_alert(
                dedupe_key,
                kind,
                f"{managed.get('name') or managed.get('workspace_key')} context is {pct:.1f}% full. {action_text}",
                severity=severity,
                data={
                    "gateway_session_id": gateway_session_id,
                    "managed_session_id": managed_id,
                    "workspace_key": managed.get("workspace_key"),
                    "context_usage_percent": pct,
                    "source": clean_source,
                    "rollover": advice,
                },
            )
        else:
            await self.db.resolve_alert(dedupe_key)

        should_emit_transition = urgency != previous_urgency and (
            urgency in {"recommended", "critical"}
            or previous_urgency in {"recommended", "critical"}
        )
        if should_emit_transition:
            event_kind = (
                "managed.session.context_critical"
                if urgency == "critical"
                else "managed.session.context_near_full"
                if urgency == "recommended"
                else "managed.session.context_recovered"
            )
            await self.db.add_event(
                event_kind,
                f"{managed.get('name') or managed_id} context usage {pct:.1f}% ({urgency})",
                data={
                    "gateway_session_id": gateway_session_id,
                    "managed_session_id": managed_id,
                    "workspace_key": managed.get("workspace_key"),
                    "context_usage_percent": pct,
                    "source": clean_source,
                    "previous_urgency": previous_urgency,
                    "urgency": urgency,
                },
            )
        await self.db.add_audit(
            "managed.session.context.report",
            actor=actor,
            target_type="managed_session",
            target_id=managed_id,
            data={
                "gateway_session_id": gateway_session_id,
                "context_usage_percent": pct,
                "source": clean_source,
                "urgency": urgency,
            },
        )
        status = self.context_status_from_gateway(updated)
        return {
            **status,
            "gateway_session_id": gateway_session_id,
            "managed_session": managed,
            "alert_open": bool(advice["recommended"]),
            "transitioned": should_emit_transition,
            "handoff_tool": "mcpstudio_handoff_session" if advice["recommended"] else None,
            "next_action": (
                "rollover-now"
                if urgency == "critical"
                else "prepare-rollover"
                if urgency == "recommended"
                else "continue"
            ),
        }

    async def prepare_session_handoff(
        self,
        gateway_session_id: str,
        *,
        summary: str,
        reason: str | None = None,
        ttl_seconds: int = 1800,
        context_usage_percent: float | None = None,
        actor: str = "chatgpt/mcp",
    ) -> dict[str, Any]:
        gateway = await self.db.get_gateway_session(gateway_session_id)
        managed_id = str(gateway.get("managed_session_id") or "").strip()
        if not managed_id:
            raise ManagedSessionError("SESSION_HANDOFF_REQUIRES_BOUND_SESSION")
        clean_summary = str(summary or "").strip()
        if not clean_summary:
            raise ManagedSessionError("session handoff summary is required")
        if len(clean_summary) > 12000:
            raise ManagedSessionError("session handoff summary exceeds 12000 characters")
        clean_reason = str(reason or "").strip()[:500] or None
        pct: float | None = None
        if context_usage_percent is not None:
            pct = float(context_usage_percent)
            if pct < 0.0 or pct > 100.0:
                raise ManagedSessionError("context_usage_percent must be between 0 and 100")
        ttl = max(60, min(int(ttl_seconds), 86400))
        if pct is None:
            reported = await self.context_status(gateway_session_id)
            if reported.get("telemetry_available"):
                pct = float(reported["context_usage_percent"])
        else:
            await self.report_context_usage(
                gateway_session_id,
                context_usage_percent=pct,
                source="handoff_caller",
                actor=actor,
            )

        # The raw token is returned exactly once and never persisted. Only its
        # SHA-256 digest is stored so DB/audit access cannot claim a handoff.
        claim_token = "msh_" + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
        try:
            handoff = await self.db.create_session_handoff(
                token_hash=token_hash,
                managed_session_id=managed_id,
                source_gateway_session_id=gateway_session_id,
                source_studio_session_id=str(gateway.get("studio_session_id") or "") or None,
                source_client_id=str(gateway.get("client_id") or "") or None,
                summary=clean_summary,
                reason=clean_reason,
                context_usage_percent=pct,
                ttl_seconds=ttl,
            )
        except ValueError as exc:
            raise ManagedSessionError(str(exc)) from exc
        managed = await self.managed_sessions.get_session(managed_id)
        await self.db.add_audit(
            "managed.session.handoff.prepare",
            actor=actor,
            target_type="managed_session",
            target_id=managed_id,
            data={
                "handoff_id": handoff["id"],
                "source_gateway_session_id": gateway_session_id,
                "workspace_key": managed.get("workspace_key"),
                "expires_at": handoff.get("expires_at"),
                "context_usage_percent": pct,
                "reason": clean_reason,
            },
        )
        return {
            "handoff": self._public_session_handoff(handoff),
            "claim_token": claim_token,
            "managed_session": managed,
            "ownership_transferred": False,
            "source_remains_attached_until_claim": True,
            "rollover": self._rollover_advice(pct),
            "next_step": "Open the new ChatGPT/MCP conversation and call mcpstudio_accept_handoff with claim_token.",
        }

    async def accept_session_handoff(
        self,
        gateway_session_id: str,
        *,
        token: str,
        actor: str = "chatgpt/mcp",
    ) -> dict[str, Any]:
        clean_token = str(token or "").strip()
        if len(clean_token) < 16:
            raise ManagedSessionError("invalid session handoff token")
        token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
        target_gateway = await self.db.get_gateway_session(gateway_session_id)
        try:
            reserved = await self.db.reserve_session_handoff(
                token_hash,
                target_gateway_session_id=gateway_session_id,
                target_studio_session_id=str(target_gateway.get("studio_session_id") or "") or None,
                target_client_id=str(target_gateway.get("client_id") or "") or None,
            )
        except KeyError as exc:
            raise ManagedSessionError("unknown session handoff token") from exc
        except ValueError as exc:
            raise ManagedSessionError(str(exc)) from exc

        handoff_id = str(reserved["id"])
        managed_id = str(reserved["managed_session_id"])
        previous_target_managed = str(target_gateway.get("managed_session_id") or "").strip() or None
        if previous_target_managed == managed_id:
            await self.db.release_session_handoff(
                handoff_id,
                error="target gateway is already attached to handoff session",
            )
            raise ManagedSessionError("target gateway is already attached to the handoff session")

        target_bound = False
        try:
            attached = await self.bind_managed_session(
                gateway_session_id,
                managed_id,
                actor=f"{actor}/handoff-claim",
            )
            target_bound = True

            source_gateway_id = str(reserved["source_gateway_session_id"])
            try:
                source_gateway = await self.db.get_gateway_session(source_gateway_id)
            except KeyError:
                source_gateway = None

            source_closed = False
            if source_gateway is not None:
                source_studio_id = str(source_gateway.get("studio_session_id") or "")
                if source_studio_id:
                    await self.db.clear_logical_session_managed_pin(
                        source_studio_id,
                        expected_managed_session_id=managed_id,
                    )
                    try:
                        await self.db.disconnect_session(source_studio_id)
                    except KeyError:
                        pass
                old_url = source_gateway.get("upstream_url")
                old_upstream_id = source_gateway.get("upstream_session_id")
                await self.db.close_gateway_session(
                    source_gateway_id,
                    error="session handoff claimed by another conversation",
                )
                await self.db.resolve_alert(f"session-context-rollover:{source_gateway_id}")
                source_closed = True
                if old_url or old_upstream_id:
                    await self._close_upstream_session(old_url, old_upstream_id)

            claimed = await self.db.complete_session_handoff(handoff_id)
            await self.db.add_audit(
                "managed.session.handoff.claim",
                actor=actor,
                target_type="managed_session",
                target_id=managed_id,
                data={
                    "handoff_id": handoff_id,
                    "source_gateway_session_id": source_gateway_id,
                    "target_gateway_session_id": gateway_session_id,
                    "source_gateway_closed": source_closed,
                },
            )
            return {
                "handoff": self._public_session_handoff(claimed),
                "gateway_session": attached["gateway_session"],
                "managed_session": attached["managed_session"],
                "context": {
                    "summary": claimed.get("summary") or "",
                    "reason": claimed.get("reason"),
                },
                "ownership_transferred": True,
                "source_gateway_closed": source_closed,
                "claim_consumed": True,
            }
        except Exception as exc:
            if target_bound:
                try:
                    if previous_target_managed:
                        await self.bind_managed_session(
                            gateway_session_id,
                            previous_target_managed,
                            actor=f"{actor}/handoff-rollback",
                        )
                    else:
                        await self.detach_managed_session(
                            gateway_session_id,
                            actor=f"{actor}/handoff-rollback",
                        )
                except Exception:
                    pass
            try:
                await self.db.release_session_handoff(handoff_id, error=str(exc))
            except Exception:
                pass
            if isinstance(exc, ManagedSessionError):
                raise
            raise ManagedSessionError(f"session handoff claim failed: {exc}") from exc

    async def world_authoring_audit(
        self,
        *,
        workspace: str,
        events: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        if not self.world_authoring:
            raise WorldAuthoringError("HIRDA Earth world authoring integration is unavailable")
        workspace_selector = str(workspace or "")
        target = await self.world_authoring.resolve_workspace(workspace_selector)
        normalized = self.world_authoring.validate_audit_events(events)
        for event in normalized:
            await self.db.add_audit(
                f"world_authoring.{event['kind']}",
                actor=actor,
                target_type="managed_workspace",
                target_id=str(target.get("key") or workspace_selector),
                data={
                    **event,
                    "preview_only": True,
                    "world_authority_changed": False,
                },
            )
        return {
            "workspace": {
                "key": target.get("key"),
                "name": target.get("name"),
                "project_path": target.get("project_path"),
            },
            "accepted": len(normalized),
            "audit_only": True,
            "world_authority_changed": False,
            "asset_promoted_by_hirda": False,
        }

    async def world_authoring_preview(
        self,
        *,
        workspace: str,
        proposal: dict[str, Any],
        providers: list[dict[str, Any]] | None = None,
        actor: str,
    ) -> dict[str, Any]:
        if not self.world_authoring:
            raise WorldAuthoringError("HIRDA Earth world authoring integration is unavailable")
        if not isinstance(proposal, dict):
            raise WorldAuthoringError("proposal must be an object")
        raw_providers = providers or []
        if any(not isinstance(provider, dict) for provider in raw_providers):
            raise WorldAuthoringError("providers must be an array of objects")
        result = await self.world_authoring.preview(
            workspace=str(workspace or ""),
            proposal=proposal,
            providers=[dict(provider) for provider in raw_providers],
        )
        proposal_fingerprint = hashlib.sha256(
            json.dumps(
                proposal,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        validation = (
            result.get("earth_validation", {})
            if isinstance(result.get("earth_validation"), dict)
            else {}
        )
        await self.db.add_audit(
            "world_authoring.preview",
            actor=actor,
            target_type="managed_workspace",
            target_id=str(workspace or ""),
            outcome="success" if validation.get("valid") is not False else "rejected",
            data={
                "proposal_id": proposal.get("proposalId"),
                "proposal_kind": proposal.get("kind"),
                "proposal_fingerprint": proposal_fingerprint,
                "validation_id": validation.get("validationId"),
                "requires_approval": validation.get("requiresApproval"),
                "valid": validation.get("valid"),
                "preview_only": True,
                "world_authority_changed": False,
                "asset_promoted": False,
            },
        )
        return {
            **result,
            "proposal_fingerprint": proposal_fingerprint,
        }

    async def world_authoring_promote(
        self,
        *,
        workspace: str,
        proposal: dict[str, Any],
        commands: list[dict[str, Any]],
        validation_id: str,
        rationale: str,
        actor: str,
    ) -> dict[str, Any]:
        if not self.world_authoring:
            raise WorldAuthoringError("HIRDA Earth world authoring integration is unavailable")
        target = await self.world_authoring.resolve_workspace(str(workspace or ""))
        result = await self.world_authoring.promote(
            proposal=proposal,
            commands=commands,
            validation_id=validation_id,
            rationale=rationale,
            authored_by="nova",
        )
        await self.db.add_audit(
            "world_authoring.promote",
            actor=actor,
            target_type="managed_workspace",
            target_id=str(target.get("key") or workspace or ""),
            outcome="success",
            data={
                "proposal_id": proposal.get("proposalId"),
                "proposal_kind": proposal.get("kind"),
                "validation_id": validation_id,
                "authored_by": "nova",
                "earth_mutation_authorized": True,
                "world_authority": "earth-616",
                "hirda_world_authority": False,
            },
        )
        return {
            "workspace": {
                "key": target.get("key"),
                "name": target.get("name"),
                "project_path": target.get("project_path"),
            },
            "earth": result,
            "promoted_by": "earth-616",
            "hirda_world_authority": False,
        }

    async def _handle_management_tool(self, gateway_session_id: str, name: str, args: dict[str, Any]) -> Any:
        if not self.managed_sessions:
            raise ManagedSessionError("managed sessions are unavailable")
        if name == "mcpstudio_list_workspaces":
            return {"workspaces": await self.managed_sessions.list_workspaces()}
        if name == "mcpstudio_register_workspace":
            return {"workspace": await self.managed_sessions.register_workspace(
                key=str(args.get("key") or ""), project_path=str(args.get("project_path") or ""),
                name=(str(args.get("name")) if args.get("name") is not None else None), actor="chatgpt/mcp",
            )}
        if name == "mcpstudio_list_sessions":
            return {"sessions": await self.managed_sessions.list_sessions()}
        if name == "mcpstudio_get_session":
            return {"session": await self._resolve_managed_session(str(args.get("session") or ""))}
        if name == "mcpstudio_rename_session":
            target = await self._resolve_managed_session(str(args.get("session") or ""))
            return {"session": await self.managed_sessions.rename_session(
                target["id"], name=str(args.get("name") or ""), actor="chatgpt/mcp"
            )}
        if name == "mcpstudio_set_permissions":
            target = await self._resolve_managed_session(str(args.get("session") or ""))
            changes = {
                key: args[key]
                for key in ("read", "write", "execute", "destructive", "scope", "fail_closed_unknown")
                if key in args
            }
            session = await self.managed_sessions.update_permissions(target["id"], changes, actor="chatgpt/mcp")
            return {"session": session, "permissions": await self.managed_sessions.permissions(target["id"])}
        if name in {
            "mcpstudio_lane_status",
            "mcpstudio_set_lane_state",
            "mcpstudio_list_capsules",
            "mcpstudio_get_capsule",
            "mcpstudio_set_capsule_contract",
            "mcpstudio_approve_capsule_handoff",
        }:
            if not self.capsules:
                raise ManagedSessionError("HIRDA capsule control is unavailable")
            if name == "mcpstudio_lane_status":
                return {"lanes": await self.capsules.lane_states()}
            if name == "mcpstudio_set_lane_state":
                return await self.capsules.set_lane_state(
                    str(args.get("agent") or ""),
                    str(args.get("state") or ""),
                    reason=(str(args.get("reason")) if args.get("reason") is not None else None),
                    actor="chatgpt/mcp",
                    auto_handoff=bool(args.get("auto_handoff", True)),
                )
            if name == "mcpstudio_list_capsules":
                return await self.capsules.overview(int(args.get("limit") or 100))
            if name == "mcpstudio_get_capsule":
                try:
                    return {"capsule": await self.capsules.get(str(args.get("capsule_id") or ""))}
                except CapsuleNotFound as exc:
                    raise ManagedSessionError("capsule not found") from exc
            if name == "mcpstudio_set_capsule_contract":
                try:
                    return {
                        "capsule": await self.capsules.update_contract(
                            str(args.get("capsule_id") or ""),
                            dict(args.get("contract") or {}),
                        )
                    }
                except CapsuleNotFound as exc:
                    raise ManagedSessionError("capsule not found") from exc
            if name == "mcpstudio_approve_capsule_handoff":
                try:
                    return {
                        "capsule": await self.capsules.approve_handoff(
                            str(args.get("capsule_id") or ""),
                            str(args.get("handoff_id") or ""),
                            approved_by=str(args.get("approved_by") or "chatgpt/mcp"),
                        )
                    }
                except CapsuleNotFound as exc:
                    raise ManagedSessionError("capsule not found") from exc
        if name == "mcpstudio_world_authoring_audit":
            raw_events = args.get("events")
            if not isinstance(raw_events, list):
                raise WorldAuthoringError("events must be an array")
            return await self.world_authoring_audit(
                workspace=str(args.get("workspace") or ""),
                events=[
                    dict(event) if isinstance(event, dict) else event
                    for event in raw_events
                ],
                actor="chatgpt/mcp",
            )
        if name == "mcpstudio_world_authoring_preview":
            raw_providers = args.get("providers") or []
            if not isinstance(raw_providers, list) or any(
                not isinstance(provider, dict) for provider in raw_providers
            ):
                raise WorldAuthoringError("providers must be an array of objects")
            proposal = args.get("proposal")
            if not isinstance(proposal, dict):
                raise WorldAuthoringError("proposal must be an object")
            return await self.world_authoring_preview(
                workspace=str(args.get("workspace") or ""),
                proposal=proposal,
                providers=[dict(provider) for provider in raw_providers],
                actor="chatgpt/mcp",
            )
        if name.startswith("mcpstudio_graft_"):
            if not self.graft:
                raise GraftError("HIRDA Graft integration is unavailable")
            workspace_key = await self._resolve_graft_workspace(gateway_session_id, args)
            if name == "mcpstudio_graft_status":
                return await self.graft.status(workspace_key)
            if name == "mcpstudio_graft_configure":
                result = await self.graft.configure(
                    workspace_key,
                    enabled=(bool(args["enabled"]) if "enabled" in args else None),
                    rollout_percent=(float(args["rollout_percent"]) if "rollout_percent" in args else None),
                    actor="chatgpt/mcp",
                )
                # Graft is a HIRDA sidecar context plane. Do not alter the
                # managed Serena session's write authority; Serena remains the sole
                # editor under its existing lease/permission policy.
                return result
            if name == "mcpstudio_graft_query":
                return await self.graft.query(
                    workspace_key,
                    question=(str(args.get("question")) if args.get("question") is not None else None),
                    tool=(str(args.get("tool")) if args.get("tool") is not None else None),
                    arguments=(dict(args.get("arguments") or {})),
                    request_id=(str(args.get("request_id")) if args.get("request_id") is not None else None),
                    actor="chatgpt/mcp",
                )
            if name == "mcpstudio_graft_rollback":
                return await self.graft.rollback(
                    workspace_key,
                    reason=str(args.get("reason") or "operator-rollback"),
                    actor="chatgpt/mcp",
                )
            if name == "mcpstudio_graft_rearm":
                return await self.graft.rearm(workspace_key, actor="chatgpt/mcp")
        if name == "mcpstudio_session_history":
            target = await self._resolve_managed_session(str(args.get("session") or ""))
            return await self.managed_sessions.history(target["id"])
        if name == "mcpstudio_create_session":
            created = await self.managed_sessions.create_session(
                name=str(args.get("name") or ""), workspace_key=str(args.get("workspace") or ""), actor="chatgpt/mcp",
            )
            attached = None
            if args.get("attach", True):
                attached = await self.bind_managed_session(gateway_session_id, created["id"], actor="chatgpt/mcp")
            return {"session": created, "attached": bool(attached)}
        if name == "mcpstudio_use_workspace":
            target = await self.managed_sessions.ensure_workspace_selector_session(
                workspace=str(args.get("workspace") or ""),
                name=(str(args.get("name")) if args.get("name") is not None else None),
                actor="chatgpt/mcp",
            )
            return await self.bind_managed_session(gateway_session_id, target["id"], actor="chatgpt/mcp")
        if name == "mcpstudio_use_session":
            selector = str(args.get("session") or "").strip()
            natural_request = str(args.get("request") or "").strip()
            if selector:
                try:
                    target = await self._resolve_managed_session(selector)
                    resolution = {"session": target, "confidence": 1.0, "reason": "explicit", "candidates": [target]}
                except ManagedSessionError:
                    resolution = await self.resolve_session_natural(gateway_session_id, selector)
            elif natural_request:
                resolution = await self.resolve_session_natural(gateway_session_id, natural_request)
            else:
                raise ManagedSessionError("session or request is required")
            target = resolution.get("session")
            if not target:
                return {
                    "attached": False,
                    "resolved": False,
                    "reason": resolution.get("reason"),
                    "candidates": resolution.get("candidates") or [],
                }
            attached = await self.bind_managed_session(gateway_session_id, target["id"], actor="chatgpt/mcp")
            observed = None
            if args.get("record_handoff", True):
                observed = await self._record_observed_cross_chat_handoff(
                    gateway_session_id,
                    attached["managed_session"],
                    summary=natural_request or selector or f"Continue {target.get('name') or target.get('workspace_key')}",
                    reason="natural-language-session-continuation" if natural_request or selector != str(target.get("id")) else "session-continuation",
                    actor="chatgpt/mcp",
                )
            return {
                **attached,
                "resolution": {
                    "confidence": resolution.get("confidence"),
                    "reason": resolution.get("reason"),
                },
                "handoff": self._public_session_handoff(observed) if observed else None,
            }
        if name == "mcpstudio_context_status":
            return await self.context_status(gateway_session_id)
        if name == "mcpstudio_report_context_usage":
            return await self.report_context_usage(
                gateway_session_id,
                context_usage_percent=float(args.get("context_usage_percent")),
                source=str(args.get("source") or "product_surface"),
                actor="chatgpt/mcp",
            )
        if name == "mcpstudio_handoff_session":
            return await self.prepare_session_handoff(
                gateway_session_id,
                summary=str(args.get("summary") or ""),
                reason=(str(args.get("reason")) if args.get("reason") is not None else None),
                ttl_seconds=int(args.get("ttl_seconds") or 1800),
                context_usage_percent=(
                    float(args["context_usage_percent"])
                    if args.get("context_usage_percent") is not None
                    else None
                ),
                actor="chatgpt/mcp",
            )
        if name == "mcpstudio_accept_handoff":
            return await self.accept_session_handoff(
                gateway_session_id,
                token=str(args.get("token") or ""),
                actor="chatgpt/mcp",
            )
        if name == "mcpstudio_current_session":
            gateway = await self.db.get_gateway_session(gateway_session_id)
            managed_id = gateway.get("managed_session_id")
            return {
                "gateway_session_id": gateway_session_id,
                "managed_session": await self.managed_sessions.get_session(managed_id) if managed_id else None,
                "context": self.context_status_from_gateway(gateway),
            }
        if name == "mcpstudio_detach_session":
            return await self.detach_managed_session(gateway_session_id, actor="chatgpt/mcp")
        if name == "mcpstudio_close_session":
            target = await self._resolve_managed_session(str(args.get("session") or ""))
            gateway = await self.db.get_gateway_session(gateway_session_id)
            if gateway.get("managed_session_id") == target["id"]:
                await self.detach_managed_session(gateway_session_id, actor="chatgpt/mcp")
            return {"session": await self.managed_sessions.stop_session(target["id"], actor="chatgpt/mcp")}
        raise ManagedSessionError(f"unknown MCP Studio management tool: {name}")

    @staticmethod
    def _proxy_headers(request: Request, upstream_session_id: str | None = None) -> dict[str, str]:
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in {
                "authorization",
                "mcp-session-id",
                "x-mcp-studio-client-id",
                "x-mcp-studio-client-type",
            }
        }
        if upstream_session_id:
            headers["Mcp-Session-Id"] = upstream_session_id
        return headers

    @staticmethod
    def _response_headers(response: httpx.Response) -> dict[str, str]:
        return {k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "mcp-session-id"}

    async def _send(
        self,
        *,
        server_url: str,
        method: str,
        headers: dict[str, str],
        body: bytes,
        stream: bool = True,
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        upstream = client.build_request(method, server_url, headers=headers, content=body)
        try:
            response = await client.send(upstream, stream=stream)
            return client, response
        except Exception:
            await client.aclose()
            raise

    async def _reinitialize_upstream(self, session: dict[str, Any], server_url: str) -> dict[str, Any]:
        upstream_id = await self._open_upstream_session(session, server_url)
        updated = await self.db.reconnect_gateway_session(
            session["id"], upstream_session_id=upstream_id, error=None
        )
        await self.db.add_event(
            "gateway.session.reconnected",
            f"Gateway session {session['id']} reinitialized upstream MCP session",
            server_id=session["server_id"],
            data={
                "gateway_session_id": session["id"],
                "studio_session_id": session["studio_session_id"],
                "managed_session_id": session.get("managed_session_id"),
                "generation": updated["generation"],
                "reconnect_count": updated["reconnect_count"],
            },
        )
        return updated

    async def initialize(
        self,
        *,
        request: Request,
        server: Any,
        body: bytes,
        jsonrpc: dict[str, Any],
        client_id: str,
        client_type: str,
        identity_scope: str,
        identity_source: str,
        ingress_override: dict[str, Any] | None = None,
    ) -> Response:
        observation = self.client_observation(
            request, jsonrpc, identity_scope=identity_scope, identity_source=identity_source
        )
        if client_type == "openai-secure-tunnel":
            observation["openai_like"] = True
        ingress = ingress_override or await self.resolve_ingress(request, server.id)
        if observation.get("openai_like") and client_type == "mcp-http":
            client_type = "openai-chatgpt"
        reclaim_allowed = (
            identity_scope in {"explicit", "conversation"}
            or (identity_scope == "connector" and self.settings.studio.openai_connector_reclaim_enabled)
        )
        session_payload = {
            "client_id": client_id,
            "client_type": client_type,
            "server_id": server.id,
            "metadata": {
                "gateway": "m5.3.2",
                "user_agent": request.headers.get("user-agent", ""),
                "client_observation": observation,
                "reclaim_scope": identity_scope,
                "last_ingress": ingress,
            },
        }
        if reclaim_allowed:
            studio_session, reclaimed = await self.db.reclaim_session(session_payload)
        else:
            studio_session, reclaimed = await self.db.create_session(session_payload), False

        # A durable Studio session owns its project pin. When a transport is
        # reclaimed, initialize it directly against the same dedicated Serena
        # process instead of briefly falling back to the neutral/base Serena.
        managed_session_id = studio_session.get("managed_session_id")
        target_url = server.url
        if managed_session_id:
            if not (self.managed_sessions and self.managed_sessions.enabled):
                raise HTTPException(
                    status_code=503,
                    detail="Managed project pin cannot be restored while managed sessions are disabled",
                )
            managed = await self.managed_sessions.ensure_running(str(managed_session_id))
            target_url = managed.get("endpoint") or f"http://127.0.0.1:{managed['port']}/mcp"

        # ChatGPT may create a fresh external Streamable-HTTP transport for each
        # tool call. Reclaimed logical sessions must keep the same gateway/upstream
        # Serena session or Serena's workspace lease becomes stale immediately.
        if reclaimed:
            reusable = await self.db.find_reusable_gateway_session(
                studio_session_id=studio_session["id"],
                server_id=server.id,
                managed_session_id=(str(managed_session_id) if managed_session_id else None),
            )
            if reusable is not None:
                cached = self._cached_initialize_response(
                    reusable, request_id=jsonrpc.get("id")
                )
                if cached is not None:
                    content, status_code, content_type = cached
                    reusable = await self.db.update_gateway_session_ingress(
                        reusable["id"], ingress
                    )
                    await self.db.touch_gateway_session(reusable["id"])
                    try:
                        await self.db.heartbeat_session(
                            studio_session["id"],
                            {"metadata": {"gateway_reused": True, "last_ingress": ingress}},
                        )
                    except KeyError:
                        pass
                    response_headers = {
                        "Mcp-Session-Id": reusable["id"],
                        "X-MCP-Studio-Session-Id": studio_session["id"],
                        "X-MCP-Studio-Reclaimed": "true",
                        "X-MCP-Studio-Gateway-Reused": "true",
                        "X-MCP-Studio-Generation": str(reusable.get("generation") or 1),
                        "X-MCP-Studio-Identity-Scope": identity_scope,
                        "X-MCP-Studio-Client-Class": (
                            "openai-like" if observation.get("openai_like") else "generic-mcp"
                        ),
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "Content-Type": content_type,
                    }
                    if reusable.get("last_tunnel_id"):
                        response_headers["X-MCP-Studio-Tunnel-Id"] = str(reusable["last_tunnel_id"])
                    if reusable.get("managed_session_id"):
                        response_headers["X-MCP-Studio-Managed-Session-Id"] = str(reusable["managed_session_id"])
                    await self.db.add_event(
                        "gateway.session.reused",
                        f"Gateway session {reusable['id']} reused for {client_id}",
                        server_id=server.id,
                        data={
                            "gateway_session_id": reusable["id"],
                            "studio_session_id": studio_session["id"],
                            "managed_session_id": reusable.get("managed_session_id"),
                        },
                    )
                    return Response(
                        content=content,
                        status_code=status_code,
                        headers=response_headers,
                    )

        headers = self._proxy_headers(request)
        client, response = await self._send(
            server_url=target_url,
            method=request.method,
            headers=headers,
            body=body,
            stream=True,
        )
        content = await response.aread()
        upstream_id = response.headers.get("mcp-session-id")
        response_headers = self._response_headers(response)
        status_code = response.status_code
        content_type = response.headers.get("content-type", "application/json")
        await response.aclose()
        await client.aclose()
        if status_code >= 400:
            return Response(content=content, status_code=status_code, headers=response_headers)

        params = jsonrpc.get("params") if isinstance(jsonrpc.get("params"), dict) else {}
        gateway_session = await self.db.create_gateway_session(
            studio_session_id=studio_session["id"],
            client_id=client_id,
            client_type=client_type,
            server_id=server.id,
            upstream_session_id=upstream_id,
            protocol_version=params.get("protocolVersion"),
            init_payload=jsonrpc,
            metadata={
                "reclaimed_studio_session": reclaimed,
                "identity_scope": identity_scope,
                "identity_source": identity_source,
                "client_observation": observation,
                "ingress": ingress,
                "initialize_response": self._initialize_response_cache(
                    content,
                    status_code=status_code,
                    content_type=content_type,
                ),
            },
            ingress=ingress,
            managed_session_id=(str(managed_session_id) if managed_session_id else None),
            upstream_url=target_url,
        )
        response_headers["Mcp-Session-Id"] = gateway_session["id"]
        response_headers["X-MCP-Studio-Session-Id"] = studio_session["id"]
        response_headers["X-MCP-Studio-Reclaimed"] = "true" if reclaimed else "false"
        response_headers["X-MCP-Studio-Generation"] = str(gateway_session["generation"])
        response_headers["X-MCP-Studio-Identity-Scope"] = identity_scope
        response_headers["X-MCP-Studio-Client-Class"] = "openai-like" if observation.get("openai_like") else "generic-mcp"
        if gateway_session.get("last_tunnel_id"):
            response_headers["X-MCP-Studio-Tunnel-Id"] = str(gateway_session["last_tunnel_id"])
        if gateway_session.get("managed_session_id"):
            response_headers["X-MCP-Studio-Managed-Session-Id"] = str(gateway_session["managed_session_id"])
        response_headers["Cache-Control"] = "no-store"
        response_headers["X-Content-Type-Options"] = "nosniff"
        await self.db.add_event(
            "gateway.session.opened",
            f"Gateway session {gateway_session['id']} opened for {client_id}",
            server_id=server.id,
            data={
                "gateway_session_id": gateway_session["id"],
                "studio_session_id": studio_session["id"],
                "reclaimed": reclaimed,
                "identity_scope": identity_scope,
                "openai_like": observation.get("openai_like"),
                "client_info_name": observation.get("client_info_name"),
                "tunnel_id": gateway_session.get("last_tunnel_id"),
                "ingress_host": gateway_session.get("ingress_host"),
                "attribution_confidence": gateway_session.get("attribution_confidence"),
            },
        )
        if observation.get("openai_like"):
            await self.db.add_event(
                "gateway.openai_client.observed",
                f"OpenAI/ChatGPT-like MCP client observed on {gateway_session['id']}",
                server_id=server.id,
                data={
                    "gateway_session_id": gateway_session["id"],
                    "studio_session_id": studio_session["id"],
                    "identity_scope": identity_scope,
                    "client_info_name": observation.get("client_info_name"),
                    "client_info_version": observation.get("client_info_version"),
                    "observed_header_names": observation.get("observed_header_names", []),
                },
            )
        return Response(content=content, status_code=status_code, headers=response_headers)

    async def proxy_existing(
        self,
        *,
        request: Request,
        server: Any,
        body: bytes,
        gateway_session_id: str,
        ingress_override: dict[str, Any] | None = None,
    ):
        try:
            session = await self.db.get_gateway_session(gateway_session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown MCP Studio gateway session")
        if session["server_id"] != server.id:
            raise HTTPException(status_code=409, detail="Gateway session belongs to another MCP server")
        if session["status"] == "closed":
            raise HTTPException(status_code=410, detail="Gateway session is closed; initialize a new transport session")

        ingress = ingress_override or await self.resolve_ingress(request, server.id)
        try:
            session = await self.db.update_gateway_session_ingress(gateway_session_id, ingress)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown MCP Studio gateway session")
        try:
            await self.db.heartbeat_session(
                session["studio_session_id"],
                {"metadata": {"gateway_last_seen": True, "last_ingress": ingress}},
            )
        except KeyError:
            pass
        await self.db.touch_gateway_session(gateway_session_id)
        session = await self.db.get_gateway_session(gateway_session_id)
        session = await self._reconcile_gateway_managed_binding(gateway_session_id, session)
        header_context = self.context_usage_from_request(request)
        context_report: dict[str, Any] | None = None
        if header_context is not None and session.get("managed_session_id"):
            pct, source = header_context
            try:
                context_report = await self.report_context_usage(
                    gateway_session_id,
                    context_usage_percent=pct,
                    source=source,
                    actor="gateway/header",
                )
                session = await self.db.get_gateway_session(gateway_session_id)
            except (ManagedSessionError, KeyError):
                # Context telemetry is advisory UX metadata and must never break MCP transport.
                pass
        context_notice = self._context_transition_notice(context_report)

        jsonrpc = self.parse_jsonrpc(body)
        rpc_method = jsonrpc.get("method") if jsonrpc else None
        params = jsonrpc.get("params") if jsonrpc and isinstance(jsonrpc.get("params"), dict) else {}
        tool_name = str(params.get("name") or "") if rpc_method == "tools/call" else ""
        tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        reflex_id: str | None = None

        def _decorate_local_response(response: Response) -> Response:
            response.headers["Mcp-Session-Id"] = gateway_session_id
            response.headers["X-MCP-Studio-Session-Id"] = session["studio_session_id"]
            response.headers["X-MCP-Studio-Generation"] = str(session.get("generation") or 1)
            if session.get("last_tunnel_id"):
                response.headers["X-MCP-Studio-Tunnel-Id"] = str(session["last_tunnel_id"])
            if session.get("managed_session_id"):
                response.headers["X-MCP-Studio-Managed-Session-Id"] = str(session["managed_session_id"])
            if isinstance(context_report, dict) and context_report.get("telemetry_available"):
                response.headers["X-HIRDA-Context-Usage-Percent"] = str(context_report.get("context_usage_percent"))
                response.headers["X-HIRDA-Context-Urgency"] = str((context_report.get("rollover") or {}).get("urgency") or "unknown")
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        def local_tool_response(payload: Any, *, is_error: bool = False) -> Response:
            visible_payload = payload
            if context_notice and isinstance(payload, dict):
                visible_payload = {**payload, "context_notice": context_notice}
            return _decorate_local_response(
                self._jsonrpc_tool_response(
                    jsonrpc.get("id") if jsonrpc else None, visible_payload, is_error=is_error
                )
            )

        def local_backend_tool_response(result: dict[str, Any]) -> Response:
            visible_result = dict(result or {})
            if context_notice:
                blocks = visible_result.get("content")
                if isinstance(blocks, list):
                    blocks.append(
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"HIRDA_CONTEXT_WARNING": context_notice},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
            return _decorate_local_response(
                self._jsonrpc_raw_tool_response(
                    jsonrpc.get("id") if jsonrpc else None, visible_result
                )
            )

        def local_tools_list_response() -> Response:
            payload = self._control_tools_payload(jsonrpc.get("id") if jsonrpc else None)
            return _decorate_local_response(
                Response(
                    content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    status_code=200,
                    media_type="application/json",
                )
            )

        async def record_reflex_outcome(status_code: int, transport: str) -> None:
            if not reflex_id:
                return
            record = {
                "kind": "outcome",
                "reflex_id": reflex_id,
                "request_id": jsonrpc.get("id") if jsonrpc else None,
                "workspace_key": (
                    managed_session.get("workspace_key")
                    if isinstance(managed_session, dict)
                    else None
                ),
                "tool": tool_name,
                "outcome_level": "transport_only",
                "transport": transport,
                "upstream_status": int(status_code),
                "http_success": int(status_code) < 400,
            }
            append_dataset_record(self.settings.studio, record)
            await self.db.add_event(
                "gateway.reflex.tool_outcome",
                f"HIRDA Reflex upstream transport outcome HTTP {int(status_code)}.",
                server_id=server.id,
                data={
                    "reflex_id": reflex_id,
                    "gateway_session_id": gateway_session_id,
                    "studio_session_id": session["studio_session_id"],
                    **record,
                },
            )

        if request.method == "POST" and rpc_method == "tools/call" and tool_name.startswith("mcpstudio_"):
            try:
                result = await self._handle_management_tool(gateway_session_id, tool_name, tool_args)
                session = await self.db.get_gateway_session(gateway_session_id)
                return local_tool_response(result)
            except (ManagedSessionError, ManagedSessionConflict, WorkspaceNotAllowed, GraftError, WorldAuthoringError, KeyError) as exc:
                return local_tool_response({"error": str(exc)}, is_error=True)

        if request.method == "POST" and rpc_method == "tools/call" and self.managed_sessions and self.managed_sessions.enabled:
            if self.settings.studio.managed_session_require_binding_for_tools and not session.get("managed_session_id"):
                return local_tool_response(
                    {
                        "error": "NO_MANAGED_SESSION_BOUND",
                        "message": "Select a project first with mcpstudio_use_workspace, mcpstudio_create_session, or mcpstudio_use_session.",
                    },
                    is_error=True,
                )
            managed_session: dict[str, Any] | None = None
            if session.get("managed_session_id") and tool_name == "activate_project":
                try:
                    managed_session = await self.db.get_managed_session(str(session["managed_session_id"]))
                except KeyError:
                    return local_tool_response(
                        {
                            "error": "TOOL_PERMISSION_SESSION_MISSING",
                            "message": "Pinned managed session record is missing.",
                        },
                        is_error=True,
                    )
                requested_project = str(tool_args.get("project") or "").strip()
                pinned_path = str(managed_session.get("project_path") or "").strip()
                pinned_key = str(managed_session.get("workspace_key") or "").strip()
                same_path = False
                if requested_project and pinned_path:
                    try:
                        requested_path = Path(requested_project).expanduser()
                        if requested_path.is_absolute():
                            same_path = requested_path.resolve(strict=False) == Path(pinned_path).expanduser().resolve(strict=False)
                    except OSError:
                        same_path = False
                if not requested_project or not (same_path or requested_project == pinned_key):
                    return local_tool_response(
                        {
                            "error": "SESSION_PROJECT_PINNED",
                            "message": f"activate_project may only re-activate pinned workspace {pinned_key!r}.",
                            "workspace_key": pinned_key,
                            "project_path": pinned_path,
                        },
                        is_error=True,
                    )
            elif session.get("managed_session_id") and tool_name in set(self.settings.studio.managed_session_blocked_tools):
                return local_tool_response(
                    {
                        "error": "SESSION_PROJECT_PINNED",
                        "message": f"Tool {tool_name} is blocked because this transport is pinned to managed session {session['managed_session_id']}.",
                    },
                    is_error=True,
                )
            backend_tool = bool(
                self.integrations is not None
                and self.integrations.is_backend_tool(tool_name)
            )
            if session.get("managed_session_id") and (
                self.settings.studio.managed_session_tool_permissions_enabled or backend_tool
            ):
                try:
                    if managed_session is None:
                        managed_session = await self.db.get_managed_session(str(session["managed_session_id"]))
                except KeyError:
                    return local_tool_response(
                        {
                            "error": "TOOL_PERMISSION_SESSION_MISSING",
                            "message": "Permission policy cannot be evaluated because the managed session record is missing.",
                        },
                        is_error=True,
                    )
                policy_started = time.perf_counter()
                declared_category = (
                    self.integrations.backend_permission(tool_name)
                    if self.integrations is not None
                    else None
                )
                decision = decide_tool_call(
                    self.settings.studio,
                    managed_session,
                    tool_name,
                    tool_args,
                    declared_category=declared_category,
                )
                policy_ms = round((time.perf_counter() - policy_started) * 1000.0, 3)
                if not decision.allowed:
                    return local_tool_response(
                        {
                            "error": decision.code,
                            "message": decision.message,
                            "tool": tool_name,
                            "permission_class": decision.category,
                            "workspace_key": managed_session.get("workspace_key"),
                            "policy": decision.policy,
                        },
                        is_error=True,
                    )

                reflex_started = time.perf_counter()
                reflex_decision, reflex_features = evaluate_reflex_tool_call(
                    self.settings.studio,
                    tool_name,
                    tool_args,
                    decision.category,
                )
                reflex_ms = round((time.perf_counter() - reflex_started) * 1000.0, 3)
                reflex_id = decision_fingerprint(
                    managed_session,
                    reflex_features,
                    jsonrpc.get("id") if jsonrpc else None,
                )

                # Jev is teacher/shadow evidence only. It never grants or blocks
                # authority in the HIRDA runtime path.
                teacher_started = time.perf_counter()
                jev_decision = await evaluate_jev_tool_call(
                    self.settings.studio,
                    managed_session,
                    tool_name,
                    tool_args,
                    decision.category,
                )
                teacher_ms = round((time.perf_counter() - teacher_started) * 1000.0, 3)
                teacher_agreement = (
                    jev_decision.action == reflex_decision.action
                    if jev_decision.evaluated
                    else None
                )
                append_dataset_record(
                    self.settings.studio,
                    {
                        "kind": "decision",
                        "reflex_id": reflex_id,
                        "request_id": jsonrpc.get("id") if jsonrpc else None,
                        "workspace_key": managed_session.get("workspace_key"),
                        "features": reflex_features.as_dict(),
                        "reflex": reflex_decision.as_dict(),
                        "teacher": {
                            "provider": "typesafe_jev",
                            "enabled": jev_decision.enabled,
                            "evaluated": jev_decision.evaluated,
                            "action": jev_decision.action if jev_decision.evaluated else None,
                            "confidence": jev_decision.confidence if jev_decision.evaluated else None,
                            "model": jev_decision.model,
                            "error": jev_decision.error,
                            "agreement": teacher_agreement,
                        },
                        "timings": {
                            "policy_ms": policy_ms,
                            "reflex_ms": reflex_ms,
                            "teacher_ms": teacher_ms,
                        },
                    },
                )
                if reflex_decision.enabled:
                    await self.db.add_event(
                        "gateway.reflex.tool_decision",
                        reflex_decision.message,
                        server_id=server.id,
                        data={
                            "reflex_id": reflex_id,
                            "gateway_session_id": gateway_session_id,
                            "studio_session_id": session["studio_session_id"],
                            "managed_session_id": session.get("managed_session_id"),
                            "workspace_key": managed_session.get("workspace_key"),
                            "tool": tool_name,
                            "permission_class": decision.category,
                            "reflex": reflex_decision.as_dict(),
                            "teacher": {
                                "provider": "typesafe_jev",
                                "evaluated": jev_decision.evaluated,
                                "action": jev_decision.action if jev_decision.evaluated else None,
                                "confidence": jev_decision.confidence if jev_decision.evaluated else None,
                                "model": jev_decision.model,
                                "agreement": teacher_agreement,
                                "error": jev_decision.error,
                            },
                        },
                    )
                if reflex_decision.blocked:
                    return local_tool_response(
                        {
                            "error": reflex_decision.code,
                            "message": reflex_decision.message,
                            "tool": tool_name,
                            "permission_class": decision.category,
                            "workspace_key": managed_session.get("workspace_key"),
                            "reflex": {
                                "version": reflex_decision.version,
                                "action": reflex_decision.action,
                                "risk": reflex_decision.risk,
                                "confidence": reflex_decision.confidence,
                                "compute_lane": reflex_decision.compute_lane,
                                "signals": list(reflex_decision.signals),
                            },
                        },
                        is_error=True,
                    )

        if (
            request.method == "POST"
            and rpc_method == "tools/call"
            and self.integrations is not None
            and self.integrations.is_backend_tool(tool_name)
        ):
            if not session.get("managed_session_id"):
                return local_tool_response(
                    {
                        "error": "NO_MANAGED_SESSION_BOUND",
                        "message": "HIRDA backend tools require a managed workspace session.",
                    },
                    is_error=True,
                )
            try:
                if managed_session is None:
                    managed_session = await self.db.get_managed_session(
                        str(session["managed_session_id"])
                    )
                declared_category = self.integrations.backend_permission(tool_name)
                decision = decide_tool_call(
                    self.settings.studio,
                    managed_session,
                    tool_name,
                    tool_args,
                    declared_category=declared_category,
                )
                if not decision.allowed:
                    return local_tool_response(
                        {
                            "error": decision.code,
                            "message": decision.message,
                            "tool": tool_name,
                            "permission_class": decision.category,
                            "workspace_key": managed_session.get("workspace_key"),
                            "policy": decision.policy,
                        },
                        is_error=True,
                    )
                result = await self.integrations.call_backend_tool(
                    tool_name,
                    tool_args,
                    context={
                        "managed_session_id": managed_session.get("id"),
                        "workspace_key": managed_session.get("workspace_key"),
                        "project_path": managed_session.get("project_path"),
                        "policy": decision.policy,
                    },
                )
                await record_reflex_outcome(200, "integration-backend")
                return local_backend_tool_response(result)
            except (IntegrationError, KeyError) as exc:
                return local_tool_response(
                    {"error": type(exc).__name__, "message": str(exc), "tool": tool_name},
                    is_error=True,
                )
            except Exception as exc:
                return local_tool_response(
                    {
                        "error": "BACKEND_TOOL_FAILED",
                        "message": f"{type(exc).__name__}: {exc}",
                        "tool": tool_name,
                    },
                    is_error=True,
                )

        target_url = session.get("upstream_url") or server.url
        if (
            self.settings.studio.managed_session_cutover_enabled
            and self.settings.studio.managed_session_cutover_block_legacy_tools
            and request.method == "POST"
            and rpc_method == "tools/call"
            and not tool_name.startswith("mcpstudio_")
            and target_url.rstrip("/") == server.url.rstrip("/")
        ):
            return local_tool_response(
                {
                    "error": "LEGACY_UPSTREAM_BLOCKED",
                    "message": "Production session cutover is active. Serena tools must run through a pinned managed session.",
                },
                is_error=True,
            )
        headers = self._proxy_headers(request, session.get("upstream_session_id"))
        try:
            client, response = await self._send(
                server_url=target_url, method=request.method, headers=headers, body=body, stream=True
            )
        except Exception as exc:
            if request.method == "POST" and rpc_method == "tools/list" and self._management_tools():
                await self.db.add_event(
                    "gateway.control_tools.fallback",
                    f"Serving MCP Studio control tools because upstream tool discovery failed: {exc}",
                    server_id=server.id,
                    data={
                        "gateway_session_id": gateway_session_id,
                        "studio_session_id": session["studio_session_id"],
                        "managed_session_id": session.get("managed_session_id"),
                        "target_url": target_url,
                        "reason": type(exc).__name__,
                    },
                )
                return local_tools_list_response()
            raise

        # A deterministic MCP 404 means the selected upstream process/session
        # lost its private session id. Reinitialize against the same upstream
        # URL, including a managed Serena instance when one is pinned.
        if (
            response.status_code == 404
            and self.settings.studio.gateway_session_auto_reconnect
            and self.settings.studio.gateway_session_replay_on_404
            and request.method != "DELETE"
        ):
            await response.aread()
            await response.aclose()
            await client.aclose()
            try:
                session = await self._reinitialize_upstream(session, target_url)
            except Exception as exc:
                await self.db.touch_gateway_session(gateway_session_id, error=str(exc))
                raise HTTPException(status_code=503, detail=f"Upstream MCP session reconnect failed: {exc}")
            headers = self._proxy_headers(request, session.get("upstream_session_id"))
            client, response = await self._send(
                server_url=target_url, method=request.method, headers=headers, body=body, stream=True
            )

        response_headers = self._response_headers(response)
        response_headers["Mcp-Session-Id"] = gateway_session_id
        response_headers["X-MCP-Studio-Session-Id"] = session["studio_session_id"]
        response_headers["X-MCP-Studio-Generation"] = str(session["generation"])
        if session.get("last_tunnel_id"):
            response_headers["X-MCP-Studio-Tunnel-Id"] = str(session["last_tunnel_id"])
        if session.get("managed_session_id"):
            response_headers["X-MCP-Studio-Managed-Session-Id"] = str(session["managed_session_id"])
        if isinstance(context_report, dict) and context_report.get("telemetry_available"):
            response_headers["X-HIRDA-Context-Usage-Percent"] = str(context_report.get("context_usage_percent"))
            response_headers["X-HIRDA-Context-Urgency"] = str((context_report.get("rollover") or {}).get("urgency") or "unknown")
        response_headers["Cache-Control"] = "no-store"
        response_headers["X-Content-Type-Options"] = "nosniff"

        if request.method == "DELETE":
            content = await response.aread()
            status_code = response.status_code
            await response.aclose()
            await client.aclose()
            await self.db.close_gateway_session(gateway_session_id)
            try:
                await self.db.disconnect_session(session["studio_session_id"])
            except KeyError:
                pass
            return Response(content=content, status_code=status_code, headers=response_headers)

        ctype = response.headers.get("content-type", "")
        # Tool discovery is finite. Buffer it so Studio can advertise the
        # management tools alongside Serena's own tool catalog.
        if rpc_method == "tools/list":
            content = await response.aread()
            status_code = response.status_code
            await response.aclose()
            await client.aclose()
            if status_code < 400:
                content = self._augment_tools_content(content, ctype)
                media = "text/event-stream" if "text/event-stream" in ctype else "application/json"
                return Response(content=content, status_code=status_code, headers=response_headers, media_type=media)

            # Tool discovery must not strand an unbound ChatGPT transport.
            # Even when Serena returns an error, Studio's local control plane is
            # still usable and must remain discoverable so the client can bind
            # a workspace/session and recover.
            if self._management_tools():
                await self.db.add_event(
                    "gateway.control_tools.fallback",
                    f"Serving MCP Studio control tools because upstream tools/list returned HTTP {status_code}",
                    server_id=server.id,
                    data={
                        "gateway_session_id": gateway_session_id,
                        "studio_session_id": session["studio_session_id"],
                        "managed_session_id": session.get("managed_session_id"),
                        "target_url": target_url,
                        "upstream_status": status_code,
                    },
                )
                return local_tools_list_response()

            media = "text/event-stream" if "text/event-stream" in ctype else "application/json"
            return Response(content=content, status_code=status_code, headers=response_headers, media_type=media)

        if "text/event-stream" not in ctype:
            content = await response.aread()
            status_code = response.status_code
            await response.aclose()
            await client.aclose()
            if rpc_method == "tools/call" and context_notice:
                content = self._augment_tool_result_context_notice(content, ctype, context_notice)
            await record_reflex_outcome(status_code, "buffered")
            return Response(content=content, status_code=status_code, headers=response_headers)

        # A rollover transition must be visible to the model in the same tool
        # turn. Buffer only that rare SSE response, append the notice once, then
        # return it intact; normal SSE traffic stays streaming.
        if rpc_method == "tools/call" and context_notice:
            content = await response.aread()
            status_code = response.status_code
            await response.aclose()
            await client.aclose()
            content = self._augment_tool_result_context_notice(content, ctype, context_notice)
            await record_reflex_outcome(status_code, "buffered-context-notice")
            return Response(
                content=content,
                status_code=status_code,
                headers=response_headers,
                media_type="text/event-stream",
            )

        await record_reflex_outcome(response.status_code, "streaming")

        async def body_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            body_iter(),
            status_code=response.status_code,
            headers=response_headers,
            media_type="text/event-stream",
        )

    async def drop_upstream_for_test(self, gateway_session_id: str) -> dict[str, Any]:
        if not self.settings.studio.gateway_session_test_mode:
            raise ValueError("gateway_session_test_mode=false")
        session = await self.db.get_gateway_session(gateway_session_id)
        server = next((s for s in self.settings.servers if s.id == session["server_id"] and s.enabled), None)
        if server is None:
            raise ValueError("gateway session server is unavailable")
        upstream_id = session.get("upstream_session_id")
        if not upstream_id:
            raise ValueError("gateway session has no upstream MCP session")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": upstream_id,
        }
        target_url = session.get("upstream_url") or server.url
        async with httpx.AsyncClient(timeout=self.settings.studio.request_timeout_seconds) as client:
            response = await client.delete(target_url, headers=headers)
        if response.status_code not in (200, 202, 204, 404, 405):
            raise ValueError(f"upstream test drop failed HTTP {response.status_code}")
        await self.db.add_event(
            "gateway.session.test_upstream_dropped",
            f"Certification dropped upstream session for {gateway_session_id}",
            severity="warning",
            server_id=session["server_id"],
            data={"gateway_session_id": gateway_session_id},
        )
        return {
            "ok": True,
            "gateway_session_id": gateway_session_id,
            "studio_session_id": session["studio_session_id"],
            "generation": session["generation"],
        }


def make_gateway_router(settings: Settings, db: Database, manager: GatewaySessionManager) -> APIRouter:
    router = APIRouter()

    def _server_for(server_id: str):
        if not settings.studio.gateway_enabled:
            raise HTTPException(status_code=503, detail="Gateway disabled in config")
        allowed = settings.studio.gateway_allowed_servers
        if allowed and server_id not in allowed:
            raise HTTPException(status_code=404, detail="Unknown MCP server")
        server = next((s for s in settings.servers if s.id == server_id and s.enabled), None)
        if server is None:
            raise HTTPException(status_code=404, detail="Unknown MCP server")
        return server

    def _openai_local_identity(request: Request, server_id: str) -> tuple[str, str, str, str, dict[str, Any]]:
        if not settings.studio.openai_local_ingress_enabled:
            raise HTTPException(status_code=404, detail="Not found")
        host = request.client.host if request.client else ""
        allowed_hosts = {str(x).strip().lower() for x in settings.studio.openai_local_ingress_allowed_hosts}
        if host.lower() not in allowed_hosts:
            # Do not expose a privileged local ingress surface to LAN/public callers.
            raise HTTPException(status_code=404, detail="Not found")
        ingress_id = settings.studio.openai_local_ingress_id.strip() or "openai-serena"
        client_id = "openai-local-" + hashlib.sha256(ingress_id.encode("utf-8")).hexdigest()[:24]
        ingress = {
            "tunnel_id": ingress_id,
            "host": host,
            "path": manager._norm_path(request.url.path),
            "provider": "openai",
            "method": "loopback_openai_secure_tunnel",
            "confidence": "high",
            "cloudflare_evidence": False,
            "candidate_count": 1,
        }
        # transport scope deliberately disables connector-wide logical-session
        # reclaim until M6.2.3 Session Manager can bind an explicit workspace.
        return client_id, "openai-secure-tunnel", "transport", "openai-local-loopback", ingress

    async def _proxy_impl(
        server_id: str,
        request: Request,
        *,
        client_id: str,
        client_type: str,
        identity_scope: str,
        identity_source: str,
        ingress_override: dict[str, Any] | None = None,
    ):
        server = _server_for(server_id)
        body = await request.body()
        jsonrpc = manager.parse_jsonrpc(body)
        method = jsonrpc.get("method") if jsonrpc else None
        incoming_session = request.headers.get("mcp-session-id")
        started = time.perf_counter()
        client_class = "openai-secure-tunnel" if client_type == "openai-secure-tunnel" else ("openai-chatgpt" if client_type == "openai-chatgpt" else "mcp-http")

        async def observed_response(awaitable, *, gateway_id: str | None = None):
            try:
                response_obj = await awaitable
            except HTTPException as exc:
                await db.record_mcp_request(
                    server_id=server_id, http_method=request.method, rpc_method=method,
                    status_code=exc.status_code, latency_ms=(time.perf_counter()-started)*1000.0,
                    client_class=client_class, gateway_session_id=gateway_id,
                )
                raise
            except Exception:
                await db.record_mcp_request(
                    server_id=server_id, http_method=request.method, rpc_method=method,
                    status_code=500, latency_ms=(time.perf_counter()-started)*1000.0,
                    client_class=client_class, gateway_session_id=gateway_id,
                )
                raise
            await db.record_mcp_request(
                server_id=server_id, http_method=request.method, rpc_method=method,
                status_code=response_obj.status_code, latency_ms=(time.perf_counter()-started)*1000.0,
                client_class=client_class, gateway_session_id=gateway_id or response_obj.headers.get("mcp-session-id"),
            )
            return response_obj

        if settings.studio.gateway_session_enabled:
            if request.method == "POST" and method == "initialize":
                return await observed_response(manager.initialize(
                    request=request,
                    server=server,
                    body=body,
                    jsonrpc=jsonrpc or {},
                    client_id=client_id,
                    client_type=client_type,
                    identity_scope=identity_scope,
                    identity_source=identity_source,
                    ingress_override=ingress_override,
                ))
            if incoming_session and incoming_session.startswith("gws-"):
                return await observed_response(manager.proxy_existing(
                    request=request,
                    server=server,
                    body=body,
                    gateway_session_id=incoming_session,
                    ingress_override=ingress_override,
                ), gateway_id=incoming_session)
            if incoming_session:
                await db.record_mcp_request(
                    server_id=server_id, http_method=request.method, rpc_method=method,
                    status_code=409, latency_ms=(time.perf_counter()-started)*1000.0,
                    client_class=client_class, gateway_session_id=incoming_session,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Legacy upstream MCP session id cannot be resumed through Studio; initialize a new gateway session",
                )

        headers = manager._proxy_headers(request, incoming_session)
        try:
            client, response = await manager._send(
                server_url=server.url, method=request.method, headers=headers, body=body, stream=True
            )
        except Exception:
            await db.record_mcp_request(
                server_id=server_id, http_method=request.method, rpc_method=method,
                status_code=500, latency_ms=(time.perf_counter()-started)*1000.0,
                client_class=client_class, gateway_session_id=incoming_session,
            )
            raise
        await db.record_mcp_request(
            server_id=server_id, http_method=request.method, rpc_method=method,
            status_code=response.status_code, latency_ms=(time.perf_counter()-started)*1000.0,
            client_class=client_class, gateway_session_id=incoming_session,
        )
        response_headers = manager._response_headers(response)
        ctype = response.headers.get("content-type", "")
        if "text/event-stream" not in ctype:
            content = await response.aread()
            await response.aclose()
            await client.aclose()
            response_headers["Cache-Control"] = "no-store"
            response_headers["X-Content-Type-Options"] = "nosniff"
            return Response(content=content, status_code=response.status_code, headers=response_headers)

        async def body_iter() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        response_headers["Cache-Control"] = "no-store"
        response_headers["X-Content-Type-Options"] = "nosniff"
        return StreamingResponse(
            body_iter(), status_code=response.status_code, headers=response_headers, media_type="text/event-stream"
        )

    @router.api_route("/mcp/{server_id}", methods=["GET", "POST", "DELETE"])
    async def proxy(server_id: str, request: Request):
        _token, client_id, client_type, identity_scope, identity_source = manager.authenticate(request)
        return await _proxy_impl(
            server_id, request,
            client_id=client_id, client_type=client_type,
            identity_scope=identity_scope, identity_source=identity_source,
        )

    @router.api_route("/ingress/openai/{server_id}", methods=["GET", "POST", "DELETE"])
    async def openai_local_proxy(server_id: str, request: Request):
        # Deliberately local-only. The OpenAI tunnel client is an outbound bridge
        # on the same host; public/LAN callers receive 404 even if this path is guessed.
        _server_for(server_id)
        client_id, client_type, identity_scope, identity_source, ingress = _openai_local_identity(request, server_id)
        return await _proxy_impl(
            server_id, request,
            client_id=client_id, client_type=client_type,
            identity_scope=identity_scope, identity_source=identity_source,
            ingress_override=ingress,
        )

    return router
