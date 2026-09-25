from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any


class DesktopCommanderError(RuntimeError):
    pass


EXPOSED_DESKTOP_COMMANDER_TOOLS: dict[str, str] = {
    "read_file": "read",
    "read_multiple_files": "read",
    "write_file": "write",
    "write_pdf": "write",
    "create_directory": "write",
    "list_directory": "read",
    "move_file": "write",
    "get_file_info": "read",
    "edit_block": "write",
    "start_process": "execute",
    "read_process_output": "read",
    "interact_with_process": "execute",
    "force_terminate": "destructive",
}

_PATH_KEYS = {"path", "file_path", "source", "destination", "outputPath"}
_PROCESS_OWNER_TOOLS = {"read_process_output", "interact_with_process", "force_terminate"}
_PID_PATTERN = re.compile(r"Process started with PID\s+(\d+)")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


class DesktopCommanderBackend:
    """Persistent stdio MCP client used as a HIRDA machine-control backend."""

    def __init__(
        self,
        binary: str,
        *,
        timeout_seconds: float = 15.0,
        cwd: Path | None = None,
    ) -> None:
        requested = str(binary or "desktop-commander").strip() or "desktop-commander"
        resolved = shutil.which(requested) if "/" not in requested else None
        self.binary = Path(resolved or requested).expanduser()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.cwd = (cwd or Path.home()).expanduser().resolve(strict=False)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._server_info: dict[str, Any] = {}
        self._tool_cache: dict[str, dict[str, Any]] = {}
        self._pid_owners: dict[int, str] = {}

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def binary_ok(self) -> bool:
        return self.binary.is_file() and os.access(self.binary, os.X_OK)

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise DesktopCommanderError("desktop_commander_not_running")
        process.stdin.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        )
        await process.stdin.drain()

    async def _read_response(self, request_id: int) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise DesktopCommanderError("desktop_commander_not_running")
        while True:
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(), timeout=self.timeout_seconds
                )
            except TimeoutError as exc:
                raise DesktopCommanderError("desktop_commander_timeout") from exc
            if not raw:
                raise DesktopCommanderError(
                    f"desktop_commander_exited:{process.returncode}"
                )
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                raise DesktopCommanderError(
                    f"desktop_commander_rpc_error:{message['error']}"
                )
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise DesktopCommanderError("desktop_commander_invalid_result")
            return result

    async def _request_locked(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        await self._write(payload)
        return await self._read_response(request_id)

    async def _ensure_started_locked(self) -> None:
        if self.running:
            return
        if not self.binary_ok():
            raise DesktopCommanderError(
                f"desktop_commander_binary_missing:{self.binary}"
            )
        env = dict(os.environ)
        utf8_locale = env.get("LANG") or env.get("LC_CTYPE") or "C.UTF-8"
        env.setdefault("LANG", utf8_locale)
        env.setdefault("LC_CTYPE", utf8_locale)
        self._process = await asyncio.create_subprocess_exec(
            str(self.binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.cwd),
            env=env,
        )
        try:
            initialized = await self._request_locked(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "hirda-desktop-commander", "version": "1"},
                },
            )
            self._server_info = dict(initialized.get("serverInfo") or {})
            await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            await self._stop_locked()
            raise

    async def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._tool_cache = {}
        self._pid_owners = {}
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            await self._ensure_started_locked()
            if self._tool_cache and not refresh:
                return [dict(self._tool_cache[name]) for name in sorted(self._tool_cache)]
            discovered: dict[str, dict[str, Any]] = {}
            cursor: str | None = None
            for _ in range(50):
                params = {"cursor": cursor} if cursor else None
                page = await self._request_locked("tools/list", params)
                for tool in page.get("tools", []):
                    if not isinstance(tool, dict):
                        continue
                    name = str(tool.get("name") or "").strip()
                    if name in EXPOSED_DESKTOP_COMMANDER_TOOLS:
                        discovered[name] = dict(tool)
                cursor = str(page.get("nextCursor") or "").strip() or None
                if not cursor:
                    break
            self._tool_cache = discovered
            return [dict(discovered[name]) for name in sorted(discovered)]

    def _context_session_key(self, context: dict[str, Any]) -> str:
        value = str(context.get("managed_session_id") or "").strip()
        if not value:
            raise DesktopCommanderError("desktop_commander_session_context_missing")
        return value

    def _workspace_root(self, context: dict[str, Any]) -> Path | None:
        policy = context.get("policy") if isinstance(context.get("policy"), dict) else {}
        if policy.get("scope") != "workspace":
            return None
        project_path = str(context.get("project_path") or "").strip()
        if not project_path:
            raise DesktopCommanderError("desktop_commander_workspace_context_missing")
        return Path(project_path).expanduser().resolve(strict=False)

    def _normalize_path(self, root: Path, raw_value: str) -> str:
        text = str(raw_value or "").strip()
        if not text:
            return text
        if "://" in text:
            raise DesktopCommanderError("desktop_commander_url_outside_workspace")
        raw = Path(text).expanduser()
        candidate = raw if raw.is_absolute() else root / raw
        candidate = candidate.resolve(strict=False)
        if not _within(root, candidate):
            raise DesktopCommanderError(
                f"desktop_commander_scope_violation:{raw_value}"
            )
        return str(candidate)

    def _prepare_arguments(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        prepared = dict(arguments)
        root = self._workspace_root(context)
        if root is not None:
            for key in _PATH_KEYS:
                if isinstance(prepared.get(key), str):
                    prepared[key] = self._normalize_path(root, prepared[key])
            paths = prepared.get("paths")
            if isinstance(paths, list):
                prepared["paths"] = [
                    self._normalize_path(root, item)
                    if isinstance(item, str)
                    else item
                    for item in paths
                ]
            if tool_name == "start_process":
                command = str(prepared.get("command") or "").strip()
                if not command:
                    raise DesktopCommanderError("desktop_commander_command_missing")
                prepared["command"] = f"cd -- {shlex.quote(str(root))} && {command}"

        if tool_name in _PROCESS_OWNER_TOOLS:
            pid = prepared.get("pid")
            if not isinstance(pid, (int, float)):
                raise DesktopCommanderError("desktop_commander_pid_missing")
            pid_int = int(pid)
            owner = self._pid_owners.get(pid_int)
            session_key = self._context_session_key(context)
            if owner != session_key:
                raise DesktopCommanderError("desktop_commander_process_not_owned")
            prepared["pid"] = pid_int
        return prepared

    @staticmethod
    def _started_pid(result: dict[str, Any]) -> int | None:
        content = result.get("content")
        if not isinstance(content, list):
            return None
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            match = _PID_PATTERN.search(str(item.get("text") or ""))
            if match:
                return int(match.group(1))
        return None

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = str(name or "").strip()
        if tool_name not in EXPOSED_DESKTOP_COMMANDER_TOOLS:
            raise DesktopCommanderError(f"desktop_commander_tool_not_exposed:{tool_name}")
        async with self._lock:
            await self._ensure_started_locked()
            if not self._tool_cache:
                await self._list_tools_locked()
            if tool_name not in self._tool_cache:
                raise DesktopCommanderError(
                    f"desktop_commander_tool_unavailable:{tool_name}"
                )
            prepared = self._prepare_arguments(tool_name, arguments, context)
            result = await self._request_locked(
                "tools/call", {"name": tool_name, "arguments": prepared}
            )
            if bool(result.get("isError")):
                raise DesktopCommanderError(
                    f"desktop_commander_tool_error:{tool_name}"
                )
            if tool_name == "start_process":
                pid = self._started_pid(result)
                if pid is not None:
                    self._pid_owners[pid] = self._context_session_key(context)
            elif tool_name == "force_terminate":
                pid = prepared.get("pid")
                if isinstance(pid, int):
                    self._pid_owners.pop(pid, None)
            return result

    async def _list_tools_locked(self) -> list[dict[str, Any]]:
        discovered: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(50):
            page = await self._request_locked(
                "tools/list", {"cursor": cursor} if cursor else None
            )
            for tool in page.get("tools", []):
                if not isinstance(tool, dict):
                    continue
                name = str(tool.get("name") or "").strip()
                if name in EXPOSED_DESKTOP_COMMANDER_TOOLS:
                    discovered[name] = dict(tool)
            cursor = str(page.get("nextCursor") or "").strip() or None
            if not cursor:
                break
        self._tool_cache = discovered
        return [dict(discovered[name]) for name in sorted(discovered)]

    def status(self) -> dict[str, Any]:
        return {
            "ok": self.binary_ok(),
            "binary": str(self.binary),
            "binary_exists": self.binary.is_file(),
            "running": self.running,
            "server_info": dict(self._server_info),
            "exposed_tools": sorted(EXPOSED_DESKTOP_COMMANDER_TOOLS),
            "discovered_tools": sorted(self._tool_cache),
            "owned_process_count": len(self._pid_owners),
        }
