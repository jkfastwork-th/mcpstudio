from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
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
    """Best-effort TCP reachability probe for connection-safe services."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _unix_socket_probe(path: Path, timeout: float = 0.3) -> bool:
    """Best-effort readiness probe for local Wayland compositor sockets."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
        return True
    except OSError:
        return False


def _tcp_listener_present(port: int) -> bool:
    """Check Linux TCP listener state without opening a connection.

    TigerVNC counts connect-and-disconnect health probes as failed/incomplete
    handshakes and can blacklist the loopback client. Reading /proc avoids
    touching the VNC protocol while still letting us detect occupied/listening
    runtime ports.
    """
    target_port = f"{int(port):04X}"
    for proc_path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = proc_path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            local_address = fields[1]
            if ":" not in local_address:
                continue
            if local_address.rsplit(":", 1)[1].upper() == target_port:
                return True
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

    @staticmethod
    def _host_gpu_available() -> bool:
        dri = Path("/dev/dri")
        try:
            if dri.is_dir():
                for node in dri.glob("renderD*"):
                    if os.access(node, os.R_OK | os.W_OK):
                        return True
        except OSError:
            pass
        nvidia = (Path("/dev/nvidia0"), Path("/dev/nvidiactl"))
        return all(node.exists() and os.access(node, os.R_OK | os.W_OK) for node in nvidia)

    def _resolved_gpu_mode(self) -> str:
        mode = str(self._studio.computer_gpu_mode or "auto").strip().lower()
        if mode == "auto":
            return "hardware" if self._host_gpu_available() else "swiftshader"
        return mode

    def _weston_binary(self) -> str | None:
        return shutil.which("weston")

    def _nested_wayland_enabled(self) -> bool:
        return bool(
            self.session_isolation_enabled
            and self._resolved_gpu_mode() == "hardware"
            and self._weston_binary()
        )

    def _gpu_presentation_mode(self) -> str:
        if self._nested_wayland_enabled():
            return "nested-wayland"
        return "x11"

    def _wayland_runtime_dir(self, runtime: dict[str, Any]) -> Path:
        return Path(runtime["root"]) / "wayland-runtime"

    @staticmethod
    def _wayland_socket_name() -> str:
        return "wayland-mcp"

    def _wayland_socket_path(self, runtime: dict[str, Any]) -> Path:
        return self._wayland_runtime_dir(runtime) / self._wayland_socket_name()

    def _weston_pid_file(self, runtime: dict[str, Any]) -> Path:
        return Path(runtime["root"]) / "weston.pid"

    def _weston_log_path(self, runtime: dict[str, Any]) -> Path:
        return Path(runtime["root"]) / "weston.log"

    def _chrome_gpu_args(self) -> list[str]:
        common = [
            "--enable-gpu",
            "--enable-unsafe-webgpu",
            "--ignore-gpu-blocklist",
        ]
        if self._resolved_gpu_mode() == "hardware":
            if self.session_isolation_enabled:
                # Xvnc does not expose DRI3, so Chromium cannot safely present
                # its Vulkan compositor directly on X11. Run Chrome as a
                # Wayland client inside a nested Weston window instead; this
                # keeps Chrome compositing on GL while Chromium's supported
                # Linux WebGPU-on-Vulkan interop path uses the host GPU.
                # Dawn's default tiered adapter limits can under-report
                # dynamic storage/textures on Intel WebGPU and make complex
                # Oriverse GBuffer pipelines invalid. Keep the real adapter
                # limits for the nested-Wayland hardware path.
                return common + [
                    "--disable-dawn-features=tiered_adapter_limits",
                    "--ozone-platform=wayland",
                ]
            return common
        return common + [
            "--enable-features=Vulkan",
            "--use-gl=angle",
            "--use-angle=vulkan",
            "--use-vulkan=swiftshader",
            "--use-webgpu-adapter=swiftshader",
            "--disable-vulkan-surface",
            "--enable-unsafe-swiftshader",
        ]

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
            or _tcp_listener_present(vnc_port)
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

    async def _wait_listener(self, port: int, timeout: float = 15.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if _tcp_listener_present(port):
                return True
            await asyncio.sleep(0.2)
        return False


    async def _wait_unix_socket(self, path: Path, timeout: float = 12.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if _unix_socket_probe(path):
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

    @staticmethod
    def _chrome_pids_for_profile(profile_dir: str) -> list[int]:
        token = f"--user-data-dir={Path(profile_dir)}"
        pids: list[int] = []
        proc_root = Path("/proc")
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            return pids
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                args = [
                    part.decode(errors="replace")
                    for part in (entry / "cmdline").read_bytes().split(b"\0")
                    if part
                ]
            except OSError:
                continue
            if token not in args:
                continue
            if any(arg.startswith("--type=") for arg in args):
                continue
            pids.append(int(entry.name))
        return sorted(pids)

    async def _stop_chrome(self, runtime: dict[str, Any]) -> None:
        profile = str(runtime.get("profile_dir") or "")
        if not profile:
            return
        pids = self._chrome_pids_for_profile(profile)
        for pid in pids:
            try:
                pgid = os.getpgid(pid)
                if pgid == pid:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                continue

        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            remaining = self._chrome_pids_for_profile(profile)
            if not remaining:
                return
            await asyncio.sleep(0.2)

        for pid in self._chrome_pids_for_profile(profile):
            try:
                pgid = os.getpgid(pid)
                if pgid == pid:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                continue

    async def _wait_listener_absent(self, port: int, timeout: float = 8.0) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not _tcp_listener_present(port):
                return True
            await asyncio.sleep(0.2)
        return not _tcp_listener_present(port)

    async def _stop_vnc(self, runtime: dict[str, Any]) -> None:
        if runtime.get("adopted"):
            return
        port = int(runtime["vnc_port"])
        if not _tcp_listener_present(port):
            return
        vncserver = shutil.which("tigervncserver") or shutil.which("vncserver")
        if not vncserver:
            raise RuntimeError("tigervncserver is unavailable; cannot stop managed VNC runtime")
        display = f":{int(runtime['display'])}"
        root = Path(runtime["root"])
        env = os.environ.copy()
        env["HOME"] = str(root / "home")
        proc = await asyncio.create_subprocess_exec(
            vncserver, "-kill", display,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if not await self._wait_listener_absent(port):
            raise RuntimeError(f"VNC :{runtime['display']} did not stop cleanly")

    async def _stop_isolated_runtime(self, runtime: dict[str, Any]) -> None:
        if runtime.get("adopted"):
            return
        await self._stop_chrome(runtime)
        await self._stop_weston(runtime)
        await self._stop_vnc(runtime)

    def _build_repair_runtime(
        self,
        session: dict[str, Any],
        old_runtime: dict[str, Any],
        mode: str,
        target_display: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        registry = self._read_registry()
        sessions: dict[str, Any] = registry.setdefault("sessions", {})
        session_id = str(session["id"])
        workspace_key = str(session.get("workspace_key") or session_id)

        used = {
            int(item.get("slot"))
            for key, item in sessions.items()
            if key != session_id
            and isinstance(item, dict)
            and str(item.get("slot", "")).isdigit()
        }
        old_slot = int(old_runtime["slot"]) if str(old_runtime.get("slot", "")).isdigit() else None
        if old_slot is not None:
            used.add(old_slot)

        base_display = int(self._studio.computer_vnc_display_base)
        if target_display is not None:
            slot = int(target_display) - base_display
            if slot < 0 or slot > 500:
                raise ValueError(
                    f"Target display :{target_display} is outside the managed Computer range"
                )
            if old_slot is not None and slot == old_slot:
                raise ValueError(f"Target display :{target_display} is already assigned to this session")
            if slot in used:
                raise RuntimeError(f"Target display :{target_display} is already assigned")
            if not self._slot_available(slot):
                raise RuntimeError(f"Target display :{target_display} is not available")
        else:
            adopt_key = (self._studio.computer_adopt_workspace or "").strip()
            slot = 1 if adopt_key else 0
            while slot in used or not self._slot_available(slot):
                slot += 1
                if slot > 500:
                    raise RuntimeError("Computer runtime slot allocation exhausted during re-pair")

        display = base_display + slot
        vnc_port = int(self._studio.computer_vnc_port) + slot
        cdp_port = int(self._studio.computer_cdp_port) + slot
        if vnc_port > 65535 or cdp_port > 65535:
            raise RuntimeError("Computer runtime port allocation exhausted during re-pair")

        root = Path(
            old_runtime.get("root")
            or self._runtime_root / f"{self._safe_runtime_key(workspace_key)}--{self._safe_runtime_key(session_id)}"
        )
        pairing_generation = int(old_runtime.get("pairing_generation") or 0) + 1
        if mode == "keep":
            profile_dir = str(old_runtime.get("profile_dir") or root / "chrome")
        else:
            profile_dir = str(root / f"chrome-pair-{pairing_generation}")

        runtime = {
            "session_id": session_id,
            "workspace_key": workspace_key,
            "slot": slot,
            "adopted": False,
            "display": display,
            "vnc_port": vnc_port,
            "cdp_port": cdp_port,
            "root": str(root),
            "profile_dir": profile_dir,
            "desktop_script": str(root / "desktop-start.sh"),
            "chrome_log": str(root / f"chrome-pair-{pairing_generation}.log"),
            "pairing_generation": pairing_generation,
            "pairing_mode": mode,
            "previous_slot": old_runtime.get("slot"),
        }
        return registry, runtime

    async def repair_targets(self, managed_session_id: str) -> dict[str, Any]:
        if not self._enabled:
            raise PermissionError("Computer Use is disabled")
        if not self.session_isolation_enabled:
            raise RuntimeError("Computer re-pair targets require session-isolated mode")

        try:
            session = await self._db.get_managed_session(managed_session_id)
        except KeyError:
            raise KeyError("Unknown managed session") from None

        async with self._runtime_lock:
            registry = self._read_registry()
            sessions: dict[str, Any] = registry.setdefault("sessions", {})
            current = sessions.get(managed_session_id)
            current_slot = None
            current_display = None
            current_adopted = False
            if isinstance(current, dict) and str(current.get("slot", "")).isdigit():
                current_slot = int(current["slot"])
                current_display = int(current["display"])
                current_adopted = bool(current.get("adopted"))

            owners: dict[int, dict[str, Any]] = {}
            for session_id, runtime in sessions.items():
                if not isinstance(runtime, dict) or not str(runtime.get("slot", "")).isdigit():
                    continue
                owners[int(runtime["slot"])] = {
                    "session_id": session_id,
                    "workspace_key": runtime.get("workspace_key"),
                }

            max_registered_slot = max(owners.keys(), default=0)
            max_slot = min(max(max_registered_slot + 4, 8), 32)
            base_display = int(self._studio.computer_vnc_display_base)
            targets: list[dict[str, Any]] = []

            for slot in range(0, max_slot + 1):
                display = base_display + slot
                owner = owners.get(slot)
                if current_slot == slot:
                    targets.append({
                        "display": display,
                        "available": False,
                        "state": "current",
                        "owner_session_id": managed_session_id,
                        "owner_workspace_key": session.get("workspace_key"),
                    })
                    continue
                if owner is not None:
                    targets.append({
                        "display": display,
                        "available": False,
                        "state": "assigned",
                        "owner_session_id": owner.get("session_id"),
                        "owner_workspace_key": owner.get("workspace_key"),
                    })
                    continue

                available = self._slot_available(slot)
                targets.append({
                    "display": display,
                    "available": bool(available),
                    "state": "available" if available else "occupied",
                    "owner_session_id": None,
                    "owner_workspace_key": None,
                })

        return {
            "session_id": managed_session_id,
            "workspace_key": session.get("workspace_key"),
            "current_display": current_display,
            "current_adopted": current_adopted,
            "targets": targets,
        }

    async def _start_vnc(self, runtime: dict[str, Any]) -> None:
        host = self._studio.computer_vnc_host
        port = int(runtime["vnc_port"])
        if _tcp_listener_present(port):
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
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        if await self._wait_listener(port, timeout=12):
            return

        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        raise RuntimeError(
            f"VNC :{runtime['display']} failed to start; see {session_home / '.vnc'}"
        )


    async def _start_weston(self, runtime: dict[str, Any]) -> None:
        if not self.session_isolation_enabled or self._resolved_gpu_mode() != "hardware":
            return
        weston = self._weston_binary()
        if not weston:
            raise RuntimeError("Weston is required for hardware GPU presentation under isolated VNC")

        runtime_dir = self._wayland_runtime_dir(runtime)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.chmod(0o700)
        socket_path = self._wayland_socket_path(runtime)
        if _unix_socket_probe(socket_path):
            return
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass

        root = Path(runtime["root"])
        session_home = root / "home"
        xauthority = session_home / ".Xauthority"
        if not xauthority.is_file():
            raise RuntimeError(f"VNC Xauthority is missing for nested Wayland: {xauthority}")

        match = re.fullmatch(r"(\d+)x(\d+)", str(self._studio.computer_geometry or ""))
        width, height = (match.group(1), match.group(2)) if match else ("1440", "900")
        log_path = self._weston_log_path(runtime)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["DISPLAY"] = f":{int(runtime['display'])}"
        env["XAUTHORITY"] = str(xauthority)
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        env["HOME"] = str(session_home)

        log_handle = log_path.open("ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                weston,
                "--backend=x11-backend.so",
                f"--socket={self._wayland_socket_name()}",
                f"--width={width}",
                f"--height={height}",
                "--idle-time=0",
                env=env,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()

        self._weston_pid_file(runtime).write_text(f"{proc.pid}\n")
        if await self._wait_unix_socket(socket_path, timeout=12):
            return

        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        raise RuntimeError(f"Weston failed to start; see {log_path}")

    def _weston_pid_for_runtime(self, runtime: dict[str, Any]) -> int | None:
        pid_file = self._weston_pid_file(runtime)
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\x00")
        except OSError:
            return None
        expected_runtime = f"XDG_RUNTIME_DIR={self._wayland_runtime_dir(runtime)}".encode()
        if b"weston" not in cmdline or b"--socket=wayland-mcp" not in cmdline:
            return None
        if expected_runtime not in environ:
            return None
        return pid

    async def _stop_weston(self, runtime: dict[str, Any]) -> None:
        pid = self._weston_pid_for_runtime(runtime)
        if pid is not None:
            try:
                pgid = os.getpgid(pid)
                if pgid == pid:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            deadline = asyncio.get_running_loop().time() + 4.0
            while asyncio.get_running_loop().time() < deadline:
                if not Path(f"/proc/{pid}").exists():
                    break
                await asyncio.sleep(0.2)
            if Path(f"/proc/{pid}").exists():
                try:
                    pgid = os.getpgid(pid)
                    if pgid == pid:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        for path in (self._weston_pid_file(runtime), self._wayland_socket_path(runtime)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

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
        if self._nested_wayland_enabled():
            env["XDG_RUNTIME_DIR"] = str(self._wayland_runtime_dir(runtime))
            env["WAYLAND_DISPLAY"] = self._wayland_socket_name()
        else:
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
                *self._chrome_gpu_args(),
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
            if not _tcp_listener_present(int(runtime["vnc_port"])):
                raise RuntimeError(f"Adopted VNC :{runtime['display']} is not running")
            if not _tcp_probe("127.0.0.1", int(runtime["cdp_port"])):
                raise RuntimeError(f"Adopted Chrome CDP {runtime['cdp_port']} is not running")
            return
        await self._start_vnc(runtime)
        await self._start_weston(runtime)
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

    async def repair_runtime(
        self,
        managed_session_id: str,
        mode: str = "keep",
        target_display: int | None = None,
    ) -> dict[str, Any]:
        if not self._enabled:
            raise PermissionError("Computer Use is disabled")
        if not self.session_isolation_enabled:
            raise RuntimeError("Computer re-pair requires session-isolated mode")
        if mode not in {"keep", "fresh"}:
            raise ValueError("Computer re-pair mode must be 'keep' or 'fresh'")

        try:
            session = await self._db.get_managed_session(managed_session_id)
        except KeyError:
            raise KeyError("Unknown managed session") from None

        initial_pair = False
        previous_display: str | None = None
        previous_slot: int | None = None
        pairing_generation = 0

        async with self._runtime_lock:
            registry = self._read_registry()
            sessions: dict[str, Any] = registry.setdefault("sessions", {})
            old_runtime = sessions.get(managed_session_id)

            if not isinstance(old_runtime, dict):
                if target_display is not None:
                    synthetic_old = {
                        "slot": -1,
                        "display": -1,
                        "root": str(
                            self._runtime_root
                            / f"{self._safe_runtime_key(str(session.get('workspace_key') or managed_session_id))}--{self._safe_runtime_key(managed_session_id)}"
                        ),
                    }
                    registry, runtime = self._build_repair_runtime(
                        session, synthetic_old, mode, target_display
                    )
                    registry.setdefault("sessions", {})[managed_session_id] = runtime
                    self._write_registry(registry)
                    try:
                        await self._ensure_isolated_runtime(session, runtime)
                    except Exception:
                        registry.setdefault("sessions", {}).pop(managed_session_id, None)
                        self._write_registry(registry)
                        raise
                else:
                    runtime = self._allocate_runtime(session)
                    await self._ensure_isolated_runtime(session, runtime)
                initial_pair = True
                pairing_generation = int(runtime.get("pairing_generation") or 0)
            else:
                if old_runtime.get("adopted") and mode == "keep":
                    raise RuntimeError(
                        "Adopted Computer runtime cannot safely preserve browser state during re-pair; choose fresh desktop"
                    )

                previous_display = f":{int(old_runtime['display'])}"
                previous_slot = int(old_runtime["slot"])
                registry, runtime = self._build_repair_runtime(
                    session, old_runtime, mode, target_display
                )
                sessions = registry.setdefault("sessions", {})

                if not old_runtime.get("adopted"):
                    await self._stop_isolated_runtime(old_runtime)

                sessions[managed_session_id] = runtime
                self._write_registry(registry)

                try:
                    await self._ensure_isolated_runtime(session, runtime)
                except Exception as exc:
                    sessions[managed_session_id] = old_runtime
                    self._write_registry(registry)
                    rollback_error: Exception | None = None
                    if not old_runtime.get("adopted"):
                        try:
                            await self._ensure_isolated_runtime(session, old_runtime)
                        except Exception as rollback_exc:
                            rollback_error = rollback_exc
                    if rollback_error is not None:
                        raise RuntimeError(
                            f"Computer re-pair failed: {exc}; rollback also failed: {rollback_error}"
                        ) from exc
                    raise

                pairing_generation = int(runtime.get("pairing_generation") or 0)

        descriptor = await self.descriptor(managed_session_id)
        descriptor["re_pair"] = {
            "initial_pair": initial_pair,
            "mode": mode,
            "browser_state_preserved": bool(mode == "keep" and not initial_pair),
            "previous_display": previous_display,
            "previous_slot": previous_slot,
            "target_display": int(descriptor["desktop_display"].lstrip(":")),
            "pairing_generation": pairing_generation,
        }
        return descriptor

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
                "gpu_mode": self._resolved_gpu_mode(),
                "gpu_hardware_available": self._host_gpu_available(),
                "gpu_presentation_mode": self._gpu_presentation_mode(),
                "weston_available": bool(self._weston_binary()),
            }

        novnc_available = bool(novnc_dir and Path(novnc_dir).is_dir())
        if self.session_isolation_enabled:
            hardware_wayland_ready = bool(
                self._resolved_gpu_mode() != "hardware" or self._weston_binary()
            )
            tools_ready = bool(
                (shutil.which("tigervncserver") or shutil.which("vncserver"))
                and self._vnc_password_file().is_file()
                and self._chrome_binary()
                and hardware_wayland_ready
            )
            transport_ready = bool(tools_ready and _is_loopback(self._studio.computer_vnc_host))
            registry = self._read_registry()
            runtime_displays = {
                str(session_id): int(runtime["display"])
                for session_id, runtime in registry.get("sessions", {}).items()
                if isinstance(runtime, dict) and str(runtime.get("display", "")).lstrip("-").isdigit()
            }
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
                "gpu_mode": self._resolved_gpu_mode(),
                "gpu_hardware_available": self._host_gpu_available(),
                "gpu_presentation_mode": self._gpu_presentation_mode(),
                "weston_available": bool(self._weston_binary()),
                "runtime_displays": runtime_displays,
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
            "gpu_mode": self._resolved_gpu_mode(),
            "gpu_hardware_available": self._host_gpu_available(),
            "gpu_presentation_mode": self._gpu_presentation_mode(),
            "weston_available": bool(self._weston_binary()),
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
            "gpu_mode": self._resolved_gpu_mode(),
            "gpu_hardware_available": self._host_gpu_available(),
            "gpu_presentation_mode": self._gpu_presentation_mode(),
            "weston_available": bool(self._weston_binary()),
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
