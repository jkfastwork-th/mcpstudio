from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
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


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _read_agent_session_result(
    pane: dict[str, Any] | None,
    request_id: str,
    *,
    home: Path | None = None,
    max_lines: int = 2000,
) -> str | None:
    """Read a framed result from an agent's durable session transcript.

    This is a fallback for terminal UIs whose alternate-screen rendering does
    not preserve the assistant response in host scrollback. Transcript reads
    are bounded and require an exact HIRDA request marker.
    """
    if not pane or not request_id:
        return None

    start_marker = f"<<<HIRDA_COGNITIVE_RESULT:{request_id}>>>"
    end_marker = "<<<END_HIRDA_COGNITIVE_RESULT>>>"
    root_home = home or Path.home()
    agent = str(pane.get("agent") or "").strip().casefold()

    session = pane.get("agent_session")
    source = ""
    session_id = ""
    if isinstance(session, dict):
        source = str(session.get("source") or "").strip().casefold()
        session_id = str(session.get("value") or "").strip()

    def recent_lines(path: Path) -> deque[str] | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return deque(handle, maxlen=max(1, int(max_lines)))
        except OSError:
            return None

    if source == "herdr:claude" and _SESSION_ID_RE.fullmatch(session_id):
        root = root_home / ".claude" / "projects"
        if not root.is_dir():
            return None
        cwd = str(
            pane.get("cwd") or pane.get("foreground_cwd") or ""
        ).strip()
        candidates: list[Path] = []
        if cwd:
            candidates.append(
                root / cwd.replace("/", "-") / f"{session_id}.jsonl"
            )
        if not any(path.is_file() for path in candidates):
            candidates.extend(root.glob(f"*/{session_id}.jsonl"))

        transcript = next(
            (path for path in candidates if path.is_file()),
            None,
        )
        if transcript is None:
            return None
        recent = recent_lines(transcript)
        if recent is None:
            return None

        for raw_line in reversed(recent):
            try:
                row = json.loads(raw_line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "").casefold() != "assistant":
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "").casefold() != "assistant":
                continue
            text = _message_text(message)
            if start_marker in text and end_marker in text:
                return text
        return None

    if source == "herdr:codex" or agent == "codex":
        root = root_home / ".codex" / "sessions"
        if not root.is_dir():
            return None

        candidates: list[Path] = []
        if session_id and _SESSION_ID_RE.fullmatch(session_id):
            candidates.extend(root.rglob(f"*{session_id}.jsonl"))

        if not candidates:
            ranked: list[tuple[float, Path]] = []
            for path in root.rglob("*.jsonl"):
                if not path.is_file():
                    continue
                try:
                    ranked.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            ranked.sort(key=lambda item: item[0], reverse=True)
            candidates = [path for _mtime, path in ranked[:12]]

        for transcript in candidates[:12]:
            recent = recent_lines(transcript)
            if recent is None:
                continue
            for raw_line in reversed(recent):
                try:
                    row = json.loads(raw_line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue

                text = ""
                row_type = str(row.get("type") or "").casefold()
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue

                if row_type == "response_item":
                    if (
                        str(payload.get("type") or "").casefold() == "message"
                        and str(payload.get("role") or "").casefold()
                        == "assistant"
                    ):
                        content = payload.get("content")
                        if isinstance(content, list):
                            text = "\n".join(
                                str(item.get("text") or "")
                                for item in content
                                if isinstance(item, dict)
                                and str(item.get("type") or "").casefold()
                                in {"output_text", "text"}
                            )
                elif row_type == "event_msg":
                    item = payload.get("item")
                    if (
                        isinstance(item, dict)
                        and str(item.get("type") or "").casefold()
                        == "agentmessage"
                    ):
                        content = item.get("content")
                        if isinstance(content, list):
                            text = "\n".join(
                                str(part.get("text") or "")
                                for part in content
                                if isinstance(part, dict)
                                and str(part.get("type") or "").casefold()
                                in {"text", "output_text"}
                            )

                if start_marker in text and end_marker in text:
                    return text
        return None

    return None



def _read_agent_session_error(
    pane: dict[str, Any] | None,
    request_id: str,
    *,
    home: Path | None = None,
    max_lines: int = 2000,
) -> str | None:
    """Read an unframed provider error correlated to one HIRDA request.

    Terminal alternate-screen UIs may hide provider errors from scrollback.
    This reader uses the durable agent transcript, requires a matching request
    id in a user turn, and returns only the first subsequent assistant text.
    The cognitive router still classifies the returned text fail-closed.
    """
    if not pane or not request_id:
        return None

    root_home = home or Path.home()
    agent = str(pane.get("agent") or "").strip().casefold()
    session = pane.get("agent_session")
    source = ""
    session_id = ""
    if isinstance(session, dict):
        source = str(session.get("source") or "").strip().casefold()
        session_id = str(session.get("value") or "").strip()

    def recent_rows(path: Path) -> list[dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                raw_lines = deque(handle, maxlen=max(1, int(max_lines)))
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for raw_line in raw_lines:
            try:
                row = json.loads(raw_line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def correlated_assistant(
        turns: list[tuple[str, str]],
    ) -> str | None:
        marker = f"request_id: {request_id}"
        start = -1
        for index, (role, text) in enumerate(turns):
            if role == "user" and marker in text:
                start = index
        if start < 0:
            return None
        for role, text in turns[start + 1 :]:
            if role != "assistant":
                continue
            text = str(text or "").strip()
            if text:
                return (
                    "HIRDA COGNITIVE REQUEST\n"
                    f"request_id: {request_id}\n"
                    f"{text}"
                )
        return None

    if source == "herdr:claude" and _SESSION_ID_RE.fullmatch(session_id):
        root = root_home / ".claude" / "projects"
        if not root.is_dir():
            return None
        cwd = str(
            pane.get("cwd") or pane.get("foreground_cwd") or ""
        ).strip()
        candidates: list[Path] = []
        if cwd:
            candidates.append(
                root / cwd.replace("/", "-") / f"{session_id}.jsonl"
            )
        if not any(path.is_file() for path in candidates):
            candidates.extend(root.glob(f"*/{session_id}.jsonl"))
        transcript = next(
            (path for path in candidates if path.is_file()),
            None,
        )
        if transcript is None:
            return None
        turns: list[tuple[str, str]] = []
        for row in recent_rows(transcript):
            row_type = str(row.get("type") or "").casefold()
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or row_type).strip().casefold()
            if role not in {"user", "assistant"}:
                continue
            turns.append((role, _message_text(message)))
        return correlated_assistant(turns)

    if source == "herdr:codex" or agent == "codex":
        root = root_home / ".codex" / "sessions"
        if not root.is_dir():
            return None
        candidates: list[Path] = []
        if session_id and _SESSION_ID_RE.fullmatch(session_id):
            candidates.extend(root.rglob(f"*{session_id}.jsonl"))
        if not candidates:
            ranked: list[tuple[float, Path]] = []
            for path in root.rglob("*.jsonl"):
                if not path.is_file():
                    continue
                try:
                    ranked.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            ranked.sort(key=lambda item: item[0], reverse=True)
            candidates = [path for _mtime, path in ranked[:12]]

        for transcript in candidates[:12]:
            turns: list[tuple[str, str]] = []
            for row in recent_rows(transcript):
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue
                row_type = str(row.get("type") or "").casefold()
                if row_type == "response_item":
                    if str(payload.get("type") or "").casefold() != "message":
                        continue
                    role = str(payload.get("role") or "").casefold()
                    content = payload.get("content")
                    if role not in {"user", "assistant"} or not isinstance(
                        content,
                        list,
                    ):
                        continue
                    text = "\n".join(
                        str(item.get("text") or "")
                        for item in content
                        if isinstance(item, dict)
                        and str(item.get("type") or "").casefold()
                        in {"input_text", "output_text", "text"}
                    )
                    turns.append((role, text))
                elif row_type == "event_msg":
                    payload_type = str(payload.get("type") or "").casefold()
                    if payload_type == "task_complete":
                        error = payload.get("error")
                        if isinstance(error, dict):
                            message = str(error.get("message") or "").strip()
                            error_info = str(
                                error.get("codex_error_info") or ""
                            ).strip()
                            if message or error_info:
                                turns.append(
                                    (
                                        "assistant",
                                        "\n".join(
                                            part
                                            for part in (message, error_info)
                                            if part
                                        ),
                                    )
                                )
                        continue

                    item = payload.get("item")
                    if (
                        isinstance(item, dict)
                        and str(item.get("type") or "").casefold()
                        == "agentmessage"
                    ):
                        content = item.get("content")
                        if not isinstance(content, list):
                            continue
                        text = "\n".join(
                            str(part.get("text") or "")
                            for part in content
                            if isinstance(part, dict)
                            and str(part.get("type") or "").casefold()
                            in {"text", "output_text"}
                        )
                        turns.append(("assistant", text))
            found = correlated_assistant(turns)
            if found:
                return found
        return None

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

    def client(self, *, timeout: float | None = None) -> MCPClient:
        server = self._server()
        resolved_timeout = (
            float(timeout)
            if timeout is not None
            else float(self.settings.studio.request_timeout_seconds)
        )
        return MCPClient(
            server.url,
            timeout=resolved_timeout,
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
            panes = [x for x in value["panes"] if isinstance(x, dict)]
        elif isinstance(value, list):
            panes = [x for x in value if isinstance(x, dict)]
        else:
            panes = []

        agents_by_pane = {
            str(agent.get("pane_id")): agent
            for agent in HerdrManager.agent_list(snapshot)
            if agent.get("pane_id")
        }
        enriched: list[dict[str, Any]] = []
        for pane in panes:
            row = dict(pane)
            agent_row = agents_by_pane.get(str(row.get("pane_id") or ""))
            if agent_row:
                agent_name = str(agent_row.get("name") or "").strip()
                if agent_name:
                    row.setdefault("agent_name", agent_name)
                if not row.get("agent_session") and agent_row.get("agent_session"):
                    row["agent_session"] = agent_row.get("agent_session")
            enriched.append(row)
        return enriched

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

    def read_session_result(
        self,
        pane: dict[str, Any] | None,
        request_id: str,
    ) -> str | None:
        return _read_agent_session_result(pane, request_id)

    def read_session_error(
        self,
        pane: dict[str, Any] | None,
        request_id: str,
    ) -> str | None:
        return _read_agent_session_error(pane, request_id)

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
