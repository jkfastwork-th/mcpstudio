from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cloudflare_live import inspect_cloudflare_config, run_cloudflared_ingress_checks
from .db import Database
from .settings import Settings


TRY_CLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectivityManager:
    """M5 connectivity supervisor.

    Connectivity is deliberately isolated from execution. This manager may
    inspect/restart a tunnel process that it owns, but it never stops workers,
    cancels work, restarts Serena, or mutates Herdr panes.
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._log_handles: dict[str, Any] = {}
        self.snapshot: dict[str, Any] = {
            "status": "starting",
            "last_poll_at": None,
            "tunnels": [],
            "summary": {},
            "stale_sessions_marked": 0,
            "stale_gateway_sessions_marked": 0,
            "last_error": None,
        }

    async def start(self) -> None:
        await self.db.sync_tunnels(self.settings.tunnels)
        await self.poll_once(allow_reconnect=False)
        if self.settings.studio.connectivity_enabled:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="mcp-studio-connectivity")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Do not stop managed tunnels here. A Studio restart must not drop
        # external connectivity. PIDs are persisted and rediscovered safely.
        for handle in self._log_handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._log_handles.clear()

    def kick(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once(allow_reconnect=True)
            except Exception as exc:  # pragma: no cover - defensive loop guard
                self.snapshot["last_error"] = str(exc)
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.studio.connectivity_interval_seconds
                )
                self._wake.clear()
            except asyncio.TimeoutError:
                pass

    async def poll_once(self, *, allow_reconnect: bool = True) -> dict[str, Any]:
        stale = await self.db.mark_stale_sessions(self.settings.studio.session_stale_seconds)
        stale_gateway = await self.db.mark_stale_gateway_sessions(self.settings.studio.gateway_session_stale_seconds)
        tunnels = await self.db.list_tunnels()
        results: list[dict[str, Any]] = []
        for tunnel in tunnels:
            try:
                item = await self._probe_and_reconcile(tunnel, allow_reconnect=allow_reconnect)
            except Exception as exc:
                item = await self.db.update_tunnel_runtime(
                    tunnel["id"], status="down", error=str(exc), last_checked_at=_utcnow()
                )
            results.append(item)

        counts = {"healthy": 0, "degraded": 0, "down": 0, "unknown": 0, "stopped": 0}
        for item in results:
            state = item.get("status", "unknown")
            counts[state if state in counts else "unknown"] += 1
        enabled = [x for x in results if x.get("enabled")]
        if any(x.get("status") == "down" for x in enabled):
            overall = "degraded"
        elif enabled and all(x.get("status") == "healthy" for x in enabled):
            overall = "healthy"
        elif enabled:
            overall = "degraded"
        else:
            overall = "unknown"

        self.snapshot = {
            "status": overall,
            "last_poll_at": _utcnow(),
            "tunnels": results,
            "summary": {"total": len(results), "enabled": len(enabled), "counts": counts},
            "stale_sessions_marked": stale,
            "stale_gateway_sessions_marked": stale_gateway,
            "last_error": None,
        }
        return self.snapshot

    async def _probe_and_reconcile(
        self, tunnel: dict[str, Any], *, allow_reconnect: bool
    ) -> dict[str, Any]:
        provider = (tunnel.get("provider") or "local").lower()
        desired = tunnel.get("desired_state") or "running"
        enabled = bool(tunnel.get("enabled"))
        now = _utcnow()

        if not enabled or desired == "stopped":
            return await self.db.update_tunnel_runtime(
                tunnel["id"], status="stopped", error=None, last_checked_at=now
            )

        if provider in {"local", "direct", "openai", "external"}:
            target = tunnel.get("health_url") or tunnel.get("endpoint") or tunnel.get("origin")
            ok, detail = await self._probe_url(target)
            return await self.db.update_tunnel_runtime(
                tunnel["id"],
                status="healthy" if ok else "down",
                error=None if ok else detail,
                last_checked_at=now,
                last_healthy_at=now if ok else None,
            )

        if provider != "cloudflare":
            return await self.db.update_tunnel_runtime(
                tunnel["id"], status="down", error=f"unsupported provider: {provider}", last_checked_at=now
            )

        alive = await asyncio.to_thread(self._pid_matches, tunnel)
        endpoint = tunnel.get("health_url") or tunnel.get("endpoint")
        endpoint_ok = None
        endpoint_detail = None
        if endpoint:
            endpoint_ok, endpoint_detail = await self._probe_url(endpoint)

        if tunnel.get("managed") and not alive:
            should_restart = (
                allow_reconnect
                and self.settings.studio.connectivity_auto_reconnect
                and bool(tunnel.get("auto_reconnect"))
                and desired == "running"
            )
            if should_restart and await self._restart_backoff_elapsed(tunnel):
                try:
                    await self.start_tunnel(tunnel["id"], reconnect=True)
                    tunnel = await self.db.get_tunnel(tunnel["id"])
                    alive = await asyncio.to_thread(self._pid_matches, tunnel)
                except Exception as exc:
                    return await self.db.update_tunnel_runtime(
                        tunnel["id"], status="down", error=f"restart failed: {exc}", last_checked_at=now
                    )

        if tunnel.get("managed"):
            if alive and endpoint_ok is not False:
                status = "healthy"
                error = None
            elif alive:
                status = "degraded"
                error = endpoint_detail or "cloudflared alive but endpoint probe failed"
            else:
                status = "down"
                error = "managed cloudflared process is not running"
        else:
            status = "healthy" if endpoint_ok else "down"
            error = None if endpoint_ok else (endpoint_detail or "endpoint probe failed")

        return await self.db.update_tunnel_runtime(
            tunnel["id"],
            status=status,
            error=error,
            last_checked_at=now,
            last_healthy_at=now if status == "healthy" else None,
        )

    async def _restart_backoff_elapsed(self, tunnel: dict[str, Any]) -> bool:
        last_started = tunnel.get("started_at")
        if not last_started:
            return True
        try:
            value = datetime.fromisoformat(last_started)
            age = (datetime.now(timezone.utc) - value).total_seconds()
            return age >= self.settings.studio.tunnel_restart_backoff_seconds
        except Exception:
            return True

    async def _probe_url(self, value: str | None) -> tuple[bool, str | None]:
        if not value:
            return False, "no endpoint/origin configured"
        parsed = urlparse(value if "://" in value else f"tcp://{value}")
        host = parsed.hostname
        if not host:
            return False, f"invalid endpoint: {value}"
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self.settings.studio.request_timeout_seconds
            )
            del reader
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True, None
        except Exception as exc:
            return False, f"{host}:{port} unreachable: {exc}"

    def build_cloudflare_command(self, tunnel: dict[str, Any]) -> list[str]:
        executable = tunnel.get("executable") or "cloudflared"
        tunnel_name = tunnel.get("tunnel_name")
        config_file = tunnel.get("config_file")
        origin = tunnel.get("origin")
        if not tunnel_name:
            raise ValueError("M5 refuses Cloudflare quick tunnels because they expose the full Studio origin; configure a named path-scoped tunnel")
        cmd = [executable, "tunnel"]
        if config_file:
            cmd.extend(["--config", str(Path(config_file).expanduser())])
        cmd.extend(["run", tunnel_name])
        return cmd


    async def cloudflare_preflight(self, tunnel_id: str) -> dict[str, Any]:
        tunnel = await self.db.get_tunnel(tunnel_id)
        if (tunnel.get("provider") or "").lower() != "cloudflare":
            raise ValueError("preflight is only available for Cloudflare tunnels")
        if not tunnel.get("managed"):
            raise ValueError("preflight requires a managed named Cloudflare tunnel")
        endpoint = tunnel.get("endpoint")
        origin = tunnel.get("origin")
        config_file = tunnel.get("config_file")
        server_id = str((tunnel.get("metadata") or {}).get("server_id") or self.settings.studio.worker_server_id or "")
        if not endpoint or not origin or not config_file or not server_id:
            raise ValueError("Cloudflare preflight requires endpoint, origin, config_file, and metadata.server_id")
        if not bool((tunnel.get("metadata") or {}).get("path_scoped")):
            raise ValueError("Cloudflare preflight requires metadata.path_scoped=true")

        static = await asyncio.to_thread(
            inspect_cloudflare_config,
            config_file=config_file,
            endpoint=endpoint,
            origin=origin,
            server_id=server_id,
        )
        cli = await run_cloudflared_ingress_checks(
            executable=tunnel.get("executable") or "cloudflared",
            config_file=str(Path(config_file).expanduser()),
            endpoint=endpoint,
            timeout=max(10.0, float(self.settings.studio.request_timeout_seconds)),
        )
        result = {
            "ok": bool(static.get("ok")) and bool(cli.get("ok")),
            "tunnel_id": tunnel_id,
            "endpoint": endpoint,
            "server_id": server_id,
            "static": static,
            "cloudflared": cli,
        }
        await self.db.add_event(
            "connectivity.cloudflare.preflight",
            f"Cloudflare preflight {'passed' if result['ok'] else 'failed'} for {tunnel_id}",
            severity="info" if result["ok"] else "warning",
            data={"tunnel_id": tunnel_id, "ok": result["ok"], "endpoint": endpoint},
        )
        return result

    async def terminate_tunnel_for_test(self, tunnel_id: str) -> dict[str, Any]:
        if not self.settings.studio.connectivity_test_mode:
            raise ValueError("connectivity_test_mode=false")
        tunnel = await self.db.get_tunnel(tunnel_id)
        if (tunnel.get("provider") or "").lower() != "cloudflare" or not tunnel.get("managed"):
            raise ValueError("test termination is only available for managed Cloudflare tunnels")
        if (tunnel.get("desired_state") or "running") != "running":
            raise ValueError("tunnel desired_state must remain running for auto-reconnect certification")
        pid = tunnel.get("pid")
        if not pid or not await asyncio.to_thread(self._pid_matches, tunnel):
            raise ValueError("managed cloudflared process is not running")
        os.kill(int(pid), signal.SIGTERM)
        await self.db.add_event(
            "connectivity.tunnel.test_terminated",
            f"Certification terminated tunnel process {tunnel_id} pid={pid}",
            severity="warning",
            data={"tunnel_id": tunnel_id, "pid": pid},
        )
        return {"ok": True, "tunnel_id": tunnel_id, "pid": pid, "desired_state": "running"}

    async def start_tunnel(self, tunnel_id: str, *, reconnect: bool = False) -> dict[str, Any]:
        tunnel = await self.db.get_tunnel(tunnel_id)
        provider = (tunnel.get("provider") or "").lower()
        if not tunnel.get("enabled"):
            raise ValueError("tunnel is disabled")
        if provider in {"local", "direct"}:
            return await self.db.update_tunnel_runtime(tunnel_id, desired_state="running")
        if provider in {"openai", "external"}:
            raise ValueError(f"provider {provider} is externally managed; Studio cannot start it")
        if provider != "cloudflare":
            raise ValueError(f"unsupported provider: {provider}")
        if not tunnel.get("managed"):
            raise ValueError("cloudflare tunnel is inventory-only; set managed=true to control its process")
        if not bool((tunnel.get("metadata") or {}).get("path_scoped")):
            raise ValueError("managed public Cloudflare tunnel requires metadata.path_scoped=true; route only the MCP gateway path, not the Studio UI/API")
        preflight = await self.cloudflare_preflight(tunnel_id)
        if not preflight.get("ok"):
            failed = [c for section in (preflight.get("static", {}), preflight.get("cloudflared", {})) for c in section.get("checks", []) if not c.get("ok")]
            detail = "; ".join(f"{c.get('name')}: {c.get('detail')}" for c in failed[:4])
            raise ValueError(f"Cloudflare preflight failed: {detail or 'unknown failure'}")
        if await asyncio.to_thread(self._pid_matches, tunnel):
            return await self.db.update_tunnel_runtime(tunnel_id, desired_state="running")

        cmd = self.build_cloudflare_command(tunnel)
        executable = cmd[0]
        if os.path.sep not in executable:
            import shutil
            resolved = shutil.which(executable)
            if not resolved:
                raise FileNotFoundError(f"{executable} not found in PATH")
            cmd[0] = resolved
        elif not Path(executable).exists():
            raise FileNotFoundError(executable)

        log_dir = self.db.path.parent / "tunnels"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{tunnel_id}.log"
        handle = open(log_path, "ab", buffering=0)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
        except Exception:
            handle.close()
            raise
        self._processes[tunnel_id] = proc
        old = self._log_handles.pop(tunnel_id, None)
        if old:
            try:
                old.close()
            except Exception:
                pass
        self._log_handles[tunnel_id] = handle
        started_at = _utcnow()
        item = await self.db.update_tunnel_runtime(
            tunnel_id,
            desired_state="running",
            status="degraded",
            pid=proc.pid,
            command=cmd,
            log_path=str(log_path),
            started_at=started_at,
            error=None,
            increment_restart=reconnect,
        )
        if not item.get("endpoint") and not item.get("tunnel_name"):
            endpoint = await self._discover_quick_endpoint(log_path)
            if endpoint:
                item = await self.db.update_tunnel_runtime(tunnel_id, endpoint=endpoint)
        await self.db.add_event(
            "connectivity.tunnel.started",
            f"Tunnel {tunnel_id} started (pid={proc.pid})",
            data={"tunnel_id": tunnel_id, "provider": provider, "reconnect": reconnect},
        )
        return item

    async def _discover_quick_endpoint(self, log_path: Path) -> str | None:
        for _ in range(20):
            await asyncio.sleep(0.25)
            try:
                text = log_path.read_text(encoding="utf-8", errors="ignore")[-20000:]
            except Exception:
                continue
            match = TRY_CLOUDFLARE_RE.search(text)
            if match:
                return match.group(0) + "/mcp/serena-8001"
        return None

    async def stop_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        tunnel = await self.db.get_tunnel(tunnel_id)
        provider = (tunnel.get("provider") or "").lower()
        if provider in {"local", "direct"}:
            return await self.db.update_tunnel_runtime(tunnel_id, desired_state="stopped", status="stopped")
        if provider in {"openai", "external"}:
            raise ValueError(f"provider {provider} is externally managed; Studio cannot stop it")
        if provider != "cloudflare" or not tunnel.get("managed"):
            raise ValueError("tunnel is not a managed cloudflare process")

        pid = tunnel.get("pid")
        if pid and await asyncio.to_thread(self._pid_matches, tunnel):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(25):
                await asyncio.sleep(0.2)
                if not await asyncio.to_thread(self._pid_matches, tunnel):
                    break
            else:
                if await asyncio.to_thread(self._pid_matches, tunnel):
                    os.kill(pid, signal.SIGKILL)
        proc = self._processes.pop(tunnel_id, None)
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1)
            except Exception:
                pass
        handle = self._log_handles.pop(tunnel_id, None)
        if handle:
            try:
                handle.close()
            except Exception:
                pass
        item = await self.db.update_tunnel_runtime(
            tunnel_id,
            desired_state="stopped",
            status="stopped",
            pid=None,
            stopped_at=_utcnow(),
            error=None,
        )
        await self.db.add_event(
            "connectivity.tunnel.stopped", f"Tunnel {tunnel_id} stopped", data={"tunnel_id": tunnel_id}
        )
        return item

    async def restart_tunnel(self, tunnel_id: str) -> dict[str, Any]:
        tunnel = await self.db.get_tunnel(tunnel_id)
        if (tunnel.get("provider") or "").lower() != "cloudflare" or not tunnel.get("managed"):
            raise ValueError("restart is only available for managed cloudflare tunnels")
        await self.stop_tunnel(tunnel_id)
        return await self.start_tunnel(tunnel_id, reconnect=True)

    def _pid_matches(self, tunnel: dict[str, Any]) -> bool:
        pid = tunnel.get("pid")
        if not pid:
            return False
        proc = Path(f"/proc/{int(pid)}/cmdline")
        try:
            raw = proc.read_bytes()
        except Exception:
            return False
        parts = [x.decode(errors="ignore") for x in raw.split(b"\0") if x]
        if not parts or "cloudflared" not in Path(parts[0]).name.lower():
            return False
        joined = " ".join(shlex.quote(x) for x in parts)
        expected = tunnel.get("tunnel_name") or tunnel.get("origin") or tunnel.get("config_file")
        return bool(expected and str(expected) in joined)
