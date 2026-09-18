from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

from mcp_studio.settings import StudioConfig
from mcp_studio.db import Database


def _is_loopback(host: str) -> bool:
    """Fail closed: only 127.0.0.1, ::1, localhost are permitted."""
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
        family in {socket.AF_INET, socket.AF_INET6} and addr in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        for family, _, _, _, addr in addr_info
    )


def _is_loopback_strict(host: str) -> bool:
    """Strict helper exported for tests/config validation."""
    return (host or "").strip().lower() in {"127.0.0.1", "::1"}


def _tcp_probe(host: str, port: int, timeout: float = 0.5) -> bool:
    """Best-effort TCP reachability probe."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


_DEFAULT_NOVNC = "/usr/share/novnc"


class ComputerUseManager:
    """Shared Computer / Web VNC runtime manager (MVP).

    No DB migration and no VNC process spawning here. The host is expected to
    already run a VNC server + websockify bridge on the loopback addresses in
    config. Studio validates the managed session, optionally checks a bearer
    token, and proxies the browser's websocket to that bridge.

    The descriptor returned to the UI intentionally never exposes raw VNC
    host/port or the auth token.
    """

    def __init__(self, studio: StudioConfig, db: Database) -> None:
        self._studio = studio
        self._db = db
        self._enabled = bool(studio.computer_use_enabled)

    # ------------------------------------------------------------------
    # Public status — never leaks raw host/port
    # ------------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        if not self._enabled:
            return {
                "enabled": False,
                "configured": False,
                "loopback_only": True,
                "novnc_available": False,
                "novnc_dir": self._studio.computer_novnc_dir or _DEFAULT_NOVNC,
                "cdp_port": int(self._studio.computer_cdp_port),
                "auth_required": False,
            }

        websockify_up = _tcp_probe(
            self._studio.computer_websockify_host, int(self._studio.computer_websockify_port)
        )
        if asyncio.iscoroutine(websockify_up):
            websockify_up = await websockify_up
        novnc_dir = self._studio.computer_novnc_dir or _DEFAULT_NOVNC
        novnc_available = bool(novnc_dir and Path(novnc_dir).is_dir())

        return {
            "enabled": True,
            "configured": websockify_up and novnc_available,
            "loopback_only": True,
            "novnc_available": novnc_available,
            "novnc_dir": novnc_dir,
            "cdp_port": int(self._studio.computer_cdp_port),
            "auth_required": bool(self._studio.computer_auth_token),
            "websockify_reachable": websockify_up,
        }

    # ------------------------------------------------------------------
    # Descriptor (UI surface) — no raw VNC host/port, no token, no websocket_target
    # ------------------------------------------------------------------

    async def descriptor(self, managed_session_id: str) -> dict[str, Any]:
        if not self._enabled:
            raise PermissionError("Computer Use is disabled")

        if not _is_loopback(self._studio.computer_websockify_host):
            raise RuntimeError("websockify host is not loopback")

        try:
            session = await self._db.get_managed_session(managed_session_id)
        except KeyError:
            raise KeyError("Unknown managed session") from None

        return {
            "session": session,
            "viewer_url": "/computer/novnc/vnc.html",
            "websocket_path": f"/api/computer/vnc/ws/{managed_session_id}",
            "novnc_available": bool(
                self._studio.computer_novnc_dir and Path(self._studio.computer_novnc_dir).is_dir()
            ),
            "auth_required": bool(self._studio.computer_auth_token),
            "cdp_port": int(self._studio.computer_cdp_port),
        }

    # ------------------------------------------------------------------
    # Internal websocket target (used only by the WS proxy route)
    # ------------------------------------------------------------------

    def websocket_target(self) -> str:
        host = self._studio.computer_websockify_host
        port = int(self._studio.computer_websockify_port)
        if not _is_loopback(host):
            raise RuntimeError(f"websockify host is not loopback: {host!r}")
        return f"ws://{host}:{port}"

    # ------------------------------------------------------------------
    # Lifecycle (no-op for this MVP — no process to stop)
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        pass
