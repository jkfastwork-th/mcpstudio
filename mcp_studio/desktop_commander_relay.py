from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Any


EXPOSED_TOOLS = {
    "read_file",
    "read_multiple_files",
    "write_file",
    "write_pdf",
    "create_directory",
    "list_directory",
    "move_file",
    "get_file_info",
    "edit_block",
    "start_process",
    "read_process_output",
    "interact_with_process",
    "force_terminate",
}
PATH_KEYS = {"path", "file_path", "source", "destination", "outputPath"}
PID_TOOLS = {"read_process_output", "interact_with_process", "force_terminate"}
PID_RE = re.compile(r"Process started with PID\s+(\d+)")


class AgentError(RuntimeError):
    pass


def _within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([str(root), str(candidate)]) == str(root)
    except (ValueError, OSError):
        return False


class DesktopCommanderRelay:
    """Workspace-scoped local Desktop Commander MCP relay.

    Requests are serialized and stdout is consumed by a reader thread so waiting
    for a child MCP response has a bounded timeout on Windows, Linux, and macOS.
    """

    _EXIT = object()

    def __init__(
        self,
        command: list[str],
        machine_id: str,
        *,
        timeout_seconds: float = 15.0,
        client_name: str = "hirda-device-runtime",
    ) -> None:
        if not command or not all(str(part).strip() for part in command):
            raise AgentError("desktop_commander_command_required")
        self.command = [str(part) for part in command]
        self.machine_id = str(machine_id or "").strip()
        self.timeout_seconds = max(1.0, min(120.0, float(timeout_seconds)))
        self.client_name = str(client_name or "hirda-device-runtime")
        self.lock = threading.RLock()
        self.proc: subprocess.Popen[str] | None = None
        self.next_id = 1
        self.tools: dict[str, dict[str, Any]] = {}
        self.pid_owners: dict[int, str] = {}
        self._responses: queue.Queue[object] = queue.Queue()
        self._reader: threading.Thread | None = None

    def _reader_loop(self, proc: subprocess.Popen[str], responses: queue.Queue[object]) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    responses.put(message)
        finally:
            responses.put(self._EXIT)

    def start(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            env = dict(os.environ)
            if os.name != "nt":
                locale = env.get("LANG") or env.get("LC_CTYPE") or "C.UTF-8"
                env.setdefault("LANG", locale)
                env.setdefault("LC_CTYPE", locale)
            self._responses = queue.Queue()
            self.proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
                env=env,
            )
            self._reader = threading.Thread(
                target=self._reader_loop,
                args=(self.proc, self._responses),
                name=f"hirda-dc-reader-{self.machine_id or 'device'}",
                daemon=True,
            )
            self._reader.start()
            try:
                result = self._request_locked(
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": self.client_name, "version": "1"},
                    },
                )
                self._notify_locked("notifications/initialized")
                self.refresh_tools()
                if not result.get("serverInfo"):
                    raise AgentError(
                        "Desktop Commander initialize response missing serverInfo"
                    )
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        with self.lock:
            proc = self.proc
            self.proc = None
            self.tools = {}
            self.pid_owners = {}
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    def _write_locked(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
            raise AgentError("desktop_commander_not_running")
        self.proc.stdin.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.proc.stdin.flush()

    def _notify_locked(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write_locked(payload)

    def _request_locked(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        req_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        self._write_locked(payload)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError(f"desktop_commander_timeout:{method}")
            try:
                message = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise AgentError(f"desktop_commander_timeout:{method}") from exc
            if message is self._EXIT:
                code = self.proc.poll() if self.proc is not None else None
                raise AgentError(f"desktop_commander_exited:{code}")
            if not isinstance(message, dict) or message.get("id") != req_id:
                continue
            if message.get("error") is not None:
                raise AgentError(
                    f"desktop_commander_rpc_error:{message['error']}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise AgentError("desktop_commander_invalid_result")
            return result

    def refresh_tools(self) -> list[dict[str, Any]]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.start()
            result = self._request_locked("tools/list")
            self.tools = {
                str(tool.get("name")): dict(tool)
                for tool in result.get("tools", [])
                if isinstance(tool, dict)
                and str(tool.get("name")) in EXPOSED_TOOLS
            }
            return [self.tools[name] for name in sorted(self.tools)]

    @staticmethod
    def _normalize_path(root: Path, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return raw
        if "://" in raw:
            raise AgentError("url_outside_workspace")
        windows_path = PureWindowsPath(raw)
        if os.name != "nt" and windows_path.drive:
            raise AgentError(f"workspace_scope_violation:{value}")
        candidate = Path(
            raw.replace("\\", os.sep).replace("/", os.sep)
        ).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        root = Path(os.path.abspath(root))
        if not _within(root, candidate):
            raise AgentError(f"workspace_scope_violation:{value}")
        return str(candidate)

    def _prepare(
        self,
        name: str,
        args: dict[str, Any],
        workspace_root: str,
        session_id: str,
    ) -> dict[str, Any]:
        root = Path(workspace_root).expanduser()
        if not root.is_absolute():
            raise AgentError("workspace_root_must_be_absolute")
        root = Path(os.path.abspath(root))
        root.mkdir(parents=True, exist_ok=True)
        prepared = dict(args or {})
        for key in PATH_KEYS:
            if isinstance(prepared.get(key), str):
                prepared[key] = self._normalize_path(root, prepared[key])
        if isinstance(prepared.get("paths"), list):
            prepared["paths"] = [
                self._normalize_path(root, item) if isinstance(item, str) else item
                for item in prepared["paths"]
            ]
        if name == "start_process":
            command = str(prepared.get("command") or "").strip()
            if not command:
                raise AgentError("command_missing")
            if os.name == "nt":
                escaped_root = str(root).replace('"', '""')
                prepared["command"] = f'cd /d "{escaped_root}" && {command}'
            else:
                prepared["command"] = (
                    f"cd -- {shlex.quote(str(root))} && {command}"
                )
        if name in PID_TOOLS:
            pid = prepared.get("pid")
            if not isinstance(pid, (int, float)):
                raise AgentError("pid_missing")
            pid = int(pid)
            if self.pid_owners.get(pid) != session_id:
                raise AgentError("process_not_owned_by_session")
            prepared["pid"] = pid
        return prepared

    @staticmethod
    def _started_pid(result: dict[str, Any]) -> int | None:
        for block in result.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            match = PID_RE.search(str(block.get("text") or ""))
            if match:
                return int(match.group(1))
        return None

    def call(
        self,
        name: str,
        args: dict[str, Any],
        workspace_root: str,
        session_id: str,
    ) -> dict[str, Any]:
        if name not in EXPOSED_TOOLS:
            raise AgentError(f"tool_not_exposed:{name}")
        with self.lock:
            self.start()
            if name not in self.tools:
                self.refresh_tools()
            if name not in self.tools:
                raise AgentError(f"tool_unavailable:{name}")
            prepared = self._prepare(name, args, workspace_root, session_id)
            result = self._request_locked(
                "tools/call",
                {"name": name, "arguments": prepared},
            )
            if result.get("isError"):
                raise AgentError(f"tool_error:{name}")
            if name == "start_process":
                pid = self._started_pid(result)
                if pid is not None:
                    self.pid_owners[pid] = session_id
            elif (
                name == "force_terminate"
                and isinstance(prepared.get("pid"), int)
            ):
                self.pid_owners.pop(prepared["pid"], None)
            return result
