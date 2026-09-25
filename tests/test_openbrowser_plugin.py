from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from mcp_studio.plugin_folder import inspect_plugin_folder


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "openbrowser"
ADAPTER_PATH = PLUGIN_ROOT / "adapter.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hirda_test_openbrowser", ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openbrowser_plugin_manifest_passes_preflight():
    candidate = inspect_plugin_folder(PLUGIN_ROOT)
    assert candidate.preflight_ok is True
    assert candidate.plugin_id == "openbrowser"
    assert candidate.quarantine_reason == "trust_not_established"
    assert "execute_code" not in candidate.tools
    assert candidate.permissions["state"] == "read"
    assert candidate.permissions["input_text"] == "write"


@pytest.mark.asyncio
async def test_openbrowser_adapter_exposes_bounded_tools_only():
    module = _load_module()
    adapter = module.OpenBrowserIntegration()
    tools = await adapter.list_tools()
    names = {tool["name"] for tool in tools}

    assert names == set(adapter.manifest.tools)
    assert "execute_code" not in names
    assert "evaluate" not in names
    assert "upload_file" not in names
    assert "download_file" not in names
    assert adapter.manifest.metadata["raw_mcp_not_exposed"] is True


def test_generated_input_code_keeps_user_text_as_literal():
    module = _load_module()
    adapter = module.OpenBrowserIntegration()
    payload = "x'); __import__('os').system('false') #"
    code = adapter._build_code(
        "input_text",
        {"index": 7, "text": payload, "clear": True},
    )

    tree = compile(
        code,
        "<openbrowser-test>",
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT | ast.PyCF_ONLY_AST,
    )
    first = tree.body[0]
    assert isinstance(first, ast.Expr)
    assert isinstance(first.value, ast.Await)
    call = first.value.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.args[1], ast.Constant)
    assert call.args[1].value == payload


def test_navigation_rejects_non_http_schemes_and_credentials():
    module = _load_module()
    adapter = module.OpenBrowserIntegration()

    with pytest.raises(module.OpenBrowserAdapterError):
        adapter._build_code("navigate", {"url": "file:///etc/passwd"})
    with pytest.raises(module.OpenBrowserAdapterError):
        adapter._build_code(
            "navigate",
            {"url": "https://user:secret@example.com/"},
        )


def test_managed_sessions_receive_distinct_stable_profiles():
    module = _load_module()
    adapter = module.OpenBrowserIntegration()

    first = adapter._profile_path("session-a")
    first_again = adapter._profile_path("session-a")
    second = adapter._profile_path("session-b")

    assert first == first_again
    assert first != second
    assert first.parent == adapter.profile_root
    assert second.parent == adapter.profile_root


@pytest.mark.asyncio
async def test_call_tool_requires_managed_session_context(monkeypatch):
    module = _load_module()
    adapter = module.OpenBrowserIntegration()

    class FakeClient:
        async def execute_code(self, code: str):
            return {"code": code}

    async def fake_client_for(context):
        assert context["managed_session_id"] == "managed-1"
        return FakeClient()

    monkeypatch.setattr(adapter, "_client_for", fake_client_for)
    result = await adapter.call_tool(
        "click",
        {"index": 3},
        context={"managed_session_id": "managed-1"},
    )
    assert "await click(3)" in result["code"]

@pytest.mark.asyncio
async def test_session_process_uses_persistent_profile_and_graceful_eof(monkeypatch, tmp_path):
    module = _load_module()
    captured = {}

    class FakeStdin:
        def __init__(self):
            self.closed = False
        def write(self, data):
            captured.setdefault("writes", []).append(data)
        async def drain(self):
            return None
        def close(self):
            self.closed = True
        async def wait_closed(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = None
            self.stderr = None
            self.returncode = None
            self.terminated = False
        async def wait(self):
            self.returncode = 0
            return 0
        def terminate(self):
            self.terminated = True
        def kill(self):
            self.returncode = -9

    process = FakeProcess()

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return process

    client = module._OpenBrowserSessionClient(
        "uvx", tmp_path / "profile",
        runtime_root=tmp_path / "runtime",
        timeout_seconds=2,
        version="0.1.54",
    )

    async def fake_request(method, params=None):
        return {"serverInfo": {"name": "openbrowser", "version": "test"}}

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(client, "_request", fake_request)
    await client._ensure_started()

    assert captured["env"]["OPENBROWSER_USER_DATA_DIR"] == str(tmp_path / "profile")
    assert captured["env"]["OPENBROWSER_STORAGE_STATE"] == str(tmp_path / "profile" / "storage_state.json")

    await client.close()
    assert process.stdin.closed is True
    assert process.terminated is False

@pytest.mark.asyncio
async def test_upstream_soft_error_is_normalized_for_hirda_fallback(tmp_path, monkeypatch):
    module = _load_module()
    client = module._OpenBrowserSessionClient(
        "uvx",
        tmp_path / "profile",
        runtime_root=tmp_path / "runtime",
        timeout_seconds=2,
        version="0.1.54",
    )

    async def no_start():
        return None

    async def soft_error(method, params=None):
        assert method == "tools/call"
        return {
            "content": [{"type": "text", "text": "Error: DOM element is no longer attached"}],
            "isError": False,
        }

    monkeypatch.setattr(client, "_ensure_started", no_start)
    monkeypatch.setattr(client, "_request", soft_error)
    with pytest.raises(module.OpenBrowserAdapterError, match="openbrowser_execute_code_failed"):
        await client.execute_code("await click(1)")
