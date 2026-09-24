from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp_studio.integrations import IntegrationManifest


OPENBROWSER_VERSION = "0.1.54"
_PERMISSION_MAP: dict[str, str] = {
    "navigate": "execute",
    "state": "read",
    "click": "execute",
    "input_text": "write",
    "select_dropdown": "write",
    "scroll": "execute",
    "wait": "read",
    "go_back": "execute",
    "switch_tab": "execute",
    "close_tab": "execute",
    "send_keys": "write",
}
_TAB_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


class OpenBrowserAdapterError(RuntimeError):
    pass


def _descriptor(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    permission = _PERMISSION_MAP[name]
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": permission == "read",
            "destructiveHint": False,
            "idempotentHint": name in {"state", "wait"},
            "openWorldHint": True,
        },
    }


_TOOL_CATALOG = {
    "navigate": _descriptor(
        "navigate",
        "Navigate this HIRDA browser session to an HTTP(S) URL.",
        {
            "url": {"type": "string", "minLength": 1, "maxLength": 4096},
            "new_tab": {"type": "boolean", "default": False},
        },
        ["url"],
    ),
    "state": _descriptor(
        "state",
        "Return compact page, tab, and indexed interactive-element state.",
        {
            "max_elements": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 80,
            }
        },
    ),
    "click": _descriptor(
        "click",
        "Click an indexed interactive element.",
        {"index": {"type": "integer", "minimum": 1}},
        ["index"],
    ),
    "input_text": _descriptor(
        "input_text",
        "Type text into an indexed input. The typed value is not echoed by the adapter.",
        {
            "index": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "maxLength": 20000},
            "clear": {"type": "boolean", "default": True},
        },
        ["index", "text"],
    ),
    "select_dropdown": _descriptor(
        "select_dropdown",
        "Select a dropdown option by its exact visible text.",
        {
            "index": {"type": "integer", "minimum": 1},
            "text": {"type": "string", "maxLength": 4000},
        },
        ["index", "text"],
    ),
    "scroll": _descriptor(
        "scroll",
        "Scroll the page or an indexed scroll container.",
        {
            "down": {"type": "boolean", "default": True},
            "pages": {"type": "number", "exclusiveMinimum": 0, "maximum": 10, "default": 1},
            "index": {"type": ["integer", "null"], "minimum": 1, "default": None},
        },
    ),
    "wait": _descriptor(
        "wait",
        "Wait for browser-side activity to settle.",
        {"seconds": {"type": "integer", "minimum": 0, "maximum": 30, "default": 3}},
    ),
    "go_back": _descriptor("go_back", "Go back in browser history."),
    "switch_tab": _descriptor(
        "switch_tab",
        "Switch to an OpenBrowser tab using its short tab id.",
        {"tab_id": {"type": "string", "minLength": 1, "maxLength": 16}},
        ["tab_id"],
    ),
    "close_tab": _descriptor(
        "close_tab",
        "Close an OpenBrowser tab using its short tab id.",
        {"tab_id": {"type": "string", "minLength": 1, "maxLength": 16}},
        ["tab_id"],
    ),
    "send_keys": _descriptor(
        "send_keys",
        "Send keyboard keys or a shortcut to the active page.",
        {"keys": {"type": "string", "minLength": 1, "maxLength": 256}},
        ["keys"],
    ),
}


class _OpenBrowserSessionClient:
    def __init__(
        self,
        uvx_binary: str,
        profile_dir: Path,
        *,
        runtime_root: Path,
        timeout_seconds: float,
        version: str,
    ) -> None:
        self.uvx_binary = uvx_binary
        self.profile_dir = profile_dir
        self.runtime_root = runtime_root
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.version = version
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.next_id = 1
        self.server_info: dict[str, Any] = {}

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise OpenBrowserAdapterError("openbrowser_not_running")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        process.stdin.write(raw.encode("utf-8"))
        await process.stdin.drain()

    async def _read_response(self, request_id: int) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise OpenBrowserAdapterError("openbrowser_not_running")
        while True:
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(), timeout=self.timeout_seconds
                )
            except TimeoutError as exc:
                raise OpenBrowserAdapterError("openbrowser_timeout") from exc
            if not raw:
                stderr_tail = ""
                if process.stderr is not None:
                    try:
                        stderr_raw = await asyncio.wait_for(
                            process.stderr.read(), timeout=0.2
                        )
                        stderr_tail = stderr_raw.decode(
                            "utf-8", errors="replace"
                        ).strip()[-1200:]
                    except Exception:
                        stderr_tail = ""
                detail = f":{stderr_tail}" if stderr_tail else ""
                raise OpenBrowserAdapterError(
                    f"openbrowser_exited:{process.returncode}{detail}"
                )
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                raise OpenBrowserAdapterError(
                    f"openbrowser_rpc_error:{message['error']}"
                )
            result = message.get("result") or {}
            if not isinstance(result, dict):
                raise OpenBrowserAdapterError("openbrowser_invalid_result")
            return result

    async def _request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        await self._write(payload)
        return await self._read_response(request_id)

    async def _ensure_started(self) -> None:
        if self.running:
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sandbox_home = self.runtime_root / "home"
        uv_cache = self.runtime_root / "uv-cache"
        xdg_cache = self.runtime_root / "xdg-cache"
        xdg_config = self.runtime_root / "xdg-config"
        xdg_data = self.runtime_root / "xdg-data"
        for directory in (
            self.runtime_root,
            self.profile_dir,
            sandbox_home,
            uv_cache,
            xdg_cache,
            xdg_config,
            xdg_data,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        env = dict(os.environ)
        env["HOME"] = str(sandbox_home)
        env["UV_CACHE_DIR"] = str(uv_cache)
        env["XDG_CACHE_HOME"] = str(xdg_cache)
        env["XDG_CONFIG_HOME"] = str(xdg_config)
        env["XDG_DATA_HOME"] = str(xdg_data)
        env["OPENBROWSER_HEADLESS"] = "true"
        command = [
            self.uvx_binary,
            "--from",
            f"openbrowser-ai=={self.version}",
            "openbrowser-ai",
            "--mcp",
            "--headless",
            "--user-data-dir",
            str(self.profile_dir),
        ]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            initialized = await self._request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "hirda-openbrowser", "version": "1"},
                },
            )
            self.server_info = dict(initialized.get("serverInfo") or {})
            await self._write(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        except Exception:
            await self._close_unlocked()
            raise

    async def list_upstream_tools(self) -> list[dict[str, Any]]:
        async with self.lock:
            await self._ensure_started()
            page = await self._request("tools/list")
            tools = page.get("tools") or []
            return [dict(item) for item in tools if isinstance(item, dict)]

    async def execute_code(self, code: str) -> dict[str, Any]:
        async with self.lock:
            await self._ensure_started()
            result = await self._request(
                "tools/call",
                {"name": "execute_code", "arguments": {"code": code}},
            )
            if result.get("isError"):
                raise OpenBrowserAdapterError("openbrowser_execute_code_failed")
            return result

    async def _close_unlocked(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        async with self.lock:
            await self._close_unlocked()


class OpenBrowserIntegration:
    def __init__(self) -> None:
        self.version = str(
            os.getenv("HIRDA_OPENBROWSER_VERSION", OPENBROWSER_VERSION)
            or OPENBROWSER_VERSION
        ).strip()
        requested_uvx = str(os.getenv("HIRDA_OPENBROWSER_UVX", "uvx") or "uvx").strip()
        requested_path = Path(requested_uvx).expanduser()
        resolved_uvx = shutil.which(requested_uvx) if "/" not in requested_uvx else None
        local_uvx = Path.home() / ".local" / "bin" / requested_uvx
        if resolved_uvx:
            self.uvx_binary = str(Path(resolved_uvx))
        elif "/" not in requested_uvx and local_uvx.is_file():
            self.uvx_binary = str(local_uvx)
        else:
            self.uvx_binary = str(requested_path)
        self.timeout_seconds = max(
            2.0, float(os.getenv("HIRDA_OPENBROWSER_TIMEOUT_SECONDS", "30") or 30)
        )
        default_profile_root = (
            Path(__file__).resolve().parents[2] / "data" / "openbrowser"
        )
        root_value = str(
            os.getenv(
                "HIRDA_OPENBROWSER_PROFILE_ROOT",
                str(default_profile_root),
            )
        ).strip()
        self.profile_root = Path(root_value).expanduser().resolve(strict=False)
        self._clients: dict[str, _OpenBrowserSessionClient] = {}
        self._clients_lock = asyncio.Lock()
        self.manifest = IntegrationManifest.from_mapping(
            {
                "id": "openbrowser",
                "name": "OpenBrowser",
                "capabilities": [
                    "browser_automation",
                    "browser_session",
                    "browser_dom",
                    "web_interaction",
                ],
                "runtime": {
                    "type": "local-stdio-mcp",
                    "authority": "hirda-gated-backend",
                    "upstream": "openbrowser-ai",
                    "upstream_version": self.version,
                },
                "tools": list(_PERMISSION_MAP),
                "permissions": dict(_PERMISSION_MAP),
                "health": {"type": "adapter_probe"},
                "routing": {
                    "mode": "browser-control-backend",
                    "preferred_lanes": ["hermes", "codex", "claude"],
                },
                "metadata": {
                    "backend_capability": True,
                    "raw_mcp_not_exposed": True,
                    "managed_session_profile_isolation": True,
                    "permission_router": "hirda",
                },
            }
        )

    def _binary_ok(self) -> bool:
        path = Path(self.uvx_binary)
        return path.is_file() and os.access(path, os.X_OK)

    def _profile_path(self, session_id: str) -> Path:
        digest = sha256(session_id.encode("utf-8")).hexdigest()[:24]
        return self.profile_root / digest

    async def _client_for(
        self, context: dict[str, Any]
    ) -> _OpenBrowserSessionClient:
        session_id = str(context.get("managed_session_id") or "").strip()
        if not session_id:
            raise OpenBrowserAdapterError("openbrowser_session_context_missing")
        async with self._clients_lock:
            client = self._clients.get(session_id)
            if client is None:
                client = _OpenBrowserSessionClient(
                    self.uvx_binary,
                    self._profile_path(session_id),
                    runtime_root=self.profile_root,
                    timeout_seconds=self.timeout_seconds,
                    version=self.version,
                )
                self._clients[session_id] = client
            return client

    async def validate(self) -> dict[str, Any]:
        ok = self._binary_ok()
        return {
            "ok": ok,
            "reason": None if ok else "openbrowser_uvx_missing",
            "uvx_binary": self.uvx_binary,
            "upstream_version": self.version,
            "profile_root": str(self.profile_root),
            "raw_mcp_not_exposed": True,
        }

    async def certify(self) -> dict[str, Any]:
        if not self._binary_ok():
            return {"ok": False, "reason": "openbrowser_uvx_missing"}
        self.profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix="cert-", dir=str(self.profile_root)
        ) as temp_dir:
            client = _OpenBrowserSessionClient(
                self.uvx_binary,
                Path(temp_dir) / "profile",
                runtime_root=self.profile_root,
                timeout_seconds=self.timeout_seconds,
                version=self.version,
            )
            try:
                tools = await client.list_upstream_tools()
                discovered = {str(item.get("name") or "") for item in tools}
                descriptor = next(
                    (item for item in tools if item.get("name") == "execute_code"),
                    {},
                )
                schema = descriptor.get("inputSchema") or {}
                properties = schema.get("properties") or {}
                contract_ok = discovered == {"execute_code"} and "code" in properties
                return {
                    "ok": contract_ok,
                    "reason": None if contract_ok else "openbrowser_tool_contract_mismatch",
                    "server_info": dict(client.server_info),
                    "upstream_tools": sorted(discovered),
                    "raw_mcp_not_exposed": True,
                    "hirda_tools": sorted(_TOOL_CATALOG),
                }
            finally:
                await client.close()

    async def status(self) -> dict[str, Any]:
        return {
            "ok": self._binary_ok(),
            "uvx_binary": self.uvx_binary,
            "upstream_version": self.version,
            "active_session_count": sum(
                1 for client in self._clients.values() if client.running
            ),
            "known_session_count": len(self._clients),
            "profile_root": str(self.profile_root),
            "raw_mcp_not_exposed": True,
            "session_isolation": "managed_session_profile",
        }

    async def list_tools(self) -> list[dict[str, Any]]:
        return [dict(_TOOL_CATALOG[name]) for name in _PERMISSION_MAP]

    @staticmethod
    def _positive_index(arguments: dict[str, Any]) -> int:
        value = arguments.get("index")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OpenBrowserAdapterError("openbrowser_index_invalid")
        return value

    @staticmethod
    def _tab_id(arguments: dict[str, Any]) -> str:
        tab_id = str(arguments.get("tab_id") or "").strip()
        if not _TAB_ID.fullmatch(tab_id):
            raise OpenBrowserAdapterError("openbrowser_tab_id_invalid")
        return tab_id

    @staticmethod
    def _url(arguments: dict[str, Any]) -> str:
        url = str(arguments.get("url") or "").strip()
        if len(url) > 4096:
            raise OpenBrowserAdapterError("openbrowser_url_too_long")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise OpenBrowserAdapterError("openbrowser_url_scheme_not_allowed")
        if parsed.username or parsed.password:
            raise OpenBrowserAdapterError("openbrowser_url_credentials_not_allowed")
        return url

    def _build_code(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "navigate":
            url = self._url(arguments)
            new_tab = bool(arguments.get("new_tab", False))
            return (
                f"await navigate({url!r}, new_tab={new_tab!r})\n"
                "state = await browser.get_browser_state_summary()\n"
                "print(json.dumps({'ok': True, 'url': state.url, 'title': state.title}, "
                "ensure_ascii=False, default=str))"
            )
        if name == "state":
            raw_max = arguments.get("max_elements", 80)
            if isinstance(raw_max, bool) or not isinstance(raw_max, int):
                raise OpenBrowserAdapterError("openbrowser_max_elements_invalid")
            max_elements = max(1, min(raw_max, 200))
            return f"""state = await browser.get_browser_state_summary()
elements = []
for idx, el in list(state.dom_state.selector_map.items())[:{max_elements}]:
    try:
        text_value = el.get_all_children_text(max_depth=2)
    except Exception:
        text_value = ""
    elements.append({{
        "index": idx,
        "tag": getattr(el, "tag_name", None),
        "text": str(text_value or "")[:500],
        "attributes": dict(getattr(el, "attributes", {{}}) or {{}}),
    }})
tabs = [{{
    "tab_id": str(getattr(tab, "target_id", ""))[-4:],
    "url": getattr(tab, "url", None),
    "title": getattr(tab, "title", None),
}} for tab in state.tabs]
print(json.dumps({{
    "url": state.url,
    "title": state.title,
    "tabs": tabs,
    "elements": elements,
}}, ensure_ascii=False, default=str))"""
        if name == "click":
            index = self._positive_index(arguments)
            return (
                f"await click({index})\n"
                f"print(json.dumps({{'ok': True, 'action': 'click', 'index': {index}}}))"
            )
        if name == "input_text":
            index = self._positive_index(arguments)
            text_value = str(arguments.get("text") or "")
            if len(text_value) > 20000:
                raise OpenBrowserAdapterError("openbrowser_input_too_long")
            clear = bool(arguments.get("clear", True))
            return (
                f"await input_text({index}, {text_value!r}, clear={clear!r})\n"
                f"print(json.dumps({{'ok': True, 'action': 'input_text', 'index': {index}}}))"
            )
        if name == "select_dropdown":
            index = self._positive_index(arguments)
            text_value = str(arguments.get("text") or "")
            if len(text_value) > 4000:
                raise OpenBrowserAdapterError("openbrowser_dropdown_text_too_long")
            return (
                f"await select_dropdown({index}, {text_value!r})\n"
                f"print(json.dumps({{'ok': True, 'action': 'select_dropdown', 'index': {index}}}))"
            )
        if name == "scroll":
            down = bool(arguments.get("down", True))
            raw_pages = arguments.get("pages", 1)
            if isinstance(raw_pages, bool) or not isinstance(raw_pages, (int, float)):
                raise OpenBrowserAdapterError("openbrowser_scroll_pages_invalid")
            pages = float(raw_pages)
            if not 0 < pages <= 10:
                raise OpenBrowserAdapterError("openbrowser_scroll_pages_invalid")
            raw_index = arguments.get("index")
            if raw_index is None:
                index_literal = "None"
            else:
                index_literal = str(self._positive_index({"index": raw_index}))
            return (
                f"await scroll(down={down!r}, pages={pages!r}, index={index_literal})\n"
                "print(json.dumps({'ok': True, 'action': 'scroll'}))"
            )
        if name == "wait":
            raw_seconds = arguments.get("seconds", 3)
            if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, int):
                raise OpenBrowserAdapterError("openbrowser_wait_seconds_invalid")
            seconds = max(0, min(raw_seconds, 30))
            return (
                f"await wait({seconds})\n"
                f"print(json.dumps({{'ok': True, 'action': 'wait', 'seconds': {seconds}}}))"
            )
        if name == "go_back":
            return (
                "await go_back()\n"
                "state = await browser.get_browser_state_summary()\n"
                "print(json.dumps({'ok': True, 'url': state.url, 'title': state.title}, "
                "ensure_ascii=False, default=str))"
            )
        if name == "switch_tab":
            tab_id = self._tab_id(arguments)
            return (
                f"await switch({tab_id!r})\n"
                "print(json.dumps({'ok': True, 'action': 'switch_tab'}))"
            )
        if name == "close_tab":
            tab_id = self._tab_id(arguments)
            return (
                f"await close({tab_id!r})\n"
                "print(json.dumps({'ok': True, 'action': 'close_tab'}))"
            )
        if name == "send_keys":
            keys = str(arguments.get("keys") or "")
            if not keys or len(keys) > 256:
                raise OpenBrowserAdapterError("openbrowser_keys_invalid")
            return (
                f"await send_keys({keys!r})\n"
                "print(json.dumps({'ok': True, 'action': 'send_keys'}))"
            )
        raise OpenBrowserAdapterError(f"openbrowser_tool_not_exposed:{name}")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = str(name or "").strip()
        if tool_name not in _TOOL_CATALOG:
            raise OpenBrowserAdapterError(
                f"openbrowser_tool_not_exposed:{tool_name}"
            )
        code = self._build_code(tool_name, dict(arguments or {}))
        client = await self._client_for(context)
        return await client.execute_code(code)

    async def close(self) -> None:
        async with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        await asyncio.gather(*(client.close() for client in clients))
