from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import socket
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database
from .git_worktrees import is_registered_git_worktree_path
from .mcp_client import MCPClient
from .settings import Settings
from .tool_permissions import effective_policy


class ManagedSessionError(RuntimeError):
    pass


class WorkspaceNotAllowed(ManagedSessionError):
    pass


class ManagedSessionConflict(ManagedSessionError):
    pass


@dataclass(slots=True)
class RunningInstance:
    session_id: str
    process: asyncio.subprocess.Process
    log_handle: Any


class ManagedSessionManager:
    """Owns durable project pins and one Serena process per managed session.

    Serena's active project is process-scoped. Isolation is therefore provided
    by process boundaries, not by trusting MCP transport/session state.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._instances: dict[str, RunningInstance] = {}
        self._lock = asyncio.Lock()
        self._create_lock = asyncio.Lock()
        self._workspace_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.studio.managed_session_enabled)

    def _roots(self) -> list[Path]:
        roots: list[Path] = []
        for value in self.settings.studio.managed_session_workspace_roots:
            try:
                roots.append(Path(value).expanduser().resolve())
            except Exception:
                continue
        return roots

    def validate_project_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise WorkspaceNotAllowed("project path is required")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise WorkspaceNotAllowed("project path must be absolute")
        # Resolve before the root check so ``..`` components and symlink escapes
        # are judged by their canonical target, not by their textual prefix.
        path = candidate.resolve()
        if self.settings.studio.managed_session_require_existing_path and not path.is_dir():
            raise WorkspaceNotAllowed(f"project path does not exist: {path}")
        roots = self._roots()
        if not roots:
            raise WorkspaceNotAllowed("no managed_session_workspace_roots are configured")
        inside_approved_root = any(path == root or root in path.parents for root in roots)
        allow_worktree_siblings = bool(
            getattr(self.settings.studio, "managed_session_allow_git_worktree_siblings", False)
        )
        inside_registered_worktree = allow_worktree_siblings and any(
            is_registered_git_worktree_path(root, path) for root in roots
        )
        if not inside_approved_root and not inside_registered_worktree:
            raise WorkspaceNotAllowed(f"project path is outside approved roots: {path}")
        return str(path)

    @staticmethod
    def _workspace_key_from_path(project_path: str) -> str:
        """Derive a stable human-readable workspace key from a canonical path."""
        base = Path(project_path).name.strip().lower() or "workspace"
        safe = []
        previous_dash = False
        for ch in base:
            allowed = ch.isascii() and (ch.isalnum() or ch in "._-")
            out = ch if allowed else "-"
            if out == "-" and previous_dash:
                continue
            safe.append(out)
            previous_dash = out == "-"
        key = "".join(safe).strip("._-") or "workspace"
        return key[:96].rstrip("._-") or "workspace"

    async def _workspace_for_project_path(
        self, *, project_path: str, name: str | None = None, actor: str = "operator"
    ) -> dict[str, Any]:
        """Return/create the durable workspace row for an approved canonical path."""
        normalized = self.validate_project_path(project_path)
        async with self._workspace_lock:
            existing = await self.db.find_managed_workspace_by_project(normalized)
            if existing:
                return existing

            base = self._workspace_key_from_path(normalized)
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            candidates = [base]
            for width in (8, 12, 16, 24, 32, 64):
                suffix = digest[:width]
                prefix = base[: max(1, 120 - len(suffix) - 1)].rstrip("._-") or "workspace"
                candidates.append(f"{prefix}-{suffix}")

            for key in candidates:
                try:
                    by_key = await self.db.get_managed_workspace(key)
                except KeyError:
                    return await self.register_workspace(
                        key=key,
                        project_path=normalized,
                        name=name or key,
                        actor=f"{actor}:auto-path",
                    )
                if by_key["project_path"] == normalized:
                    return by_key

            # A full SHA-256 collision with an existing manually-created key is
            # extraordinarily unlikely, but fail safely rather than overwrite it.
            raise ManagedSessionConflict(
                f"could not allocate collision-safe workspace key for project path {normalized}"
            )

    async def start(self) -> None:
        if not self.enabled:
            return
        # Static config entries are allowlisted seeds. Durable runtime
        # registrations survive restarts and must never be silently retargeted
        # by a later config seed that happens to reuse their key.
        for key, path in (self.settings.studio.managed_session_workspaces or {}).items():
            try:
                key = str(key)
                normalized = self.validate_project_path(path)
                try:
                    by_key = await self.db.get_managed_workspace(key)
                except KeyError:
                    by_key = None
                by_path = await self.db.find_managed_workspace_by_project(normalized)

                if by_key and by_key["project_path"] == normalized:
                    # Already durable. Preserve runtime metadata rather than
                    # rewriting its provenance to ``config`` on every start.
                    continue
                if by_key and (by_key.get("metadata") or {}).get("source") != "config":
                    raise ManagedSessionConflict(
                        f"config workspace key {key} conflicts with durable runtime registration "
                        f"for {by_key['project_path']}"
                    )
                if by_path and by_path["key"] != key:
                    raise ManagedSessionConflict(
                        f"config workspace {key} path is already registered as {by_path['key']}"
                    )

                # Preserve historical config behavior for config-owned keys,
                # including an intentional config path change.
                await self.db.upsert_managed_workspace(
                    key=key, name=key, project_path=normalized,
                    metadata={"source": "config"},
                )
            except Exception as exc:
                await self.db.add_event(
                    "managed.workspace.seed_failed",
                    f"Managed workspace {key} could not be seeded: {exc}",
                    severity="warning",
                )
        if self.settings.studio.managed_session_auto_restore:
            for item in await self.db.list_managed_sessions(limit=500):
                if item.get("desired_state") == "running":
                    try:
                        await self.ensure_running(item["id"])
                    except Exception as exc:
                        await self.db.update_managed_session_runtime(
                            item["id"], status="error", error=str(exc), pid=None,
                        )
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="managed-session-monitor")

    async def stop(self) -> None:
        self._stopping = True
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        for session_id in list(self._instances):
            await self._terminate_instance(session_id, preserve_desired=True)

    async def _monitor_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(max(2.0, float(self.settings.studio.managed_session_monitor_interval_seconds)))
            for session_id, instance in list(self._instances.items()):
                rc = instance.process.returncode
                if rc is None:
                    continue
                if self._instances.get(session_id) is instance:
                    self._instances.pop(session_id, None)
                try:
                    instance.log_handle.close()
                except Exception:
                    pass
                item = await self.db.get_managed_session(session_id)
                updated = await self.db.update_managed_session_runtime(
                    session_id,
                    status="error",
                    error=f"Serena process exited rc={rc}",
                    pid=None,
                    expected_pid=instance.process.pid,
                )
                if updated.get("pid") is not None:
                    # A newer Serena generation already owns this durable
                    # session. Never let a stale process watcher clobber it.
                    continue
                await self.db.add_event(
                    "managed.session.process_exited",
                    f"Managed Serena process exited for {session_id} rc={rc}",
                    severity="warning",
                    data={"managed_session_id": session_id, "returncode": rc},
                )
                if (
                    not self._stopping
                    and item.get("desired_state") == "running"
                    and self.settings.studio.managed_session_auto_restart
                ):
                    try:
                        await self.ensure_running(session_id)
                    except Exception:
                        pass
            if int(self.settings.studio.managed_session_idle_stop_seconds or 0) > 0:
                try:
                    await self._stop_idle_sessions()
                except Exception as exc:
                    await self.db.add_event(
                        "managed.session.idle_reconcile_failed",
                        f"Managed session idle reconciliation failed: {exc}",
                        severity="warning",
                    )

    async def _stop_idle_sessions(self) -> None:
        threshold = int(self.settings.studio.managed_session_idle_stop_seconds or 0)
        if threshold <= 0:
            return
        now = datetime.now(timezone.utc)
        for item in await self.db.managed_session_overview(limit=500):
            if item.get("status") != "ready" or item.get("connected_transports"):
                continue
            stamp = item.get("last_used_at") or item.get("last_transport_seen_at") or item.get("last_started_at")
            if not stamp:
                continue
            try:
                seen = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - seen).total_seconds() < threshold:
                continue
            await self._terminate_instance(item["id"], preserve_desired=False)
            await self.db.add_audit(
                "managed.session.idle_stop", actor="system/lifecycle", target_type="managed_session",
                target_id=item["id"], data={"idle_seconds": threshold},
            )
            await self.db.add_event(
                "managed.session.idle_stopped",
                f"Managed session {item['id']} auto-stopped after idle timeout",
                data={"managed_session_id": item["id"], "workspace_key": item["workspace_key"], "idle_seconds": threshold},
            )

    def _port_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", int(port)))
                return True
            except OSError:
                return False

    async def _allocate_port(self) -> int:
        # Stopped managed sessions are durable project pins and still own
        # their reserved ports. Reserving all persisted ports prevents a
        # rerun from colliding with managed_sessions.port UNIQUE.
        used = {
            int(item["port"])
            for item in await self.db.list_managed_sessions(limit=1000)
            if item.get("port")
        }
        start = int(self.settings.studio.managed_session_port_start)
        end = int(self.settings.studio.managed_session_port_end)
        for port in range(start, end + 1):
            if port not in used and self._port_available(port):
                return port
        raise ManagedSessionError(f"no free managed Serena port in {start}-{end}")

    async def register_workspace(self, *, key: str, project_path: str, name: str | None = None, actor: str = "operator") -> dict[str, Any]:
        if not self.enabled:
            raise ManagedSessionError("managed sessions are disabled")
        key = str(key or "").strip()
        if not key or len(key) > 120 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in key):
            raise WorkspaceNotAllowed("workspace key must use A-Z, a-z, 0-9, dot, underscore or dash")
        normalized = self.validate_project_path(project_path)
        item = await self.db.upsert_managed_workspace(
            key=key, name=(name or key).strip()[:240], project_path=normalized,
            metadata={"source": actor},
        )
        await self.db.add_audit(
            "managed.workspace.register", actor=actor, target_type="managed_workspace", target_id=key,
            data={"project_path": normalized},
        )
        return item

    async def list_workspaces(self) -> list[dict[str, Any]]:
        return await self.db.list_managed_workspaces()

    async def ensure_workspace_selector_session(
        self, *, workspace: str, name: str | None = None, actor: str = "operator"
    ) -> dict[str, Any]:
        """Resolve a registered key or an approved absolute project path.

        Absolute paths are auto-registered durably using a collision-safe key.
        Existing path registrations are reused, so a config alias such as
        ``oriverse`` remains canonical when its project path is supplied.
        """
        selector = str(workspace or "").strip()
        if not selector:
            raise WorkspaceNotAllowed("workspace selector is required")
        candidate = Path(selector).expanduser()
        if candidate.is_absolute():
            item = await self._workspace_for_project_path(
                project_path=selector, name=name, actor=actor
            )
            workspace_key = item["key"]
        else:
            workspace_key = selector
        return await self.ensure_workspace_session(
            workspace_key=workspace_key, name=name, actor=actor
        )

    async def ensure_workspace_session(self, *, workspace_key: str, name: str | None = None, actor: str = "operator") -> dict[str, Any]:
        """Return a durable managed session for a workspace, creating/resuming as needed.

        Phase C uses this for production cutover so a client can say "use workspace X"
        without knowing a managed-session id. The project path remains the durable pin.
        """
        if not self.enabled:
            raise ManagedSessionError("managed sessions are disabled")
        workspace = await self.db.get_managed_workspace(workspace_key)
        project = self.validate_project_path(workspace["project_path"])
        async with self._create_lock:
            existing = await self.db.find_managed_session_by_project(project)
            if existing:
                if existing.get("desired_state") != "running":
                    await self.db.set_managed_session_desired_state(existing["id"], "running")
                target_id = existing["id"]
                created = False
            else:
                port = await self._allocate_port()
                item = await self.db.create_managed_session(
                    name=(name or workspace.get("name") or workspace_key).strip()[:240],
                    workspace_key=workspace_key,
                    project_path=project,
                    server_id=self.settings.studio.worker_server_id or self.settings.servers[0].id,
                    port=port,
                    desired_state="running",
                    metadata={"created_by": actor, "phase": "m6.2.3c"},
                )
                target_id = item["id"]
                created = True
        ready = await self.ensure_running(target_id)
        await self.db.add_audit(
            "managed.session.use_workspace", actor=actor, target_type="managed_session", target_id=target_id,
            data={"workspace_key": workspace_key, "project_path": project, "created": created},
        )
        return ready

    async def create_session(self, *, name: str, workspace_key: str, actor: str = "operator") -> dict[str, Any]:
        if not self.enabled:
            raise ManagedSessionError("managed sessions are disabled")
        workspace = await self.db.get_managed_workspace(workspace_key)
        project = self.validate_project_path(workspace["project_path"])
        reused: dict[str, Any] | None = None
        # Serialize project-pin creation and port allocation inside the Studio
        # process. A stopped row is a durable session and is resumed rather
        # than duplicated.
        async with self._create_lock:
            existing = await self.db.find_managed_session_by_project(project)
            if existing:
                if existing.get("desired_state") == "running":
                    raise ManagedSessionConflict(
                        f"workspace {workspace_key} is already pinned by managed session {existing['id']}"
                    )
                await self.db.set_managed_session_desired_state(existing["id"], "running")
                reused = await self.db.get_managed_session(existing["id"])
            else:
                port = await self._allocate_port()
                item = await self.db.create_managed_session(
                    name=name.strip()[:240] or workspace_key,
                    workspace_key=workspace_key,
                    project_path=project,
                    server_id=self.settings.studio.worker_server_id or self.settings.servers[0].id,
                    port=port,
                    desired_state="running",
                    metadata={"created_by": actor},
                )
        if reused is not None:
            await self.db.add_audit(
                "managed.session.resume", actor=actor, target_type="managed_session", target_id=reused["id"],
                data={"workspace_key": workspace_key, "project_path": project, "port": reused["port"]},
            )
            return await self.ensure_running(reused["id"])
        await self.db.add_audit(
            "managed.session.create", actor=actor, target_type="managed_session", target_id=item["id"],
            data={"workspace_key": workspace_key, "project_path": project, "port": port},
        )
        return await self.ensure_running(item["id"])

    async def _spawn(self, item: dict[str, Any]) -> RunningInstance:
        executable = str(self.settings.studio.managed_session_serena_executable)
        cwd = str(self.settings.studio.managed_session_serena_working_directory)
        if not Path(executable).is_file():
            raise ManagedSessionError(f"Serena executable not found: {executable}")
        if not Path(cwd).is_dir():
            raise ManagedSessionError(f"Serena working directory not found: {cwd}")
        log_dir = Path(self.settings.studio.managed_session_log_dir).expanduser()
        if not log_dir.is_absolute():
            log_dir = (Path(self.settings.config_path).parent / log_dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{item['id']}.log"
        log_handle = open(log_path, "ab", buffering=0)

        # Managed Serena processes inherit Studio's systemd sandbox. Keep HOME
        # intact for Serena config, but move mutable cache/temp state into the
        # writable managed-session data tree.
        runtime_root = log_dir / ".runtime" / str(item["id"])
        cache_dir = runtime_root / "cache"
        data_dir = runtime_root / "data"
        bin_dir = runtime_root / "bin"
        uv_cache_dir = cache_dir / "uv"
        uv_tool_dir = data_dir / "uv" / "tools"
        tmp_dir = runtime_root / "tmp"
        for path in (runtime_root, cache_dir, data_dir, bin_dir, uv_cache_dir, uv_tool_dir, tmp_dir):
            path.mkdir(parents=True, exist_ok=True)
        runtime_root.chmod(0o700)

        env = os.environ.copy()
        if self.settings.studio.managed_session_home:
            env["HOME"] = str(self.settings.studio.managed_session_home)
        if self.settings.studio.managed_session_path:
            env["PATH"] = str(self.settings.studio.managed_session_path)
        env["XDG_CACHE_HOME"] = str(cache_dir)
        env["XDG_DATA_HOME"] = str(data_dir)
        env["UV_CACHE_DIR"] = str(uv_cache_dir)
        env["UV_TOOL_DIR"] = str(uv_tool_dir)
        env["UV_TOOL_BIN_DIR"] = str(bin_dir)
        env["TMPDIR"] = str(tmp_dir)
        cmd = [
            executable,
            "start-mcp-server",
            "--transport", "streamable-http",
            "--port", str(item["port"]),
            "--context", str(self.settings.studio.managed_session_context),
            "--project", str(item["project_path"]),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            raise
        await self.db.update_managed_session_runtime(
            item["id"], status="starting", pid=process.pid, error=None,
            endpoint=f"http://127.0.0.1:{item['port']}/mcp",
            log_path=str(log_path), increment_generation=True,
        )
        return RunningInstance(item["id"], process, log_handle)

    async def _wait_ready(self, item: dict[str, Any], process: asyncio.subprocess.Process) -> None:
        endpoint = f"http://127.0.0.1:{item['port']}/mcp"
        deadline = asyncio.get_running_loop().time() + float(self.settings.studio.managed_session_start_timeout_seconds)
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise ManagedSessionError(f"Serena exited before readiness rc={process.returncode}")
            try:
                probe = await MCPClient(
                    endpoint,
                    timeout=min(3.0, float(self.settings.studio.request_timeout_seconds)),
                    protocol_version="2025-06-18",
                ).probe()
                if probe.server_info:
                    return
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.35)
        raise ManagedSessionError(f"managed Serena readiness timeout: {last_error or 'no response'}")

    async def ensure_running(self, session_id: str) -> dict[str, Any]:
        if not self.enabled:
            raise ManagedSessionError("managed sessions are disabled")
        async with self._lock:
            item = await self.db.get_managed_session(session_id)
            running = self._instances.get(session_id)
            if running and running.process.returncode is None:
                return await self.db.get_managed_session(session_id)
            if item.get("desired_state") != "running":
                await self.db.set_managed_session_desired_state(session_id, "running")
                item = await self.db.get_managed_session(session_id)
            if not self._port_available(int(item["port"])):
                # If Studio has no live child for this session, avoid attaching
                # to an unrelated process that happens to own the old port.
                port = await self._allocate_port()
                await self.db.update_managed_session_runtime(session_id, port=port, status="stopped", pid=None)
                item = await self.db.get_managed_session(session_id)
            instance = await self._spawn(item)
            self._instances[session_id] = instance
            try:
                latest = await self.db.get_managed_session(session_id)
                await self._wait_ready(latest, instance.process)
                latest = await self.db.update_managed_session_runtime(
                    session_id, status="ready", pid=instance.process.pid, error=None,
                    endpoint=f"http://127.0.0.1:{latest['port']}/mcp",
                )
                await self.db.add_event(
                    "managed.session.ready",
                    f"Managed Serena session {session_id} ready for {latest['workspace_key']}",
                    data={"managed_session_id": session_id, "workspace_key": latest["workspace_key"], "port": latest["port"]},
                )
                return latest
            except Exception as exc:
                await self._terminate_instance(session_id, preserve_desired=True)
                await self.db.update_managed_session_runtime(session_id, status="error", pid=None, error=str(exc))
                raise

    async def _terminate_instance(self, session_id: str, *, preserve_desired: bool) -> None:
        instance = self._instances.pop(session_id, None)
        expected_pid: int | None | object = ...
        if instance:
            process = instance.process
            expected_pid = process.pid
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=float(self.settings.studio.managed_session_stop_timeout_seconds))
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        await process.wait()
                    except Exception:
                        pass
            try:
                instance.log_handle.close()
            except Exception:
                pass
        if not preserve_desired:
            await self.db.set_managed_session_desired_state(session_id, "stopped")
        await self.db.update_managed_session_runtime(
            session_id,
            status="stopped",
            pid=None,
            error=None,
            expected_pid=expected_pid,
        )

    async def stop_session(self, session_id: str, *, actor: str = "operator") -> dict[str, Any]:
        connected = await self.db.count_connected_gateways_for_managed_session(session_id)
        if connected:
            raise ManagedSessionConflict(f"managed session has {connected} connected gateway session(s); detach them first")
        await self._terminate_instance(session_id, preserve_desired=False)
        await self.db.add_audit(
            "managed.session.stop", actor=actor, target_type="managed_session", target_id=session_id,
        )
        return await self.db.get_managed_session(session_id)

    async def restart_session(self, session_id: str, *, actor: str = "operator") -> dict[str, Any]:
        connected = await self.db.count_connected_gateways_for_managed_session(session_id)
        if connected:
            raise ManagedSessionConflict(f"managed session has {connected} connected gateway session(s); detach them first")
        await self._terminate_instance(session_id, preserve_desired=True)
        await self.db.set_managed_session_desired_state(session_id, "running")
        item = await self.ensure_running(session_id)
        await self.db.add_audit(
            "managed.session.restart", actor=actor, target_type="managed_session", target_id=session_id,
        )
        return item

    async def rename_session(self, session_id: str, *, name: str, actor: str = "operator") -> dict[str, Any]:
        item = await self.db.rename_managed_session(session_id, name)
        await self.db.add_audit(
            "managed.session.rename", actor=actor, target_type="managed_session", target_id=session_id,
            data={"name": item["name"]},
        )
        return item

    async def resume_session(self, session_id: str, *, actor: str = "operator") -> dict[str, Any]:
        await self.db.set_managed_session_desired_state(session_id, "running")
        item = await self.ensure_running(session_id)
        await self.db.add_audit(
            "managed.session.resume", actor=actor, target_type="managed_session", target_id=session_id,
        )
        return item

    async def history(self, session_id: str) -> dict[str, Any]:
        return await self.db.managed_session_history(
            session_id, limit=int(self.settings.studio.managed_session_history_limit or 100)
        )

    def _with_tool_permissions(self, session: dict[str, Any]) -> dict[str, Any]:
        item = dict(session)
        item["tool_permissions"] = effective_policy(self.settings.studio, item)
        item["tool_permissions_enabled"] = bool(self.settings.studio.managed_session_tool_permissions_enabled)
        return item

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return self._with_tool_permissions(await self.db.get_managed_session(session_id))

    async def list_sessions(self) -> list[dict[str, Any]]:
        sessions = await self.db.managed_session_overview(limit=500)
        return [self._with_tool_permissions(item) for item in sessions]

    async def permissions(self, session_id: str) -> dict[str, Any]:
        session = await self.db.get_managed_session(session_id)
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        override = metadata.get("tool_permissions") if isinstance(metadata, dict) else None
        return {
            "session_id": session_id,
            "workspace_key": session.get("workspace_key"),
            "enabled": bool(self.settings.studio.managed_session_tool_permissions_enabled),
            "override": dict(override) if isinstance(override, dict) else {},
            "effective": effective_policy(self.settings.studio, session),
        }

    async def update_permissions(
        self, session_id: str, changes: dict[str, Any], *, actor: str = "operator"
    ) -> dict[str, Any]:
        session = await self.db.get_managed_session(session_id)
        metadata = dict(session.get("metadata") or {})
        override = dict(metadata.get("tool_permissions") or {})
        for key in ("read", "write", "execute", "destructive", "scope", "fail_closed_unknown"):
            if key in changes:
                override[key] = changes[key]
        metadata["tool_permissions"] = override
        metadata["tool_permissions_updated_by"] = actor
        updated = await self.db.update_managed_session_metadata(session_id, metadata)
        return self._with_tool_permissions(updated)

    async def status(self) -> dict[str, Any]:
        sessions = await self.list_sessions() if self.enabled else []
        counts: dict[str, int] = {}
        for item in sessions:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "enabled": self.enabled,
            "counts": counts,
            "running_processes": sum(1 for x in self._instances.values() if x.process.returncode is None),
            "port_range": [self.settings.studio.managed_session_port_start, self.settings.studio.managed_session_port_end],
            "require_binding_for_tools": self.settings.studio.managed_session_require_binding_for_tools,
            "cutover_enabled": self.settings.studio.managed_session_cutover_enabled,
            "legacy_upstream_role": "discovery_only" if self.settings.studio.managed_session_cutover_enabled else "normal",
            "approved_roots": list(self.settings.studio.managed_session_workspace_roots),
            "allow_git_worktree_siblings": bool(
                getattr(self.settings.studio, "managed_session_allow_git_worktree_siblings", False)
            ),
            "idle_stop_seconds": int(self.settings.studio.managed_session_idle_stop_seconds or 0),
            "history_limit": int(self.settings.studio.managed_session_history_limit or 100),
            "tool_permissions_enabled": bool(self.settings.studio.managed_session_tool_permissions_enabled),
        }
