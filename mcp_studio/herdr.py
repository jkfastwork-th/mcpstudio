from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .health import HealthManager
from .mcp_client import MCPClient
from .settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_text_content(result: Any, *, _depth: int = 0) -> Any:
    if _depth > 12:
        return result
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return result
        try:
            decoded = json.loads(text)
        except Exception:
            return result
        return _decode_text_content(decoded, _depth=_depth + 1)
    if isinstance(result, list):
        return [_decode_text_content(item, _depth=_depth + 1) for item in result]
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if structured is not None:
            return _decode_text_content(structured, _depth=_depth + 1)
        content = result.get("content")
        if isinstance(content, list):
            decoded: list[Any] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    decoded.append(_decode_text_content(item["text"], _depth=_depth + 1))
                else:
                    decoded.append(_decode_text_content(item, _depth=_depth + 1))
            if len(decoded) == 1:
                return decoded[0]
            return decoded
        if "result" in result:
            decoded = _decode_text_content(result["result"], _depth=_depth + 1)
            if isinstance(decoded, (dict, list)):
                return decoded
        return {key: _decode_text_content(value, _depth=_depth + 1) for key, value in result.items()}
    return result


def _count_items(value: Any, keys: tuple[str, ...]) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return len(value[key])
        for nested in value.values():
            count = _count_items(nested, keys)
            if count is not None:
                return count
    return None


class HerdrManager:
    def __init__(self, settings: Settings, db: Database, health: HealthManager):
        self.settings = settings
        self.db = db
        self.health = health
        self.snapshot: dict[str, Any] = {
            "status": "unknown",
            "server_id": settings.studio.worker_server_id,
            "last_refreshed_at": None,
            "agent_count": None,
            "pane_count": None,
            "agents": None,
            "panes": None,
            "execution_tools": {},
            "error": None,
        }
        self.tool_definitions: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._refresh_lock = asyncio.Lock()

    def _server(self):
        return next(s for s in self.settings.servers if s.id == self.settings.studio.worker_server_id)

    def client(self) -> MCPClient:
        server = self._server()
        return MCPClient(
            server.url,
            timeout=self.settings.studio.request_timeout_seconds,
            protocol_version=server.protocol_version,
        )

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="mcp-studio-herdr-discovery")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = max(10, self.settings.studio.herdr_poll_interval_seconds)
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await self.refresh()

    @staticmethod
    def pane_list(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        value = snapshot.get("panes")
        if isinstance(value, dict) and isinstance(value.get("panes"), list):
            return [x for x in value["panes"] if isinstance(x, dict)]
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        return []

    @staticmethod
    def agent_list(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        value = snapshot.get("agents")
        if isinstance(value, dict) and isinstance(value.get("agents"), list):
            return [x for x in value["agents"] if isinstance(x, dict)]
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        return []

    def find_pane(self, *, pane_id: str | None = None, workspace: str | None = None, agent: str | None = None) -> dict[str, Any] | None:
        panes = self.pane_list(self.snapshot)
        if pane_id:
            match = next((p for p in panes if p.get("pane_id") == pane_id), None)
            if match:
                return match
        candidates = panes
        if workspace:
            candidates = [p for p in candidates if p.get("cwd") == workspace or p.get("foreground_cwd") == workspace]
        if agent:
            with_agent = [p for p in candidates if p.get("agent") == agent]
            if with_agent:
                candidates = with_agent
        if not candidates:
            return None
        rank = {"idle": 0, "done": 1, "unknown": 2}
        return sorted(
            candidates,
            key=lambda p: (
                rank.get(str(p.get("agent_status")), 3),
                0 if p.get("agent") else 1,
                1 if p.get("focused") else 0,
                str(p.get("pane_id") or ""),
            ),
        )[0]

    def tool(self, name: str) -> dict[str, Any] | None:
        return self.tool_definitions.get(name)

    async def refresh(self) -> dict[str, Any]:
        async with self._refresh_lock:
            server = self._server()
            health_snapshot = self.health.snapshots.get(server.id)
            required = {"herdr_list_agents", "herdr_list_panes"}
            if health_snapshot is not None and health_snapshot.tool_names:
                missing = required - set(health_snapshot.tool_names)
                if missing:
                    self.snapshot = {
                        **self.snapshot,
                        "status": "degraded",
                        "last_refreshed_at": _now(),
                        "error": "Missing Herdr tools: " + ", ".join(sorted(missing)),
                    }
                    return self.snapshot

            try:
                client = self.client()
                tools = await client.list_tools(client_name="mcp-studio-herdr-schema")
                self.tool_definitions = {t.get("name"): t for t in tools if t.get("name")}
                results = await client.call_tools(
                    [("herdr_list_agents", {}), ("herdr_list_panes", {})],
                    client_name="mcp-studio-herdr-observer",
                )
                agents = _decode_text_content(results[0])
                panes = _decode_text_content(results[1])
                agent_count = _count_items(agents, ("agents", "items", "results"))
                pane_count = _count_items(panes, ("panes", "items", "results"))
                execution_tools = {
                    name: bool(self.tool_definitions.get(name))
                    for name in ("herdr_prompt_agent", "herdr_read_agent", "herdr_wait_agent")
                }
                status = "healthy" if execution_tools["herdr_prompt_agent"] else "degraded"
                error = None if status == "healthy" else "herdr_prompt_agent is not available"
                self.snapshot = {
                    "status": status,
                    "server_id": server.id,
                    "last_refreshed_at": _now(),
                    "agent_count": agent_count,
                    "pane_count": pane_count,
                    "agents": agents,
                    "panes": panes,
                    "execution_tools": execution_tools,
                    "error": error,
                }
            except Exception as exc:
                self.snapshot = {
                    **self.snapshot,
                    "status": "down",
                    "last_refreshed_at": _now(),
                    "error": str(exc),
                }
            return self.snapshot
