from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
from pathlib import Path
from typing import Any

from mcp_studio.settings import StudioConfig, _is_loopback_strict
from mcp_studio.db import Database


def _is_loopback(host: str) -> bool:
    """Fail closed: only loopback addresses are permitted."""
    normalized = (host or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"127.0.0.1", "::1", "localhost"}:
        return True
    try:
        addr_info = socket.getaddrinfo(normalized, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return all(
        family in {socket.AF_INET, socket.AF_INET6}
        and addr in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        for family, _, _, _, addr in addr_info
    )


def _tcp_probe(host: str, port: int, timeout: float = 0.5) -> bool:
    """Best-effort TCP reachability probe."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


_DEFAULT_NOVNC = "/usr/share/novnc"


class ComputerUseManager:
    """Computer Use runtime manager.

    Legacy mode proxies noVNC to one shared websockify bridge. Session-isolated
    mode owns one VNC display and Chrome profile/CDP endpoint per managed
    session. Browser WebSockets are translated directly to loopback VNC TCP,
    so isolated mode does not require a websockify process per session.
    """

    def __init__(self, studio: StudioConfig, db: Database) -> None:
        self._studio = studio
        self._db = db
        self._enabled = bool(studio.computer_use_enabled)
        self._runtime_lock = asyncio.Lock()
        self._runtime_root = Path(studio.computer_runtime_dir).expanduser()

    @property
    def session_isolation_enabled(self) -> bool:
        return bool(self._studio.computer_session_isolation_enabled)

    def _novnc_dir(self) -> str:
        return self._studio.computer_novnc_dir or _DEFAULT_NOVNC

    def _chrome_binary(self) -> str | None:
        configured = (self._studio.computer_chrome_binary or "").strip()
        if configured:
            path = Path(configured).expanduser()
            return str(path) if path.is_file() else None
        for candidate in ("google-chrome", "chromium", "chromium-browser", "chrome"):
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def _vnc_password_file(self) -> Path:
        return Path(self._studio.computer_vnc_password_file).expanduser()

    def _registry_path(self) -> Path:
        return self._runtime_root / "registry.json"

    def _read_registry(self) -> dict[str, Any]:
        path = self._registry_path()
        empty = {"version": 2, "sessions": {}}
        if not path.is_file():
            return empty
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(data, dict):
            return empty
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            return {"version": 2, "sessions": sessions}
        # v1 was experimental and keyed runtimes by workspace. Do not silently
        # reuse those entries because two managed sessions may share a workspace.
        return empty

    def _write_registry(self, data: dict[str, Any]) -> None:
        self._runtime_root.mkdir(parents=True, exist_ok=True)
        path = self._registry_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(path)

    @staticmethod
    def _safe_runtime_key(value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-.")
        return slug or "session"

    @staticmethod
    def _display_occupied(display: int) -> bool:
        return Path(f"/tmp/.X{display}-lock").exists() or Path(f"/tmp/.X11-unix/X{display}").exists()

    def _slot_available(self, slot: int) -> bool:
        display = int(self._studio.computer_vnc_display_base) + slot
        vnc_port = int(self._studio.computer_vnc_port) + slot
        cdp_port = int(self._studio.computer_cdp_port) + slot
        if vnc_port > 65535 or cdp_port > 65535:
            return False
        return not (
            self._display_occupied(display)
            or _tcp_probe(self._studio.computer_vnc_host, vnc_port, timeout=0.1)
            or _tcp_probe("127.0.0.1", cdp_port, timeout=0.1)
        )

    def _allocate_runtime(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            raise RuntimeError("Managed session id is required for Computer runtime allocation")
        workspace_key = str(session.get("workspace_key") or session_id)
        registry = self._read_registry()
        sessions: dict[str, Any] = registry.setdefault("sessions", {})
        existing = sessions.get(session_id)
        if isinstance(existing, dict):
            return existing

        used = {
            int(item.get("slot"))
            for item in sessions.values()
            if isinstance(item, dict) and str(item.get("slot", "")).isdigit()
        }
        adopt_key = (self._studio.computer_adopt_workspace or "").strip()
        adopted = bool(adopt_key and workspace_key == adopt_key and 0 not in used)
        if adopted:
            slot = 0
        else:
            slot = 1 if adopt_key else 0
            while slot in used or not self._slot_available(slot):
                slot += 1
                if slot > 500:
                    raise RuntimeError("Computer runtime slot allocation exhausted")

        display = int(self._studio.computer_vnc_display_base) + slot
        vnc_port = int(self._studio.computer_vnc_port) + slot
        cdp_port = int(self._studio.computer_cdp_port) + slot
        if vnc_port > 65535 or cdp_port > 65535:
            raise RuntimeError("Computer runtime port allocation exhausted")

        root_name = f"{self._safe_runtime_key(workspace_key)}--{self._safe_runtime_key(session_id)}"
        root = self._runtime_root / root_name
        runtime = {
            "session_id": session_id,
            "workspace_key": workspace_key,
            "slot": slot,
            "adopted": adopted,
            "display": display,
            "vnc_port": vnc_port,
            "cdp_port": cdp_port,
            "root": str(root),
            "profile_dir": str(root / "chrome"),
            "desktop_script": str(root / "desktop-start.sh"),
            "chrome_log": str(root / "chrome.log"),
        }
        sessions[session_id] = runtime
        self._write_registry(registry)
        return runtime

    async def _wait_tcp(self, host: str, port: int, timeout: float = 15.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if _tcp_probe(host, port, timeout=0.2):
                return True
            await asyncio.sleep(0.2)
        return False

    def _write_desktop_script(self, runtime: dict[str, Any]) -> Path:
        root = Path(runtime["root"])
        desktop = root / "desktop"
        session_home = root / "home"
        session_home.mkdir(parents=True, exist_ok=True)
        (session_home / ".vnc").mkdir(parents=True, exist_ok=True)
        for name in ("config", "cache", "state", "runtime"):
            path = desktop / name
            path.mkdir(parents=True, exist_ok=True)
            if name == "runtime":
                path.chmod(0o700)
        script = Path(runtime["desktop_script"])
        script.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "unset SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS WAYLAND_DISPLAY\n"
            f"export HOME={session_home}\n"
            f"export XDG_CONFIG_HOME={desktop / 'config'}\n"
            f"export XDG_CACHE_HOME={desktop / 'cache'}\n"
            f"export XDG_STATE_HOME={desktop / 'state'}\n"
            f"export XDG_RUNTIME_DIR={desktop / 'runtime'}\n"
            "export XDG_SESSION_TYPE=x11\n"
            "export DESKTOP_SESSION=xfce\n"
            "export XDG_CURRENT_DESKTOP=XFCE\n"
            "exec /usr/bin/dbus-run-session -- /usr/bin/startxfce4\n"
        )
        script.chmod(0o700)
        return script

    async def _start_vnc(self, runtime: dict[str, Any]) -> None:
        host = self._studio.computer_vnc_host
        port = int(runtime["vnc_port"])
        if _tcp_probe(host, port):
            return
        vncserver = shutil.which("tigervncserver") or shutil.which("vncserver")
        if not vncserver:
            raise RuntimeError("tigervncserver is not installed")
        password_file = self._vnc_password_file()
        if not password_file.is_file():
            raise RuntimeError(f"VNC password file is missing: {password_file}")
        script = self._write_desktop_script(runtime)
        display = f":{int(runtime['display'])}"
        session_home = Path(runtime["root"]) / "home"
        env = os.environ.copy()
        env["HOME"] = str(session_home)
        env["USER"] = env.get("USER", "alfred")
        env["LOGNAME"] = env.get("LOGNAME", env["USER"])

        # Clean only a stale allocation. A live listener above is never killed.
        cleanup = await asyncio.create_subprocess_exec(
            vncserver, "-kill", display,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=5)
        except asyncio.TimeoutError:
            cleanup.kill()
            await cleanup.wait()

        proc = await asyncio.create_subprocess_exec(
            vncserver,
            display,
            "-localhost", "yes",
            "-geometry", self._studio.computer_geometry,
            "-rfbport", str(port),
            "-rfbauth", str(password_file),
            "-SecurityTypes", "VncAuth",
            "-xstartup", str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            proc.kill()
            output, _ = await proc.communicate()
        if not await self._wait_tcp(host, port, timeout=12):
            detail = (output or b"").decode(errors="replace")[-1200:]
            raise RuntimeError(f"VNC :{runtime['display']} failed to start: {detail}")

    async def _start_chrome(self, runtime: dict[str, Any]) -> None:
        host = "127.0.0.1"
        port = int(runtime["cdp_port"])
        if _tcp_probe(host, port):
            return
        chrome = self._chrome_binary()
        if not chrome:
            raise RuntimeError("Chrome/Chromium binary is unavailable")
        profile = Path(runtime["profile_dir"])
        profile.mkdir(parents=True, exist_ok=True)
        log_path = Path(runtime["chrome_log"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["DISPLAY"] = f":{int(runtime['display'])}"
        root = Path(runtime["root"])
        env["HOME"] = str(root / "home")
        env["XDG_CONFIG_HOME"] = str(root / "desktop" / "config")
        env["XDG_CACHE_HOME"] = str(root / "desktop" / "cache")
        env["XDG_STATE_HOME"] = str(root / "desktop" / "state")
        env["XDG_RUNTIME_DIR"] = str(root / "desktop" / "runtime")
        log_handle = log_path.open("ab")
        try:
            await asyncio.create_subprocess_exec(
                chrome,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "about:blank",
                env=env,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        if not await self._wait_tcp(host, port, timeout=15):
            raise RuntimeError(f"Chrome CDP {port} failed to start; see {log_path}")

    async def _ensure_isolated_runtime(self, session: dict[str, Any], runtime: dict[str, Any]) -> None:
        host = self._studio.computer_vnc_host
        if not _is_loopback(host):
            raise RuntimeError("VNC host is not loopback")
        if runtime.get("adopted"):
            if not _tcp_probe(host, int(runtime["vnc_port"])):
                raise RuntimeError(f"Adopted VNC :{runtime['display']} is not running")
            if not _tcp_probe("127.0.0.1", int(runtime["cdp_port"])):
                raise RuntimeError(f"Adopted Chrome CDP {runtime['cdp_port']} is not running")
            return
        await self._start_vnc(runtime)
        await self._start_chrome(runtime)

    async def ensure_runtime(self, managed_session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._enabled:
            raise PermissionError("Computer Use is disabled")
        try:
            session = await self._db.get_managed_session(managed_session_id)
        except KeyError:
            raise KeyError("Unknown managed session") from None
        if not self.session_isolation_enabled:
            return session, {
                "display": int(self._studio.computer_vnc_display_base),
                "vnc_port": int(self._studio.computer_vnc_port),
                "cdp_port": int(self._studio.computer_cdp_port),
                "adopted": True,
            }
        async with self._runtime_lock:
            runtime = self._allocate_runtime(session)
            await self._ensure_isolated_runtime(session, runtime)
        return session, runtime

    async def status(self) -> dict[str, Any]:
        novnc_dir = self._novnc_dir()
        if not self._enabled:
            return {
                "enabled": False,
                "configured": False,
                "loopback_only": True,
                "novnc_available": False,
                "novnc_dir": novnc_dir,
                "cdp_port": int(self._studio.computer_cdp_port),
                "auth_required": False,
                "runtime_mode": "session-isolated" if self.session_isolation_enabled else "shared",
            }

        novnc_available = bool(novnc_dir and Path(novnc_dir).is_dir())
        if self.session_isolation_enabled:
            tools_ready = bool(
                (shutil.which("tigervncserver") or shutil.which("vncserver"))
                and self._vnc_password_file().is_file()
                and self._chrome_binary()
            )
            transport_ready = bool(tools_ready and _is_loopback(self._studio.computer_vnc_host))
            return {
                "enabled": True,
                "configured": bool(novnc_available and transport_ready),
                "loopback_only": True,
                "novnc_available": novnc_available,
                "novnc_dir": novnc_dir,
                "cdp_port": int(self._studio.computer_cdp_port),
                "auth_required": bool(self._studio.computer_auth_token),
                "transport_ready": transport_ready,
                "runtime_mode": "session-isolated",
            }

        websockify_up = _tcp_probe(
            self._studio.computer_websockify_host, int(self._studio.computer_websockify_port)
        )
        if asyncio.iscoroutine(websockify_up):
            websockify_up = await websockify_up
        return {
            "enabled": True,
            "configured": bool(websockify_up and novnc_available),
            "loopback_only": True,
            "novnc_available": novnc_available,
            "novnc_dir": novnc_dir,
            "cdp_port": int(self._studio.computer_cdp_port),
            "auth_required": bool(self._studio.computer_auth_token),
            "websockify_reachable": websockify_up,
            "transport_ready": websockify_up,
            "runtime_mode": "shared",
        }

    async def descriptor(self, managed_session_id: str) -> dict[str, Any]:
        if not self._enabled:
            raise PermissionError("Computer Use is disabled")
        if not self.session_isolation_enabled and not _is_loopback(self._studio.computer_websockify_host):
            raise RuntimeError("websockify host is not loopback")
        session, runtime = await self.ensure_runtime(managed_session_id)
        return {
            "session": session,
            "viewer_url": "/computer/novnc/vnc.html",
            "websocket_path": f"/api/computer/vnc/ws/{managed_session_id}",
            "novnc_available": bool(self._novnc_dir() and Path(self._novnc_dir()).is_dir()),
            "auth_required": bool(self._studio.computer_auth_token),
            "cdp_port": int(runtime["cdp_port"]),
            "desktop_display": f":{int(runtime['display'])}",
            "runtime_mode": "session-isolated" if self.session_isolation_enabled else "shared",
        }

    def websocket_target(self) -> str:
        """Legacy shared-mode websockify target."""
        host = self._studio.computer_websockify_host
        port = int(self._studio.computer_websockify_port)
        if not _is_loopback(host):
            raise RuntimeError(f"websockify host is not loopback: {host!r}")
        return f"ws://{host}:{port}"

    async def tcp_target(self, managed_session_id: str) -> tuple[str, int]:
        """Internal session-owned raw VNC target; never exposed to the browser."""
        if not self.session_isolation_enabled:
            raise RuntimeError("Session-isolated Computer Use is disabled")
        _, runtime = await self.ensure_runtime(managed_session_id)
        host = self._studio.computer_vnc_host
        if not _is_loopback(host):
            raise RuntimeError(f"VNC host is not loopback: {host!r}")
        return host, int(runtime["vnc_port"])

    async def stop(self) -> None:
        # Session browser profiles are durable. Child processes are owned by the
        # Studio service cgroup and are safely reprovisioned on demand after a
        # service restart. Adopted external runtimes are never stopped here.
        return None
