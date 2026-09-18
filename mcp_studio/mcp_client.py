from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class MCPProbeResult:
    tools: list[dict[str, Any]]
    schema_hash: str
    session_id: str | None
    server_info: dict[str, Any]


def canonical_tools_hash(tools: list[dict[str, Any]]) -> str:
    normalized = []
    for tool in sorted(tools, key=lambda t: t.get("name", "")):
        normalized.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "inputSchema": tool.get("inputSchema", {}),
                "outputSchema": tool.get("outputSchema"),
            }
        )
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    ctype = response.headers.get("content-type", "")
    if "application/json" in ctype:
        return response.json()
    if "text/event-stream" in ctype:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw and raw != "[DONE]":
                    return json.loads(raw)
        raise RuntimeError("MCP SSE response contained no JSON data event")
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"Unsupported MCP response content-type: {ctype or 'unknown'}") from exc


class MCPClient:
    def __init__(self, url: str, timeout: float = 8.0, protocol_version: str = "2025-06-18"):
        self.url = url
        self.timeout = timeout
        self.protocol_version = protocol_version
        self.session_id: str | None = None
        self._next_id = 1

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "mcp-studio/0.4",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = await client.post(self.url, headers=self._headers(), json=payload)
        response.raise_for_status()
        self.session_id = response.headers.get("mcp-session-id", self.session_id)
        data = _decode_response(response)
        if "error" in data:
            raise RuntimeError(f"MCP {method} error: {data['error']}")
        return data.get("result", {})

    async def _notify(self, client: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        response = await client.post(self.url, headers=self._headers(), json=payload)
        if response.status_code not in (200, 202, 204):
            response.raise_for_status()

    async def _initialize(self, client: httpx.AsyncClient, client_name: str) -> dict[str, Any]:
        init = await self._post(
            client,
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "0.4.0"},
            },
        )
        await self._notify(client, "notifications/initialized")
        return init

    async def _close(self, client: httpx.AsyncClient) -> None:
        if not self.session_id:
            return
        try:
            response = await client.delete(self.url, headers=self._headers())
            if response.status_code not in (200, 202, 204, 404, 405):
                response.raise_for_status()
        except Exception:
            pass
        finally:
            # A Streamable HTTP MCP session id is scoped to the session that
            # was just closed. Reusing it on the next initialize can make a
            # conforming server return 404 for an unknown/deleted session.
            self.session_id = None

    async def _list_tools_open(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            params = {"cursor": cursor} if cursor else None
            page = await self._post(client, "tools/list", params)
            tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return tools

    async def list_tools(self, *, client_name: str = "mcp-studio-schema-reader") -> list[dict[str, Any]]:
        limits = httpx.Limits(max_keepalive_connections=2, max_connections=4)
        async with httpx.AsyncClient(timeout=self.timeout, limits=limits, follow_redirects=True) as client:
            await self._initialize(client, client_name)
            try:
                return await self._list_tools_open(client)
            finally:
                await self._close(client)

    async def probe(self) -> MCPProbeResult:
        limits = httpx.Limits(max_keepalive_connections=2, max_connections=4)
        async with httpx.AsyncClient(timeout=self.timeout, limits=limits, follow_redirects=True) as client:
            init = await self._initialize(client, "mcp-studio-observer")
            tools = await self._list_tools_open(client)
            schema_hash = canonical_tools_hash(tools)
            session_id = self.session_id
            await self._close(client)
            return MCPProbeResult(
                tools=tools,
                schema_hash=schema_hash,
                session_id=session_id,
                server_info=init.get("serverInfo", {}),
            )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        client_name: str = "mcp-studio-worker",
    ) -> dict[str, Any]:
        results = await self.call_tools([(name, arguments or {})], client_name=client_name)
        return results[0]

    async def call_tools(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        client_name: str = "mcp-studio-worker",
    ) -> list[dict[str, Any]]:
        limits = httpx.Limits(max_keepalive_connections=2, max_connections=4)
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=self.timeout, limits=limits, follow_redirects=True) as client:
            await self._initialize(client, client_name)
            try:
                for name, arguments in calls:
                    result = await self._post(
                        client,
                        "tools/call",
                        {"name": name, "arguments": arguments or {}},
                    )
                    if result.get("isError"):
                        raise RuntimeError(f"MCP tool {name} returned isError=true: {result}")
                    results.append(result)
            finally:
                await self._close(client)
        return results
