from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from mcp_studio.computer import ComputerUseManager, _is_loopback_strict, _tcp_probe
from mcp_studio.settings import StudioConfig
from mcp_studio.db import Database


# ------------------------------------------------------------------
# Loopback validation (fail closed)
# ------------------------------------------------------------------


def test_settings_rejects_non_loopback_vnc_host() -> None:
    with pytest.raises(ValueError, match="computer_vnc_host"):
        StudioConfig(
            computer_use_enabled=True,
            computer_vnc_host="192.168.1.100",
            computer_websockify_host="127.0.0.1",
        )


def test_settings_rejects_non_loopback_websockify_host() -> None:
    with pytest.raises(ValueError, match="computer_websockify_host"):
        StudioConfig(
            computer_use_enabled=True,
            computer_vnc_host="127.0.0.1",
            computer_websockify_host="0.0.0.0",
        )


def test_settings_allows_loopback_defaults() -> None:
    cfg = StudioConfig(computer_use_enabled=True)
    assert cfg.computer_vnc_host == "127.0.0.1"
    assert cfg.computer_websockify_host == "127.0.0.1"


def test_settings_rejects_short_auth_token() -> None:
    with pytest.raises(ValueError, match="computer_auth_token"):
        StudioConfig(
            computer_use_enabled=True,
            computer_auth_token="short",
        )


def test_settings_allows_long_auth_token() -> None:
    cfg = StudioConfig(
        computer_use_enabled=True,
        computer_auth_token="a-secure-token-here",
    )
    assert cfg.computer_auth_token == "a-secure-token-here"


# ------------------------------------------------------------------
# Status shape
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_disabled_returns_correct_shape() -> None:
    studio = StudioConfig(computer_use_enabled=False)
    db = Database(":memory:")
    mgr = ComputerUseManager(studio, db)

    status = await mgr.status()

    assert status["enabled"] is False
    assert status["configured"] is False
    assert status["loopback_only"] is True
    assert status["novnc_available"] is False
    assert status["cdp_port"] == 9222
    assert status["auth_required"] is False
    assert "vnc" not in status


@pytest.mark.asyncio
async def test_status_enabled_basic_shape() -> None:
    studio = StudioConfig(computer_use_enabled=True)
    db = Database(":memory:")
    mgr = ComputerUseManager(studio, db)

    with patch("mcp_studio.computer._tcp_probe", new=AsyncMock(return_value=False)):
        status = await mgr.status()

    assert status["enabled"] is True
    assert status["configured"] is False
    assert status["loopback_only"] is True
    assert status["novnc_available"] is False
    assert status["cdp_port"] == 9222
    assert status["auth_required"] is False
    assert status["websockify_reachable"] is False


@pytest.mark.asyncio
async def test_status_enabled_configured_when_everything_ready() -> None:
    studio = StudioConfig(computer_use_enabled=True)
    db = Database(":memory:")
    mgr = ComputerUseManager(studio, db)

    os_path_exists = patch("pathlib.Path.is_dir", return_value=True)
    probe = patch("mcp_studio.computer._tcp_probe", return_value=True)
    with os_path_exists:
        with probe:
            status = await mgr.status()

    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["novnc_available"] is True
    assert status["websockify_reachable"] is True


# ------------------------------------------------------------------
# Disabled path
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_manager_raises_permission_error_on_descriptor() -> None:
    studio = StudioConfig(computer_use_enabled=False)
    db = Database(":memory:")
    mgr = ComputerUseManager(studio, db)

    with pytest.raises(PermissionError, match="disabled"):
        await mgr.descriptor("ms-does-not-matter")


@pytest.mark.asyncio
async def test_disabled_manager_status_has_no_vnc_endpoint_leak() -> None:
    studio = StudioConfig(computer_use_enabled=False)
    db = Database(":memory:")
    mgr = ComputerUseManager(studio, db)

    status = await mgr.status()

    # No raw VNC host/port should leak even in the disabled status shape.
    assert "vnc" not in status
    assert "websockify" not in status
    assert "host" not in status
    assert "port" not in status


# ------------------------------------------------------------------
# Non-loopback rejection (descriptor / websocket_target)
# ------------------------------------------------------------------



@pytest.mark.asyncio
async def test_descriptor_rejects_non_loopback_websockify() -> None:
    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return {"id": session_id}

    studio = StudioConfig(computer_use_enabled=True)
    # Settings fail closed at construction time. Mutate after validated
    # construction to exercise the manager's defense-in-depth runtime guard.
    studio.computer_websockify_host = "10.0.0.2"
    mgr = ComputerUseManager(studio, FakeDB())

    with pytest.raises(RuntimeError, match="not loopback"):
        await mgr.descriptor("ms-ignored")


def test_websocket_target_rejects_non_loopback() -> None:
    class FakeDB:
        pass

    studio = StudioConfig(computer_use_enabled=True)
    # Bypass only construction-time validation so this test targets the
    # runtime WebSocket target guard specifically.
    studio.computer_websockify_host = "172.16.0.9"
    mgr = ComputerUseManager(studio, FakeDB())

    with pytest.raises(RuntimeError, match="not loopback"):
        mgr.websocket_target()

def test_websocket_target_returns_loopback_url() -> None:
    studio = StudioConfig(
        computer_use_enabled=True,
        computer_websockify_host="127.0.0.1",
        computer_websockify_port=6080,
    )
    db = Database(":memory:")
    mgr = ComputerUseManager(studio, db)

    assert mgr.websocket_target() == "ws://127.0.0.1:6080"


# ------------------------------------------------------------------
# Unknown session
# ------------------------------------------------------------------



@pytest.mark.asyncio
async def test_descriptor_raises_for_unknown_session() -> None:
    class MissingSessionDB:
        async def get_managed_session(self, session_id: str):
            raise KeyError("Unknown managed session")

    studio = StudioConfig(computer_use_enabled=True)
    mgr = ComputerUseManager(studio, MissingSessionDB())

    with pytest.raises(KeyError, match="Unknown managed session"):
        await mgr.descriptor("ms-unknown-session-id")


@pytest.mark.asyncio
async def test_descriptor_does_not_leak_vnc_endpoint() -> None:
    class ExistingSessionDB:
        async def get_managed_session(self, session_id: str):
            return {"id": session_id, "workspace_key": "test"}

    studio = StudioConfig(computer_use_enabled=True)
    mgr = ComputerUseManager(studio, ExistingSessionDB())
    desc = await mgr.descriptor("ms-test")

    # The descriptor is browser-facing metadata only. Raw VNC endpoints and
    # the internal upstream target must never be exposed.
    assert "vnc_host" not in desc
    assert "vnc_port" not in desc
    assert "websocket_target" not in desc
    assert "viewer_url" in desc
    assert desc["viewer_url"] == "/computer/novnc/vnc.html"
    assert "websocket_path" in desc
    assert desc["websocket_path"].startswith("/api/computer/vnc/ws/")

def test_tcp_probe_returns_false_for_unbound_port() -> None:
    assert _tcp_probe("127.0.0.1", 1, timeout=0.25) is False

# ------------------------------------------------------------------
# Per-managed-session isolation
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolated_runtime_allocates_stable_session_slots(tmp_path) -> None:
    sessions = {
        "ms-o": {"id": "ms-o", "workspace_key": "oriverse"},
        "ms-e": {"id": "ms-e", "workspace_key": "earth-616"},
        "ms-n": {"id": "ms-n", "workspace_key": "nova-oracle"},
    }

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return sessions[session_id]

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_adopt_workspace="oriverse",
        computer_vnc_display_base=2,
        computer_vnc_port=5902,
        computer_cdp_port=9222,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with (
        patch.object(mgr, "_ensure_isolated_runtime", new=AsyncMock(return_value=None)),
        patch.object(mgr, "_slot_available", return_value=True),
    ):
        o = await mgr.descriptor("ms-o")
        e = await mgr.descriptor("ms-e")
        n = await mgr.descriptor("ms-n")

    assert (o["desktop_display"], o["cdp_port"]) == (":2", 9222)
    assert (e["desktop_display"], e["cdp_port"]) == (":3", 9223)
    assert (n["desktop_display"], n["cdp_port"]) == (":4", 9224)
    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    assert registry["sessions"]["ms-o"]["adopted"] is True
    assert len({v["profile_dir"] for v in registry["sessions"].values()}) == 3


@pytest.mark.asyncio
async def test_isolated_tcp_targets_are_distinct(tmp_path) -> None:
    sessions = {
        "ms-a": {"id": "ms-a", "workspace_key": "alpha"},
        "ms-b": {"id": "ms-b", "workspace_key": "beta"},
    }

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return sessions[session_id]

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=30,
        computer_vnc_port=15930,
        computer_cdp_port=19330,
    )
    mgr = ComputerUseManager(studio, FakeDB())
    with patch.object(mgr, "_ensure_isolated_runtime", new=AsyncMock(return_value=None)),          patch.object(mgr, "_slot_available", return_value=True):
        first = await mgr.tcp_target("ms-a")
        second = await mgr.tcp_target("ms-b")
    assert first == ("127.0.0.1", 15930)
    assert second == ("127.0.0.1", 15931)


@pytest.mark.asyncio
async def test_isolated_runtime_separates_two_sessions_in_same_workspace(tmp_path) -> None:
    sessions = {
        "ms-a": {"id": "ms-a", "workspace_key": "same"},
        "ms-b": {"id": "ms-b", "workspace_key": "same"},
    }

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return sessions[session_id]

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=20,
        computer_vnc_port=15920,
        computer_cdp_port=19220,
    )
    mgr = ComputerUseManager(studio, FakeDB())
    with patch.object(mgr, "_ensure_isolated_runtime", new=AsyncMock(return_value=None)):
        a = await mgr.descriptor("ms-a")
        b = await mgr.descriptor("ms-b")
    assert a["desktop_display"] != b["desktop_display"]
    assert a["cdp_port"] != b["cdp_port"]
    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    assert set(registry["sessions"]) == {"ms-a", "ms-b"}


def test_settings_rejects_invalid_isolated_geometry() -> None:
    with pytest.raises(ValueError, match="computer_geometry"):
        StudioConfig(
            computer_use_enabled=True,
            computer_session_isolation_enabled=True,
            computer_geometry="wide",
        )
