#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import select
import struct
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any

from mcp_studio.desktop_commander_relay import AgentError, DesktopCommanderRelay


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _computer_use_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("computer_use")
    if not isinstance(raw, dict) or not bool(raw.get("enabled", False)):
        return {}
    template = str(raw.get("websocket_url_template") or "").strip()
    if "{session_id}" not in template:
        raise AgentError("computer_use websocket_url_template must contain {session_id}")
    if not template.startswith(("ws://", "wss://")):
        raise AgentError("computer_use websocket_url_template must use ws:// or wss://")

    vnc_proxy = raw.get("vnc_proxy") if isinstance(raw.get("vnc_proxy"), dict) else {}
    normalized_vnc: dict[str, Any] = {}
    if bool(vnc_proxy.get("enabled", False)):
        host = str(vnc_proxy.get("host") or "127.0.0.1").strip()
        if host not in {"127.0.0.1", "::1"}:
            raise AgentError("computer_use vnc_proxy host must be loopback")
        port = int(vnc_proxy.get("port") or 5900)
        if not 1024 <= port <= 65535:
            raise AgentError("computer_use vnc_proxy port must be between 1024 and 65535")
        normalized_vnc = {
            "enabled": True,
            "host": host,
            "port": port,
            "connect_timeout_seconds": float(vnc_proxy.get("connect_timeout_seconds") or 3.0),
        }

    descriptor = raw.get("descriptor") if isinstance(raw.get("descriptor"), dict) else {}
    return {
        "enabled": True,
        "websocket_url_template": template,
        "descriptor": dict(descriptor),
        "vnc_proxy": normalized_vnc,
    }


def _computer_use_descriptor(config: dict[str, Any], session_id: str) -> dict[str, Any]:
    if not config.get("enabled"):
        raise AgentError("computer_use_unavailable")
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise AgentError("invalid_session_id")
    websocket_url = str(config["websocket_url_template"]).replace("{session_id}", session_id)
    advertised = config.get("descriptor") if isinstance(config.get("descriptor"), dict) else {}
    default_runtime_mode = (
        "machine-console-remote"
        if bool((config.get("vnc_proxy") or {}).get("enabled"))
        else "session-isolated-remote"
    )
    descriptor: dict[str, Any] = {
        "runtime_mode": str(advertised.get("runtime_mode") or default_runtime_mode),
        "transport": "websocket",
    }
    for key in (
        "desktop_display",
        "cdp_port",
        "gpu_mode",
        "gpu_hardware_available",
        "gpu_presentation_mode",
    ):
        if key in advertised:
            descriptor[key] = advertised[key]
    return {"descriptor": descriptor, "websocket_url": websocket_url}


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("websocket_closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_ws_frame(sock: socket.socket) -> tuple[int, bytes]:
    head = _recv_exact(sock, 2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > 8 * 1024 * 1024:
        raise AgentError("websocket_frame_too_large")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def _send_ws_frame(sock: socket.socket, payload: bytes, *, opcode: int = 0x2) -> None:
    size = len(payload)
    head = bytearray([0x80 | (opcode & 0x0F)])
    if size < 126:
        head.append(size)
    elif size <= 0xFFFF:
        head.append(126)
        head.extend(struct.pack("!H", size))
    else:
        head.append(127)
        head.extend(struct.pack("!Q", size))
    sock.sendall(bytes(head) + payload)


def _computer_use_ready(config: dict[str, Any]) -> bool:
    if not config.get("enabled"):
        return False
    proxy = config.get("vnc_proxy") if isinstance(config.get("vnc_proxy"), dict) else {}
    if not proxy.get("enabled"):
        return True
    try:
        with socket.create_connection(
            (str(proxy["host"]), int(proxy["port"])),
            timeout=float(proxy.get("connect_timeout_seconds") or 3.0),
        ):
            return True
    except OSError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "HIRDAMachineAgent/1"
    agent_version = "1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        allowed_ip = self.server.allow_client_ip  # type: ignore[attr-defined]
        if allowed_ip and self.client_address[0] != allowed_ip:
            return False
        expected = self.server.token  # type: ignore[attr-defined]
        if not expected:
            return True
        supplied = self.headers.get("Authorization", "")
        return supplied == f"Bearer {expected}"

    def _proxy_vnc_websocket(self, session_id: str) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            self._json(400, {"ok": False, "error": "invalid_session_id"})
            return
        config = self.server.computer_use  # type: ignore[attr-defined]
        proxy = config.get("vnc_proxy") if isinstance(config.get("vnc_proxy"), dict) else {}
        if not proxy.get("enabled"):
            self._json(404, {"ok": False, "error": "vnc_proxy_unavailable"})
            return
        upgrade = str(self.headers.get("Upgrade") or "").strip().lower()
        key = str(self.headers.get("Sec-WebSocket-Key") or "").strip()
        if upgrade != "websocket" or not key:
            self._json(426, {"ok": False, "error": "websocket_upgrade_required"})
            return

        try:
            upstream = socket.create_connection(
                (str(proxy["host"]), int(proxy["port"])),
                timeout=float(proxy.get("connect_timeout_seconds") or 3.0),
            )
        except OSError as exc:
            self._json(503, {"ok": False, "error": f"vnc_unavailable:{exc}"})
            return

        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        client = self.connection
        client.settimeout(None)
        upstream.settimeout(None)
        try:
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 30.0)
                if not readable:
                    continue
                if client in readable:
                    opcode, payload = _recv_ws_frame(client)
                    if opcode == 0x8:
                        try:
                            _send_ws_frame(client, payload, opcode=0x8)
                        except OSError:
                            pass
                        return
                    if opcode == 0x9:
                        _send_ws_frame(client, payload, opcode=0xA)
                    elif opcode in {0x0, 0x1, 0x2} and payload:
                        upstream.sendall(payload)
                if upstream in readable:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        return
                    _send_ws_frame(client, chunk, opcode=0x2)
        except (ConnectionError, OSError, AgentError):
            return
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        relay = self.server.relay  # type: ignore[attr-defined]
        computer_use = self.server.computer_use  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        ws_prefix = "/v1/computer/ws/"
        if path.startswith(ws_prefix):
            self._proxy_vnc_websocket(path[len(ws_prefix):])
            return
        if path not in {"/health", "/identity"}:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            tools = relay.refresh_tools()
            computer_configured = bool(computer_use.get("enabled"))
            computer_ready = _computer_use_ready(computer_use)
            runtime_mode = None
            if computer_configured:
                runtime_mode = _computer_use_descriptor(
                    computer_use, "identity"
                )["descriptor"]["runtime_mode"]
            if path == "/identity":
                capabilities = ["filesystem", "process"]
                providers: dict[str, Any] = {
                    "desktop_commander": {
                        "available": True,
                        "tool_count": len(tools),
                    }
                }
                if computer_configured:
                    capabilities.append("computer_use")
                    providers["computer_use"] = {
                        "available": True,
                        "ready": computer_ready,
                        "runtime_mode": runtime_mode,
                        "transport": "websocket",
                    }
                self._json(
                    200,
                    {
                        "schema": "hirda-machine-agent-v1",
                        "machine_id": relay.machine_id,
                        "hostname": socket.gethostname(),
                        "platform": sys.platform,
                        "architecture": platform.machine() or "unknown",
                        "agent_version": self.agent_version,
                        "capabilities": capabilities,
                        "providers": providers,
                    },
                )
                return
            self._json(
                200,
                {
                    "ok": True,
                    "machine_id": relay.machine_id,
                    "platform": sys.platform,
                    "desktop_commander": True,
                    "computer_use": computer_ready,
                    "computer_use_configured": computer_configured,
                    "computer_use_runtime_mode": runtime_mode,
                    "tool_count": len(tools),
                    "tools": [tool.get("name") for tool in tools],
                },
            )
        except Exception as exc:
            self._json(503, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path not in {"/v1/tools/call", "/v1/computer/descriptor"}:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise AgentError("invalid_content_length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise AgentError("request_must_be_object")
            session_id = str(body.get("session_id") or "")
            if not session_id:
                raise AgentError("session_id_required")
            if self.path == "/v1/computer/descriptor":
                result = _computer_use_descriptor(
                    self.server.computer_use,  # type: ignore[attr-defined]
                    session_id,
                )
                self._json(200, {"ok": True, **result})
                return
            name = str(body.get("name") or "")
            args = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
            workspace_root = str(body.get("workspace_root") or "")
            result = self.server.relay.call(name, args, workspace_root, session_id)  # type: ignore[attr-defined]
            self._json(200, {"ok": True, "result": result})
        except AgentError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file")
    parser.add_argument("--allow-client-ip", required=True)
    parser.add_argument("--config", required=True, help="JSON file containing desktop_commander_command array")
    args = parser.parse_args()
    token = ""
    if args.token_file:
        token = Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise SystemExit("token must be at least 32 characters")
    config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
    command = config.get("desktop_commander_command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise SystemExit("desktop_commander_command must be a non-empty JSON string array")
    try:
        computer_use = _computer_use_config(config)
    except AgentError as exc:
        raise SystemExit(str(exc)) from exc
    relay = DesktopCommanderRelay(command, args.machine_id)
    relay.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.token = token  # type: ignore[attr-defined]
    server.allow_client_ip = args.allow_client_ip  # type: ignore[attr-defined]
    server.relay = relay  # type: ignore[attr-defined]
    server.computer_use = computer_use  # type: ignore[attr-defined]
    print(json.dumps({"ok": True, "machine_id": args.machine_id, "listen": f"{args.host}:{args.port}"}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
