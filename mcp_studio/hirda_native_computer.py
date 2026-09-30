from __future__ import annotations

import asyncio
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class NativeComputerError(RuntimeError):
    pass


NATIVE_COMPUTER_PERMISSIONS: dict[str, str] = {
    "read_file": "read",
    "read_multiple_files": "read",
    "write_file": "write",
    "create_directory": "write",
    "list_directory": "read",
    "move_file": "write",
    "get_file_info": "read",
    "start_process": "execute",
    "read_process_output": "read",
    "interact_with_process": "execute",
    "force_terminate": "destructive",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


NATIVE_COMPUTER_TOOLS = [
    _tool("read_file", "Read a UTF-8 file inside the managed workspace.", {"path": {"type": "string"}}, ["path"]),
    _tool("read_multiple_files", "Read multiple UTF-8 files inside the managed workspace.", {"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"]),
    _tool("write_file", "Write UTF-8 text to a file inside the managed workspace.", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("create_directory", "Create a directory inside the managed workspace.", {"path": {"type": "string"}}, ["path"]),
    _tool("list_directory", "List entries in a directory inside the managed workspace.", {"path": {"type": "string"}}, ["path"]),
    _tool("move_file", "Move a file inside the managed workspace.", {"source": {"type": "string"}, "destination": {"type": "string"}}, ["source", "destination"]),
    _tool("get_file_info", "Get basic metadata for a path inside the managed workspace.", {"path": {"type": "string"}}, ["path"]),
    _tool("start_process", "Start a shell process in the managed workspace.", {"command": {"type": "string"}}, ["command"]),
    _tool("read_process_output", "Read output from a process started by this managed session.", {"pid": {"type": "integer"}, "offset": {"type": "integer", "minimum": 0}}, ["pid"]),
    _tool("interact_with_process", "Send input to a process started by this managed session.", {"pid": {"type": "integer"}, "input": {"type": "string"}}, ["pid", "input"]),
    _tool("force_terminate", "Terminate a process started by this managed session.", {"pid": {"type": "integer"}}, ["pid"]),
]
_MAX_PROCESS_OUTPUT_BYTES = 1_048_576


@dataclass
class _OwnedProcess:
    process: asyncio.subprocess.Process
    session_id: str
    output: bytearray = field(default_factory=bytearray)
    reader: asyncio.Task | None = None


class NativeComputerBackend:
    """Workspace-confined native filesystem and session-owned process provider."""

    def __init__(self, workspaces: dict[str, str] | None = None) -> None:
        self.workspaces = {str(k): Path(v).expanduser().resolve(strict=False) for k, v in (workspaces or {}).items()}
        self._processes: dict[int, _OwnedProcess] = {}
        self._lock = asyncio.Lock()

    def _root(self, context: dict[str, Any]) -> Path:
        project_path = str(context.get("project_path") or "").strip()
        workspace_key = str(context.get("workspace_key") or "").strip()
        if project_path:
            return Path(project_path).expanduser().resolve(strict=False)
        if workspace_key in self.workspaces:
            return self.workspaces[workspace_key]
        raise NativeComputerError("managed_workspace_required")

    @staticmethod
    def _path(root: Path, raw: Any) -> Path:
        value = str(raw or "").strip()
        if not value or "://" in value:
            raise NativeComputerError("workspace_path_invalid")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(root.resolve(strict=False))
        except ValueError as exc:
            raise NativeComputerError("workspace_scope_violation") from exc
        return candidate

    @staticmethod
    def _result(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    def _owned_process(self, pid: Any, context: dict[str, Any]) -> _OwnedProcess:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError) as exc:
            raise NativeComputerError("process_pid_invalid") from exc
        owner = self._processes.get(pid_int)
        session_id = str(context.get("managed_session_id") or "")
        if owner is None or not session_id or owner.session_id != session_id:
            raise NativeComputerError("process_not_owned")
        return owner

    async def _capture(self, owner: _OwnedProcess) -> None:
        stream = owner.process.stdout
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            remaining = _MAX_PROCESS_OUTPUT_BYTES - len(owner.output)
            if remaining > 0:
                owner.output.extend(chunk[:remaining])

    async def call_tool(self, name: str, arguments: dict[str, Any], *, context: dict[str, Any]) -> dict[str, Any]:
        tool = str(name or "").strip()
        if tool not in NATIVE_COMPUTER_PERMISSIONS:
            raise NativeComputerError(f"native_tool_not_supported:{tool}")
        args = dict(arguments or {})
        session_id = str(context.get("managed_session_id") or "").strip()
        if not session_id:
            raise NativeComputerError("managed_session_id_required")
        root = self._root(context)
        if tool == "read_file":
            return self._result(self._path(root, args.get("path")).read_text(encoding="utf-8"))
        if tool == "read_multiple_files":
            paths = args.get("paths")
            if not isinstance(paths, list):
                raise NativeComputerError("paths_must_be_array")
            texts = [f"{path}:\n{self._path(root, path).read_text(encoding='utf-8')}" for path in paths]
            return self._result("\n\n".join(texts))
        if tool == "write_file":
            path = self._path(root, args.get("path"))
            path.write_text(str(args.get("content") or ""), encoding="utf-8")
            return self._result(f"Wrote {path}")
        if tool == "create_directory":
            path = self._path(root, args.get("path"))
            path.mkdir(parents=True, exist_ok=True)
            return self._result(f"Created directory {path}")
        if tool == "list_directory":
            path = self._path(root, args.get("path"))
            entries = sorted(path.iterdir(), key=lambda item: item.name.lower())
            return self._result("\n".join(f"{'[dir]' if item.is_dir() else '[file]'} {item.name}" for item in entries))
        if tool == "move_file":
            source = self._path(root, args.get("source"))
            destination = self._path(root, args.get("destination"))
            shutil.move(str(source), str(destination))
            return self._result(f"Moved {source} to {destination}")
        if tool == "get_file_info":
            path = self._path(root, args.get("path"))
            info = path.stat()
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            return self._result(f"{path}\ntype: {kind}\nsize: {info.st_size}\nmodified: {info.st_mtime}")
        if tool == "start_process":
            command = str(args.get("command") or "").strip()
            if not command:
                raise NativeComputerError("process_command_required")
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
            )
            owner = _OwnedProcess(process=process, session_id=session_id)
            async with self._lock:
                self._processes[process.pid] = owner
            owner.reader = asyncio.create_task(self._capture(owner))
            return self._result(f"Process started with PID {process.pid}")
        if tool == "read_process_output":
            owner = self._owned_process(args.get("pid"), context)
            if owner.reader is not None:
                await asyncio.sleep(0)
            offset = max(0, int(args.get("offset") or 0))
            text = bytes(owner.output[offset:]).decode("utf-8", errors="replace")
            return self._result(text)
        if tool == "interact_with_process":
            owner = self._owned_process(args.get("pid"), context)
            stdin = owner.process.stdin
            if stdin is None or owner.process.returncode is not None:
                raise NativeComputerError("process_not_running")
            stdin.write(str(args.get("input") or "").encode("utf-8"))
            await stdin.drain()
            return self._result("Input sent")
        if tool == "force_terminate":
            owner = self._owned_process(args.get("pid"), context)
            if owner.process.returncode is None:
                owner.process.terminate()
                try:
                    await asyncio.wait_for(owner.process.wait(), timeout=2)
                except TimeoutError:
                    owner.process.kill()
                    await owner.process.wait()
            if owner.reader is not None:
                await owner.reader
            self._processes.pop(owner.process.pid, None)
            return self._result(f"Terminated process {owner.process.pid}")
        raise NativeComputerError(f"native_tool_not_supported:{tool}")

    async def call(self, *, name: str, arguments: dict[str, Any], workspace_key: str, session_id: str) -> dict[str, Any]:
        root = self.workspaces.get(str(workspace_key or ""))
        if root is None:
            raise NativeComputerError(f"workspace_not_configured:{workspace_key}")
        context = {
            "workspace_key": workspace_key,
            "managed_session_id": session_id,
            "policy": {"scope": "workspace"},
            "project_path": str(root),
        }
        return await self.call_tool(name, arguments, context=context)

    async def close(self) -> None:
        for pid, owner in list(self._processes.items()):
            if owner.process.returncode is None:
                owner.process.terminate()
                try:
                    await asyncio.wait_for(owner.process.wait(), timeout=2)
                except TimeoutError:
                    owner.process.kill()
                    await owner.process.wait()
            if owner.reader is not None:
                await owner.reader
            self._processes.pop(pid, None)
