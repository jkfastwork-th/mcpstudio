from __future__ import annotations

import importlib.util
import socket
import struct
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hirda_machine_agent_test_module",
    ROOT / "scripts" / "hirda_machine_agent.py",
)
assert SPEC is not None and SPEC.loader is not None
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def test_computer_use_loopback_proxy_config_and_descriptor():
    config = agent._computer_use_config(
        {
            "computer_use": {
                "enabled": True,
                "websocket_url_template": "ws://100.85.206.7:8765/v1/computer/ws/{session_id}",
                "vnc_proxy": {"enabled": True, "host": "127.0.0.1", "port": 5900},
            }
        }
    )
    result = agent._computer_use_descriptor(config, "ms-remote-1")
    assert result["websocket_url"].endswith("/ms-remote-1")
    assert result["descriptor"]["runtime_mode"] == "machine-console-remote"
    assert result["descriptor"]["transport"] == "websocket"


def test_computer_use_proxy_rejects_non_loopback_vnc_target():
    with pytest.raises(agent.AgentError, match="must be loopback"):
        agent._computer_use_config(
            {
                "computer_use": {
                    "enabled": True,
                    "websocket_url_template": "ws://100.85.206.7:8765/v1/computer/ws/{session_id}",
                    "vnc_proxy": {"enabled": True, "host": "100.85.206.7", "port": 5900},
                }
            }
        )


def test_machine_agent_rejects_windows_style_workspace_escape(tmp_path):
    relay = agent.DesktopCommanderRelay(["desktop-commander"], "test-machine")
    with pytest.raises(agent.AgentError, match="workspace_scope_violation"):
        relay._normalize_path(tmp_path, "..\\\\..\\\\outside.txt")


def test_machine_agent_rejects_foreign_windows_drive_on_posix(tmp_path):
    if agent.os.name == "nt":
        pytest.skip("foreign Windows-drive check is POSIX-specific")
    relay = agent.DesktopCommanderRelay(["desktop-commander"], "test-machine")
    with pytest.raises(agent.AgentError, match="workspace_scope_violation"):
        relay._normalize_path(tmp_path, "C:\\\\outside\\\\secret.txt")


def test_computer_use_descriptor_rejects_invalid_session_id():
    config = agent._computer_use_config(
        {
            "computer_use": {
                "enabled": True,
                "websocket_url_template": "ws://100.85.206.7:8765/v1/computer/ws/{session_id}",
            }
        }
    )
    with pytest.raises(agent.AgentError, match="invalid_session_id"):
        agent._computer_use_descriptor(config, "../other-session")


def test_websocket_frame_decoder_unmasks_client_payload():
    left, right = socket.socketpair()
    try:
        payload = b"RFB 003.008\\n"
        mask = b"abcd"
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        frame = bytes([0x82, 0x80 | len(payload)]) + mask + masked
        left.sendall(frame)
        opcode, decoded = agent._recv_ws_frame(right)
        assert opcode == 0x2
        assert decoded == payload
    finally:
        left.close()
        right.close()


def test_websocket_frame_encoder_uses_binary_opcode():
    left, right = socket.socketpair()
    try:
        payload = b"x" * 130
        agent._send_ws_frame(left, payload)
        head = right.recv(4)
        assert head[:2] == bytes([0x82, 126])
        assert struct.unpack("!H", head[2:4])[0] == len(payload)
        assert right.recv(len(payload)) == payload
    finally:
        left.close()
        right.close()
