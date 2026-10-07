from __future__ import annotations

import asyncio
import json
import re
import shutil
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


class HerdrUnavailable(RuntimeError):
    """The optional execution substrate cannot currently accept requests."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"herdr_unavailable: {reason}")


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

    @staticmethod
    def _persistent_tab_labels() -> frozenset[str]:
        return frozenset({"fern", "nova"})

    @staticmethod
    def _task_tab_label(task: dict[str, Any]) -> str:
        task_id = str(task.get("task_id") or "TASK").strip()
        goal = " ".join(str(task.get("goal") or "").split())
        suffix = f" · {goal[:56]}" if goal else ""
        return f"[PStack] {task_id}{suffix}"

    @staticmethod
    def _tabs_from_cli(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("tabs"), list):
            return [dict(row) for row in result["tabs"] if isinstance(row, dict)]
        return []

    @staticmethod
    def _tab_from_create_cli(payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(payload, dict):
            return {}, {}
        result = payload.get("result")
        if not isinstance(result, dict):
            return {}, {}
        tab = result.get("tab")
        root_pane = result.get("root_pane")
        return (
            dict(tab) if isinstance(tab, dict) else {},
            dict(root_pane) if isinstance(root_pane, dict) else {},
        )

    @staticmethod
    def _herdr_cli_binary() -> str:
        discovered = shutil.which("herdr")
        if discovered:
            return discovered
        local = Path.home() / ".local" / "bin" / "herdr"
        if local.is_file():
            return str(local)
        raise HerdrUnavailable("cli_not_found")

    async def _run_cli_json(
        self,
        *args: str,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._herdr_cli_binary(),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise HerdrUnavailable("cli_not_found") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=max(1.0, float(timeout_seconds)),
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise HerdrUnavailable("timeout") from None
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            detail = stderr_text or stdout_text or f"exit_{proc.returncode}"
            # Herdr's CLI emits this stable error code when its socket is absent.
            if detail.strip() == "server_not_running":
                raise HerdrUnavailable("server_not_running")
            try:
                error_payload = json.loads(detail)
            except json.JSONDecodeError:
                error_payload = None
            error = error_payload.get("error") if isinstance(error_payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else error
            if code == "server_not_running":
                raise HerdrUnavailable("server_not_running")
            raise RuntimeError(f"herdr_cli_failed: {detail[:500]}")
        if not stdout_text:
            return {}
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"herdr_cli_invalid_json: {stdout_text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("herdr_cli_invalid_payload")
        return payload

    async def task_workspace_id(self) -> str:
        payload = await self._run_cli_json("tab", "list")
        tabs = self._tabs_from_cli(payload)
        labels_by_workspace: dict[str, set[str]] = {}
        for tab in tabs:
            workspace_id = str(tab.get("workspace_id") or "").strip()
            label = str(tab.get("label") or "").strip().casefold()
            if workspace_id and label:
                labels_by_workspace.setdefault(workspace_id, set()).add(label)
        persistent = self._persistent_tab_labels()
        candidates = [
            workspace_id
            for workspace_id, labels in labels_by_workspace.items()
            if persistent.issubset(labels)
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                "herdr_task_workspace_not_unique: expected one workspace "
                "containing Fern and Nova"
            )
        return candidates[0]

    async def create_task_tab(
        self,
        task: dict[str, Any],
        *,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = await self.task_workspace_id()
        label = self._task_tab_label(task)
        args = [
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--label",
            label,
        ]
        cwd_text = str(cwd or "").strip()
        if cwd_text:
            args.extend(["--cwd", cwd_text])
        environment = task.get("execution_environment")
        environment = environment if isinstance(environment, dict) else {}
        identity_env = {
            "PSTACK_TASK_ID": task.get("task_id"),
            "PSTACK_TRACE_ID": task.get("trace_id"),
            "HIRDA_WORKSPACE_KEY": task.get("workspace_key"),
            "HIRDA_EXECUTION_ENVIRONMENT_ID": environment.get("environment_id"),
            "HIRDA_COMPUTER_MODE": environment.get("mode"),
            "HIRDA_ISOLATION_LEVEL": environment.get("isolation_level"),
        }
        for key, value in identity_env.items():
            value_text = str(value or "").strip()
            if value_text:
                args.extend(["--env", f"{key}={value_text}"])
        args.append("--no-focus")
        payload = await self._run_cli_json(*args)
        tab, root_pane = self._tab_from_create_cli(payload)
        tab_id = str(tab.get("tab_id") or "").strip()
        if not tab_id:
            raise RuntimeError("herdr_task_tab_create_missing_id")
        return {
            "status": "open",
            "owner": "pstack",
            "persistent": False,
            "workspace_id": str(tab.get("workspace_id") or workspace_id),
            "tab_id": tab_id,
            "root_pane_id": str(root_pane.get("pane_id") or "").strip() or None,
            "label": str(tab.get("label") or label),
            "created_at": _now(),
        }

    @staticmethod
    def _cognitive_tab_label(
        *,
        principal_id: str,
        request_id: str,
        runtime_id: str,
    ) -> str:
        principal = " ".join(str(principal_id or "").split()) or "anonymous"
        request = " ".join(str(request_id or "").split()) or "request"
        runtime = " ".join(str(runtime_id or "").split()) or "runtime"
        return f"[Cognitive] {principal}:{runtime} · {request[:48]}"

    async def create_cognitive_tab(
        self,
        *,
        principal_id: str,
        request_id: str,
        runtime_id: str,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Create one disposable, unfocused tab for isolated cognitive execution."""
        workspace_id = await self.task_workspace_id()
        label = self._cognitive_tab_label(
            principal_id=principal_id,
            request_id=request_id,
            runtime_id=runtime_id,
        )
        args = [
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--label",
            label,
        ]
        cwd_text = str(cwd or "").strip()
        if cwd_text:
            args.extend(["--cwd", cwd_text])
        identity_env = {
            "HIRDA_COGNITIVE_REQUEST_ID": request_id,
            "HIRDA_PRINCIPAL_ID": principal_id,
            "HIRDA_RUNTIME_ID": runtime_id,
        }
        for key, value in identity_env.items():
            value_text = str(value or "").strip()
            if value_text:
                args.extend(["--env", f"{key}={value_text}"])
        args.append("--no-focus")
        payload = await self._run_cli_json(*args)
        tab, root_pane = self._tab_from_create_cli(payload)
        tab_id = str(tab.get("tab_id") or "").strip()
        root_pane_id = str(root_pane.get("pane_id") or "").strip()
        if not tab_id or not root_pane_id:
            raise RuntimeError("herdr_cognitive_tab_create_missing_id")
        return {
            "status": "open",
            "owner": "cognitive",
            "persistent": False,
            "workspace_id": str(tab.get("workspace_id") or workspace_id),
            "tab_id": tab_id,
            "root_pane_id": root_pane_id,
            "label": str(tab.get("label") or label),
            "principal_id": str(principal_id or "").strip(),
            "request_id": str(request_id or "").strip(),
            "runtime_id": str(runtime_id or "").strip().casefold(),
            "created_at": _now(),
        }

    async def close_cognitive_tab(self, tab: dict[str, Any]) -> dict[str, Any]:
        """Close only a tab explicitly created by cognitive bootstrap."""
        tab_id = str(tab.get("tab_id") or "").strip()
        if not tab_id:
            return {
                **tab,
                "status": "closed",
                "closed_at": _now(),
                "already_closed": True,
            }
        payload = await self._run_cli_json("tab", "list")
        live_tabs = self._tabs_from_cli(payload)
        live = next(
            (row for row in live_tabs if str(row.get("tab_id") or "") == tab_id),
            None,
        )
        if live is None:
            return {
                **tab,
                "status": "closed",
                "closed_at": _now(),
                "already_closed": True,
            }
        live_label = str(live.get("label") or tab.get("label") or "").strip()
        if live_label.casefold() in self._persistent_tab_labels():
            raise RuntimeError("herdr_persistent_tab_close_refused")
        if str(tab.get("owner") or "").casefold() != "cognitive":
            raise RuntimeError("herdr_non_cognitive_tab_close_refused")
        if not live_label.startswith("[Cognitive] "):
            raise RuntimeError("herdr_cognitive_tab_label_mismatch")
        await self._run_cli_json("tab", "close", tab_id)
        return {
            **tab,
            "status": "closed",
            "label": live_label or tab.get("label"),
            "closed_at": _now(),
        }

    async def close_task_tab(self, tab: dict[str, Any]) -> dict[str, Any]:
        tab_id = str(tab.get("tab_id") or "").strip()
        if not tab_id:
            return {
                **tab,
                "status": "closed",
                "closed_at": _now(),
                "already_closed": True,
            }
        payload = await self._run_cli_json("tab", "list")
        live_tabs = self._tabs_from_cli(payload)
        live = next(
            (row for row in live_tabs if str(row.get("tab_id") or "") == tab_id),
            None,
        )
        if live is None:
            return {
                **tab,
                "status": "closed",
                "closed_at": _now(),
                "already_closed": True,
            }
        live_label = str(live.get("label") or tab.get("label") or "").strip()
        if live_label.casefold() in self._persistent_tab_labels():
            raise RuntimeError("herdr_persistent_tab_close_refused")
        if str(tab.get("owner") or "").casefold() != "pstack":
            raise RuntimeError("herdr_non_pstack_tab_close_refused")
        await self._run_cli_json("tab", "close", tab_id)
        return {
            **tab,
            "status": "closed",
            "label": live_label or tab.get("label"),
            "closed_at": _now(),
        }


    async def probe_execution(self) -> None:
        """Check CLI/socket availability before reconciliation mutates tasks."""
        await self._run_cli_json("tab", "list")

    async def reconcile_disposable_task_tabs(
        self,
        *,
        keep_tab_ids: set[str] | None = None,
        close_tab_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Reconcile disposable PStack tabs without guessing ownership state.

        Only tabs explicitly named in close_tab_ids are auto-closed. PStack-labelled
        tabs that are neither protected nor referenced by canonical task state are
        kept and reported as uncertain so a restart cannot destroy recoverable work.
        """
        keep = {
            str(tab_id).strip()
            for tab_id in (keep_tab_ids or set())
            if str(tab_id).strip()
        }
        close = {
            str(tab_id).strip()
            for tab_id in (close_tab_ids or set())
            if str(tab_id).strip()
        }
        payload = await self._run_cli_json("tab", "list")
        live_tabs = self._tabs_from_cli(payload)
        persistent = self._persistent_tab_labels()
        closed: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        kept = 0
        protected = 0
        candidates = 0

        for live in live_tabs:
            tab_id = str(live.get("tab_id") or "").strip()
            label = str(live.get("label") or "").strip()
            label_key = label.casefold()
            if not tab_id:
                continue
            if tab_id in keep:
                kept += 1
                continue
            if label_key in persistent:
                protected += 1
                continue
            if not label.startswith("[PStack] "):
                continue

            candidates += 1
            candidate = {
                "status": "open",
                "owner": "pstack",
                "persistent": False,
                "workspace_id": live.get("workspace_id"),
                "tab_id": tab_id,
                "label": label,
            }
            if tab_id not in close:
                uncertain.append({**candidate, "reconcile_disposition": "keep_report"})
                continue
            closed.append(await self.close_task_tab(candidate))

        return {
            "scanned": len(live_tabs),
            "candidates": candidates,
            "closed": len(closed),
            "kept": kept + len(uncertain),
            "protected": protected,
            "uncertain": len(uncertain),
            "uncertain_tabs": uncertain,
            "closed_tabs": closed,
        }

    async def start_agent_in_pane(
        self,
        *,
        name: str,
        kind: str,
        pane_id: str,
        timeout_ms: int = 30000,
        agent_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start one HIRDA-selected runtime inside an existing HERDR pane."""
        agent_name = str(name or "").strip()
        runtime_kind = str(kind or "").strip().casefold()
        target_pane = str(pane_id or "").strip()
        if not agent_name or not runtime_kind or not target_pane:
            raise ValueError("agent name, runtime kind, and pane id are required")
        bounded_timeout = max(1000, min(int(timeout_ms or 30000), 300000))
        clean_args = [
            str(item)
            for item in (agent_args or [])
            if str(item or "").strip()
        ]
        command = [
            "agent",
            "start",
            agent_name,
            "--kind",
            runtime_kind,
            "--pane",
            target_pane,
            "--timeout",
            str(bounded_timeout),
        ]
        if clean_args:
            command.extend(["--", *clean_args])
        payload = await self._run_cli_json(
            *command,
            timeout_seconds=(bounded_timeout / 1000.0) + 5.0,
        )
        await self.refresh()
        pane = self.find_pane(pane_id=target_pane)
        if not pane:
            raise RuntimeError("herdr_started_agent_pane_missing")
        detected_kind = str(pane.get("agent") or "").strip().casefold()
        if detected_kind != runtime_kind:
            raise RuntimeError(
                "herdr_started_agent_kind_mismatch: "
                f"expected {runtime_kind}, observed {detected_kind or 'none'}"
            )
        return {
            "name": agent_name,
            "kind": runtime_kind,
            "pane_id": target_pane,
            "agent_session": pane.get("agent_session"),
            "agent_status": pane.get("agent_status"),
            "agent_args": clean_args,
            "payload": payload,
        }

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
                agent_kind = str(agent_row.get("agent") or "").strip().casefold()
                if agent_kind:
                    row.setdefault("agent", agent_kind)
                agent_name = str(agent_row.get("name") or "").strip()
                if agent_name:
                    row.setdefault("agent_name", agent_name)
                if not row.get("agent_session") and agent_row.get("agent_session"):
                    row["agent_session"] = agent_row.get("agent_session")
                if not row.get("agent_status") and agent_row.get("agent_status"):
                    row["agent_status"] = agent_row.get("agent_status")
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
