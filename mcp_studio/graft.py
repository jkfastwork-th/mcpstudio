from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from .db import Database
from .settings import Settings


ALLOWED_GRAFT_TOOLS = {
    "graft_find_code",
    "graft_file_api",
    "graft_check_freshness",
    "graft_trace_calls",
    "graft_find_all",
    "graft_repo_map",
}


class GraftError(RuntimeError):
    pass


class GraftManager:
    """HIRDA's read-only Graft context plane.

    Workspace metadata is the durable control plane. Graft never receives
    source-write, destructive, or cognitive-memory authority here.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    @property
    def enabled(self) -> bool:
        return bool(self.settings.studio.graft_enabled)

    def _defaults(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "mode": "read_only",
            "rollout_percent": float(self.settings.studio.graft_default_rollout_percent),
            "circuit_open": False,
            "circuit_reason": None,
            "write_authority": False,
            "destructive_authority": False,
            "cognitive_memory_write": False,
        }

    @staticmethod
    def _profile_from_workspace(workspace: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        metadata = workspace.get("metadata") if isinstance(workspace.get("metadata"), dict) else {}
        current = metadata.get("graft") if isinstance(metadata, dict) else {}
        profile = dict(defaults)
        if isinstance(current, dict):
            profile.update(current)
        # Authority invariants are not configurable.
        profile["mode"] = "read_only"
        profile["write_authority"] = False
        profile["destructive_authority"] = False
        profile["cognitive_memory_write"] = False
        return profile

    async def _workspace(self, key: str) -> dict[str, Any]:
        return await self.db.get_managed_workspace(key)

    async def profile(self, workspace_key: str) -> dict[str, Any]:
        workspace = await self._workspace(workspace_key)
        return self._profile_from_workspace(workspace, self._defaults())

    async def configure(
        self,
        workspace_key: str,
        *,
        enabled: bool | None = None,
        rollout_percent: float | None = None,
        actor: str = "operator",
    ) -> dict[str, Any]:
        workspace = await self._workspace(workspace_key)
        metadata = dict(workspace.get("metadata") or {})
        profile = self._profile_from_workspace(workspace, self._defaults())
        if enabled is not None:
            profile["enabled"] = bool(enabled)
        if rollout_percent is not None:
            value = float(rollout_percent)
            if value < 0 or value > 100:
                raise GraftError("rollout_percent must be within 0..100")
            profile["rollout_percent"] = value

        profile["mode"] = "read_only"
        profile["write_authority"] = False
        profile["destructive_authority"] = False
        profile["cognitive_memory_write"] = False
        metadata["graft"] = profile
        metadata["graft_updated_by"] = actor
        updated = await self.db.update_managed_workspace_metadata(workspace_key, metadata)
        await self.db.add_audit(
            "graft.workspace.configure",
            actor=actor,
            target_type="managed_workspace",
            target_id=workspace_key,
            data={
                "enabled": profile["enabled"],
                "rollout_percent": profile["rollout_percent"],
                "mode": "read_only",
            },
        )
        return {
            "workspace": updated,
            "graft": profile,
            "graft_permission_profile": self.permission_profile(),
        }

    @staticmethod
    def permission_profile() -> dict[str, Any]:
        return {
            "read": True,
            "write": False,
            "execute": True,
            "destructive": False,
            "scope": "workspace",
            "fail_closed_unknown": True,
        }

    async def rollback(
        self, workspace_key: str, *, reason: str = "operator-rollback", actor: str = "operator"
    ) -> dict[str, Any]:
        workspace = await self._workspace(workspace_key)
        metadata = dict(workspace.get("metadata") or {})
        profile = self._profile_from_workspace(workspace, self._defaults())
        profile["circuit_open"] = True
        profile["circuit_reason"] = str(reason or "operator-rollback")[:500]
        metadata["graft"] = profile
        metadata["graft_updated_by"] = actor
        await self.db.update_managed_workspace_metadata(workspace_key, metadata)
        await self.db.add_audit(
            "graft.workspace.rollback",
            actor=actor,
            target_type="managed_workspace",
            target_id=workspace_key,
            data={"reason": profile["circuit_reason"]},
        )
        return await self.status(workspace_key)

    async def rearm(self, workspace_key: str, *, actor: str = "operator") -> dict[str, Any]:
        workspace = await self._workspace(workspace_key)
        metadata = dict(workspace.get("metadata") or {})
        profile = self._profile_from_workspace(workspace, self._defaults())
        profile["circuit_open"] = False
        profile["circuit_reason"] = None
        metadata["graft"] = profile
        metadata["graft_updated_by"] = actor
        await self.db.update_managed_workspace_metadata(workspace_key, metadata)
        await self.db.add_audit(
            "graft.workspace.rearm",
            actor=actor,
            target_type="managed_workspace",
            target_id=workspace_key,
        )
        return await self.status(workspace_key)

    def _cli_path(self) -> Path:
        return Path(self.settings.studio.graft_cli_path).expanduser().resolve()

    async def status(self, workspace_key: str) -> dict[str, Any]:
        workspace = await self._workspace(workspace_key)
        profile = self._profile_from_workspace(workspace, self._defaults())
        project = Path(str(workspace["project_path"])).resolve()
        cli = self._cli_path()
        graph_candidates = [
            project / "graft" / ".graph" / "wiring.json",
            project / "graft" / "workspace.json",
        ]
        graph_files = [str(p) for p in graph_candidates if p.is_file()]
        available = bool(
            self.enabled
            and profile["enabled"]
            and not profile["circuit_open"]
            and cli.is_file()
            and project.is_dir()
        )
        return {
            "workspace_key": workspace_key,
            "project_path": str(project),
            "global_enabled": self.enabled,
            "profile": profile,
            "graft_permission_profile": self.permission_profile(),
            "cli_path": str(cli),
            "cli_exists": cli.is_file(),
            "graph_files": graph_files,
            "graph_present": bool(graph_files),
            "available": available,
            "fallback_mode": "serena-only" if not available else None,
        }

    @staticmethod
    def _eligible(request_id: str, rollout_percent: float) -> bool:
        if rollout_percent <= 0:
            return False
        if rollout_percent >= 100:
            return True
        bucket = int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8], 16) % 10_000
        return bucket < int(rollout_percent * 100)

    async def _read_rpc(self, proc: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
        assert proc.stdout is not None
        timeout = float(self.settings.studio.graft_request_timeout_seconds)
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not line:
                stderr = ""
                if proc.stderr is not None:
                    try:
                        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")[:2000]
                    except Exception:
                        pass
                raise GraftError(f"Graft MCP closed before response id={request_id}: {stderr}")
            try:
                item = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if item.get("id") == request_id:
                return item

    async def _tool_call(
        self, project_path: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        cli = self._cli_path()
        if not cli.is_file():
            raise GraftError(f"Graft CLI not found: {cli}")
        proc = await asyncio.create_subprocess_exec(
            str(self.settings.studio.graft_node_executable),
            str(cli),
            "mcp",
            project_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert proc.stdin is not None

            def send(payload: dict[str, Any]) -> None:
                proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))

            send({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "hirda-graft", "version": "1"},
                },
            })
            await proc.stdin.drain()
            init = await self._read_rpc(proc, 1)
            if "error" in init:
                raise GraftError(f"Graft initialize failed: {init['error']}")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            })
            await proc.stdin.drain()
            response = await self._read_rpc(proc, 2)
            if "error" in response:
                raise GraftError(f"Graft tool RPC failed: {response['error']}")
            return response.get("result") or {}
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def query(
        self,
        workspace_key: str,
        *,
        question: str | None = None,
        tool: str | None = None,
        arguments: dict[str, Any] | None = None,
        request_id: str | None = None,
        actor: str = "operator",
    ) -> dict[str, Any]:
        workspace = await self._workspace(workspace_key)
        profile = self._profile_from_workspace(workspace, self._defaults())
        status = await self.status(workspace_key)

        if not self.enabled or not profile["enabled"]:
            return {
                "mode": "serena-only",
                "fallback_required": True,
                "reason": "graft-disabled",
                "workspace_key": workspace_key,
            }
        if profile["circuit_open"]:
            return {
                "mode": "serena-only",
                "fallback_required": True,
                "reason": "circuit-open",
                "circuit_reason": profile["circuit_reason"],
                "workspace_key": workspace_key,
            }
        rid = request_id or str(question or tool or "graft-request")
        if not self._eligible(rid, float(profile["rollout_percent"])):
            return {
                "mode": "serena-only",
                "fallback_required": True,
                "reason": "outside-canary-bucket",
                "workspace_key": workspace_key,
            }
        if not status["cli_exists"]:
            await self.rollback(workspace_key, reason="graft-cli-missing", actor=actor)
            return {
                "mode": "serena-only",
                "fallback_required": True,
                "reason": "graft-cli-missing",
                "workspace_key": workspace_key,
            }

        selected = str(tool or "graft_find_code")
        if selected not in ALLOWED_GRAFT_TOOLS:
            raise GraftError(f"unsupported Graft tool: {selected}")
        args = dict(arguments or {})
        if selected == "graft_find_code" and "query" not in args:
            if not question:
                raise GraftError("question is required when graft_find_code has no query argument")
            args["query"] = question

        try:
            result = await self._tool_call(str(workspace["project_path"]), selected, args)
        except Exception as exc:
            await self.rollback(
                workspace_key,
                reason=f"shadow-exception:{type(exc).__name__}",
                actor=actor,
            )
            return {
                "mode": "serena-only",
                "fallback_required": True,
                "reason": "rollback-on-graft-exception",
                "error": str(exc)[:1000],
                "workspace_key": workspace_key,
            }

        is_error = bool(result.get("isError"))
        if is_error:
            await self.rollback(
                workspace_key,
                reason=f"shadow-rejected:{selected}",
                actor=actor,
            )
            return {
                "mode": "serena-only",
                "fallback_required": True,
                "reason": "rollback-on-graft-rejection",
                "workspace_key": workspace_key,
                "graft_result": result,
            }

        await self.db.add_audit(
            "graft.workspace.query",
            actor=actor,
            target_type="managed_workspace",
            target_id=workspace_key,
            data={"tool": selected, "request_id": rid[:160]},
        )
        return {
            "mode": "graft-shadow",
            "fallback_required": False,
            "workspace_key": workspace_key,
            "tool": selected,
            "request_id": rid,
            "graft_result": result,
            "authority": {
                "graft": "derived-read-only-context",
                "serena_verification_required": True,
                "edit_authority": "serena-only",
            },
        }
