from __future__ import annotations

import hashlib
import json
import os
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


CONTROL_TOOL_NAMES = (
    "mcpstudio_use_workspace",
    "mcpstudio_create_session",
    "mcpstudio_use_session",
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
    ):
        self.settings = settings
        self.db = db
        self.oauth = oauth
        self.managed_sessions = managed_sessions

    def authenticate(self, request: Request) -> tuple[str, str, str, str, str]:
        """Authenticate and derive a stable client identity.

        Returns (token, client_id, client_type, identity_scope, identity_source).
        identity_scope is intentionally explicit: bearer-derived identity is a
        connector/install identity and MUST NOT be interpreted as a ChatGPT
        conversation id.
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
                # A missing/invalid OAuth signing configuration is a server
                # readiness issue, but it must not break the certified static
                # gateway credential path.
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
        if explicit:
            client_id = explicit[:240]
            identity_scope = "explicit"
            identity_source = "x-mcp-studio-client-id"
        elif oauth_claims is not None:
            # Stable across access-token refreshes without conflating this with
            # a ChatGPT conversation id. The OAuth client registration is the
            # connector identity.
            client_id = "oauth-" + hashlib.sha256(oauth_claims.client_id.encode("utf-8")).hexdigest()[:24]
            identity_scope = "connector"
            identity_source = "oauth-client-id"
        elif token:
            # Static gateway bearer compatibility path used by certification
            # and local administration. This remains connector-scoped.
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
            "conversation_identity_available": identity_scope == "explicit",
            "observed_header_names": observed_header_names,
        }

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
        return [
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
                "description": "Attach this MCP transport to an existing managed session. Future Serena calls use that session's pinned project.",
                "inputSchema": {
                    "type": "object", "properties": {"session": {"type": "string"}},
                    "required": ["session"], "additionalProperties": False,
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
                "name": "mcpstudio_close_session",
                "description": "Stop a managed Serena session. If this transport is attached to it, Studio detaches first.",
                "inputSchema": {
                    "type": "object", "properties": {"session": {"type": "string"}},
                    "required": ["session"], "additionalProperties": False,
                },
            },
        ]

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

    def _augment_tools_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result") if isinstance(payload, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return payload

        studio_tools = self._ordered_management_tools()
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
        if not self._management_tools():
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
            "result": {"tools": self._ordered_management_tools()},
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
            target = await self._resolve_managed_session(str(args.get("session") or ""))
            return await self.bind_managed_session(gateway_session_id, target["id"], actor="chatgpt/mcp")
        if name == "mcpstudio_current_session":
            gateway = await self.db.get_gateway_session(gateway_session_id)
            managed_id = gateway.get("managed_session_id")
            return {
                "gateway_session_id": gateway_session_id,
                "managed_session": await self.managed_sessions.get_session(managed_id) if managed_id else None,
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
            identity_scope == "explicit"
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

        jsonrpc = self.parse_jsonrpc(body)
        rpc_method = jsonrpc.get("method") if jsonrpc else None
        params = jsonrpc.get("params") if jsonrpc and isinstance(jsonrpc.get("params"), dict) else {}
        tool_name = str(params.get("name") or "") if rpc_method == "tools/call" else ""
        tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}

        def _decorate_local_response(response: Response) -> Response:
            response.headers["Mcp-Session-Id"] = gateway_session_id
            response.headers["X-MCP-Studio-Session-Id"] = session["studio_session_id"]
            response.headers["X-MCP-Studio-Generation"] = str(session.get("generation") or 1)
            if session.get("last_tunnel_id"):
                response.headers["X-MCP-Studio-Tunnel-Id"] = str(session["last_tunnel_id"])
            if session.get("managed_session_id"):
                response.headers["X-MCP-Studio-Managed-Session-Id"] = str(session["managed_session_id"])
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        def local_tool_response(payload: Any, *, is_error: bool = False) -> Response:
            return _decorate_local_response(
                self._jsonrpc_tool_response(
                    jsonrpc.get("id") if jsonrpc else None, payload, is_error=is_error
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

        if request.method == "POST" and rpc_method == "tools/call" and tool_name.startswith("mcpstudio_"):
            try:
                result = await self._handle_management_tool(gateway_session_id, tool_name, tool_args)
                session = await self.db.get_gateway_session(gateway_session_id)
                return local_tool_response(result)
            except (ManagedSessionError, ManagedSessionConflict, WorkspaceNotAllowed, KeyError) as exc:
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
            if session.get("managed_session_id") and tool_name in set(self.settings.studio.managed_session_blocked_tools):
                if tool_name == "activate_project":
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
                else:
                    return local_tool_response(
                        {
                            "error": "SESSION_PROJECT_PINNED",
                            "message": f"Tool {tool_name} is blocked because this transport is pinned to managed session {session['managed_session_id']}.",
                        },
                        is_error=True,
                    )
            if session.get("managed_session_id") and self.settings.studio.managed_session_tool_permissions_enabled:
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
                decision = decide_tool_call(self.settings.studio, managed_session, tool_name, tool_args)
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
            return Response(content=content, status_code=status_code, headers=response_headers)

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
