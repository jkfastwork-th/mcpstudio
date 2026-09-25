from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_studio.desktop_commander import (
    EXPOSED_DESKTOP_COMMANDER_TOOLS,
    DesktopCommanderBackend,
    DesktopCommanderError,
)
from mcp_studio.integrations import DesktopCommanderIntegrationAdapter, IntegrationManager
from mcp_studio.settings import StudioConfig


def _fake_desktop_commander(tmp_path: Path) -> Path:
    tool_names = list(EXPOSED_DESKTOP_COMMANDER_TOOLS)
    script = tmp_path / "desktop-commander"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"TOOLS = {tool_names!r}\n"
        "for line in sys.stdin:\n"
        "    try: msg=json.loads(line)\n"
        "    except Exception: continue\n"
        "    if 'id' not in msg: continue\n"
        "    method=msg.get('method'); rid=msg['id']\n"
        "    if method=='initialize':\n"
        "        result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'fake-dc','version':'1'}}\n"
        "    elif method=='tools/list':\n"
        "        result={'tools':[{'name':n,'description':'fake '+n,'inputSchema':{'type':'object','additionalProperties':True}} for n in TOOLS]}\n"
        "    elif method=='tools/call':\n"
        "        p=msg.get('params') or {}; n=p.get('name'); a=p.get('arguments') or {}\n"
        "        if n=='start_process': text='Process started with PID 4242 (shell: /bin/bash)\\nInitial output:\\nOK'\n"
        "        else: text=json.dumps({'name':n,'arguments':a}, sort_keys=True)\n"
        "        result={'content':[{'type':'text','text':text}]}\n"
        "    else: result={}\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _context(root: Path, session_id: str = "ms-1") -> dict:
    return {
        "managed_session_id": session_id,
        "workspace_key": "demo",
        "project_path": str(root),
        "policy": {"scope": "workspace"},
    }


@pytest.mark.asyncio
async def test_backend_discovers_only_exposed_tools(tmp_path: Path):
    backend = DesktopCommanderBackend(str(_fake_desktop_commander(tmp_path)), cwd=tmp_path)
    try:
        tools = await backend.list_tools(refresh=True)
        assert {tool["name"] for tool in tools} == set(EXPOSED_DESKTOP_COMMANDER_TOOLS)
        assert backend.status()["running"] is True
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_backend_scopes_relative_paths_and_rejects_urls(tmp_path: Path):
    backend = DesktopCommanderBackend(str(_fake_desktop_commander(tmp_path)), cwd=tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        result = await backend.call_tool(
            "read_file", {"path": "notes.txt"}, context=_context(workspace)
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["arguments"]["path"] == str(workspace / "notes.txt")

        with pytest.raises(DesktopCommanderError, match="url_outside_workspace"):
            await backend.call_tool(
                "read_file",
                {"path": "https://example.test/secret"},
                context=_context(workspace),
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_backend_process_handles_are_session_owned(tmp_path: Path):
    backend = DesktopCommanderBackend(str(_fake_desktop_commander(tmp_path)), cwd=tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        await backend.call_tool(
            "start_process",
            {"command": "pwd", "timeout_ms": 1000},
            context=_context(workspace, "ms-owner"),
        )
        ok = await backend.call_tool(
            "read_process_output",
            {"pid": 4242},
            context=_context(workspace, "ms-owner"),
        )
        assert ok["content"]
        with pytest.raises(DesktopCommanderError, match="process_not_owned"):
            await backend.call_tool(
                "read_process_output",
                {"pid": 4242},
                context=_context(workspace, "ms-other"),
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_integration_exposes_hirda_prefixed_backend_catalog(tmp_path: Path):
    binary = _fake_desktop_commander(tmp_path)
    studio = StudioConfig(
        desktop_commander_backend_enabled=True,
        desktop_commander_backend_binary=str(binary),
        desktop_commander_backend_cwd=str(tmp_path),
    )
    manager = IntegrationManager()
    manager.register(
        DesktopCommanderIntegrationAdapter(studio, base_dir=tmp_path),
        source="builtin:desktop-commander",
    )
    try:
        detail = await manager.reconcile("desktop-commander")
        assert detail["stage"] == "ready"
        tools = manager.backend_tools()
        assert len(tools) == len(EXPOSED_DESKTOP_COMMANDER_TOOLS)
        names = {tool["name"] for tool in tools}
        assert "hirda__desktop_commander__read_file" in names
        assert manager.backend_permission("hirda__desktop_commander__write_file") == "write"
    finally:
        await manager.close()
