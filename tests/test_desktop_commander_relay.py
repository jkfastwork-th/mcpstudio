from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mcp_studio.desktop_commander_relay import AgentError, DesktopCommanderRelay


CHILD = r"""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"serverInfo": {"name": "fake-dc", "version": "1"}}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": "read_file", "inputSchema": {"type": "object"}},
                {"name": "write_file", "inputSchema": {"type": "object"}},
            ]
        }
    elif method == "tools/call":
        args = request.get("params", {}).get("arguments", {})
        result = {
            "content": [
                {"type": "text", "text": json.dumps(args, sort_keys=True)}
            ]
        }
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def test_shared_relay_calls_local_desktop_commander_with_workspace_scope(tmp_path):
    workspace = tmp_path / "workspace"
    relay = DesktopCommanderRelay(
        [sys.executable, "-u", "-c", CHILD],
        "test-device",
        timeout_seconds=2.0,
    )
    try:
        result = relay.call(
            "read_file",
            {"path": "README.md"},
            str(workspace),
            "ms-1",
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["path"] == str(workspace / "README.md")
    finally:
        relay.stop()


def test_shared_relay_rejects_path_escape_before_backend_call(tmp_path):
    workspace = tmp_path / "workspace"
    relay = DesktopCommanderRelay(
        [sys.executable, "-u", "-c", CHILD],
        "test-device",
        timeout_seconds=2.0,
    )
    try:
        with pytest.raises(AgentError, match="workspace_scope_violation"):
            relay.call(
                "read_file",
                {"path": "../outside.txt"},
                str(workspace),
                "ms-1",
            )
    finally:
        relay.stop()


def test_shared_relay_times_out_instead_of_hanging(tmp_path):
    child = "import time; time.sleep(60)"
    relay = DesktopCommanderRelay(
        [sys.executable, "-u", "-c", child],
        "test-device",
        timeout_seconds=1.0,
    )
    relay.timeout_seconds = 0.05
    with pytest.raises(AgentError, match="desktop_commander_timeout:initialize"):
        relay.start()
    relay.stop()
