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
