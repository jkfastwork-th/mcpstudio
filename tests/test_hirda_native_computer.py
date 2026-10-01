from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mcp_studio.gateway import GatewaySessionManager
from mcp_studio.hirda_native_computer import NativeComputerBackend, NativeComputerError
from mcp_studio.integrations import HirdaNativeComputerIntegrationAdapter, IntegrationManager


def _context(root: Path, session: str = "ms-1") -> dict:
    return {
        "managed_session_id": session,
        "workspace_key": "demo",
        "project_path": str(root),
        "policy": {"scope": "workspace"},
    }


@pytest.mark.asyncio
async def test_native_file_operations_stay_inside_workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    backend = NativeComputerBackend()
    result = await backend.call_tool("write_file", {"path": "note.txt", "content": "hello"}, context=_context(root))
    assert "Wrote" in result["content"][0]["text"]
    assert (root / "note.txt").read_text() == "hello"
    read = await backend.call_tool("read_file", {"path": "note.txt"}, context=_context(root))
    assert read["content"][0]["text"] == "hello"

    with pytest.raises(NativeComputerError, match="workspace_scope_violation"):
        await backend.call_tool("read_file", {"path": "../outside.txt"}, context=_context(root))
    with pytest.raises(NativeComputerError, match="workspace_path_invalid"):
        await backend.call_tool("read_file", {"path": "https://example.test/a"}, context=_context(root))


@pytest.mark.asyncio
async def test_native_process_handles_are_owned_by_starting_session(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    backend = NativeComputerBackend()
    try:
        started = await backend.call_tool("start_process", {"command": "printf native-output"}, context=_context(root, "owner"))
        pid = int(started["content"][0]["text"].split("PID ", 1)[1])
        await asyncio.sleep(0.03)
        output = await backend.call_tool("read_process_output", {"pid": pid}, context=_context(root, "owner"))
        assert output["content"][0]["text"] == "native-output"
        with pytest.raises(NativeComputerError, match="process_not_owned"):
            await backend.call_tool("read_process_output", {"pid": pid}, context=_context(root, "other"))
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_native_device_call_fails_closed_for_unknown_workspace():
    backend = NativeComputerBackend({"known": "/tmp"})
    with pytest.raises(NativeComputerError, match="workspace_not_configured"):
        await backend.call(name="read_file", arguments={"path": "file"}, workspace_key="missing", session_id="ms-1")


@pytest.mark.asyncio
async def test_native_computer_is_a_ready_hirda_backend(tmp_path: Path):
    manager = IntegrationManager()
    manager.register(HirdaNativeComputerIntegrationAdapter(), source="builtin:test")
    detail = await manager.reconcile("hirda-native-computer")
    assert detail["stage"] == "ready"
    tools = manager.backend_tools()
    names = {tool["name"] for tool in tools}
    assert "hirda__hirda_native_computer__read_file" in names
    assert "hirda__hirda_native_computer__force_terminate" in names
    assert manager.backend_permission("hirda__hirda_native_computer__write_file") == "write"
    result = await manager.call_backend_tool(
        "hirda__hirda_native_computer__write_file",
        {"path": "native.txt", "content": "ok"},
        context=_context(tmp_path),
    )
    assert "Wrote" in result["content"][0]["text"]
    assert (tmp_path / "native.txt").read_text() == "ok"
    await manager.close()


def test_native_computer_tools_are_prioritized_after_workspace_selection_controls():
    class CatalogFixture:
        def _ordered_management_tools(self):
            return [
                {"name": "mcpstudio_use_workspace"},
                {"name": "mcpstudio_create_session"},
                {"name": "mcpstudio_use_session"},
                {"name": "mcpstudio_pstack_status"},
                {"name": "mcpstudio_pstack_checkpoint"},
                {"name": "mcpstudio_lane_status"},
            ]

        def _ordered_backend_tools(self):
            return [
                {"name": "hirda__desktop_commander__read_file"},
                {"name": "hirda__hirda_native_computer__read_file"},
                {"name": "hirda__openbrowser__navigate"},
                {"name": "hirda__hirda_native_computer__start_process"},
            ]

    names = [
        tool["name"]
        for tool in GatewaySessionManager._all_local_tools(CatalogFixture())
    ]
    assert names[:3] == [
        "mcpstudio_use_workspace",
        "mcpstudio_create_session",
        "mcpstudio_use_session",
    ]
    assert names[3:5] == [
        "mcpstudio_pstack_status",
        "mcpstudio_pstack_checkpoint",
    ]
    assert names[5:7] == [
        "hirda__hirda_native_computer__read_file",
        "hirda__hirda_native_computer__start_process",
    ]
    assert names[7:] == [
        "mcpstudio_lane_status",
        "hirda__desktop_commander__read_file",
        "hirda__openbrowser__navigate",
    ]
