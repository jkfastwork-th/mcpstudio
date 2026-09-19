from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, Mock, patch

from mcp_studio.computer import ComputerUseManager, _is_loopback_strict, _tcp_listener_present, _tcp_probe
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


def test_vnc_listener_check_does_not_open_a_connection() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.setblocking(False)
        port = server.getsockname()[1]

        assert _tcp_listener_present(port) is True
        with pytest.raises(BlockingIOError):
            server.accept()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_vnc_start_returns_on_listener_readiness_without_waiting_for_launcher(tmp_path) -> None:
    password_file = tmp_path / "passwd"
    password_file.write_text("test")
    root = tmp_path / "runtime"
    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_vnc_password_file=str(password_file),
        computer_runtime_dir=str(tmp_path / "computers"),
    )
    mgr = ComputerUseManager(studio, Database(tmp_path / "studio.sqlite3"))
    runtime = {
        "display": 55,
        "vnc_port": 15955,
        "root": str(root),
        "desktop_script": str(root / "desktop-start.sh"),
    }

    cleanup = Mock(returncode=0)
    cleanup.wait = AsyncMock(return_value=0)
    cleanup.kill = Mock()

    launcher = Mock(returncode=None)
    launcher.wait = AsyncMock(return_value=0)
    launcher.terminate = Mock()
    launcher.kill = Mock()
    launcher.communicate = AsyncMock(side_effect=AssertionError("launcher exit must not gate readiness"))

    with (
        patch("mcp_studio.computer._tcp_listener_present", return_value=False),
        patch("mcp_studio.computer.shutil.which", return_value="/usr/bin/tigervncserver"),
        patch("mcp_studio.computer.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[cleanup, launcher])) as spawn,
        patch.object(mgr, "_wait_listener", new=AsyncMock(return_value=True)),
    ):
        await mgr._start_vnc(runtime)

    assert spawn.await_count == 2
    launcher.communicate.assert_not_awaited()
    launcher.terminate.assert_not_called()
    launcher.kill.assert_not_called()


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
async def test_isolated_status_exposes_session_display_numbers(tmp_path) -> None:
    sessions = {
        "ms-a": {"id": "ms-a", "workspace_key": "alpha"},
        "ms-b": {"id": "ms-b", "workspace_key": "beta"},
    }

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return sessions[session_id]

    password_file = tmp_path / "passwd"
    password_file.write_text("test")
    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_password_file=str(password_file),
        computer_vnc_display_base=10,
        computer_vnc_port=15910,
        computer_cdp_port=19310,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        a = mgr._allocate_runtime(sessions["ms-a"])
        b = mgr._allocate_runtime(sessions["ms-b"])

    with (
        patch("mcp_studio.computer.shutil.which", return_value="/usr/bin/tigervncserver"),
        patch.object(mgr, "_chrome_binary", return_value="/usr/bin/chrome"),
    ):
        status = await mgr.status()

    assert status["runtime_displays"] == {
        "ms-a": a["display"],
        "ms-b": b["display"],
    }
    assert "vnc" not in status


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


@pytest.mark.asyncio
async def test_repair_runtime_keep_moves_slot_and_preserves_profile(tmp_path) -> None:
    session = {"id": "ms-a", "workspace_key": "alpha"}

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            assert session_id == "ms-a"
            return session

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=40,
        computer_vnc_port=15940,
        computer_cdp_port=19340,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        old = mgr._allocate_runtime(session)

    old_profile = old["profile_dir"]
    stop = AsyncMock(return_value=None)
    ensure = AsyncMock(return_value=None)
    with (
        patch.object(mgr, "_slot_available", return_value=True),
        patch.object(mgr, "_stop_isolated_runtime", new=stop),
        patch.object(mgr, "_ensure_isolated_runtime", new=ensure),
    ):
        result = await mgr.repair_runtime("ms-a", "keep")

    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    current = registry["sessions"]["ms-a"]
    assert current["slot"] != old["slot"]
    assert current["profile_dir"] == old_profile
    assert current["pairing_mode"] == "keep"
    assert result["re_pair"]["browser_state_preserved"] is True
    assert result["re_pair"]["previous_display"] == ":40"
    stop.assert_awaited_once()
    assert ensure.await_count >= 2


@pytest.mark.asyncio
async def test_repair_runtime_fresh_uses_new_profile_without_deleting_old(tmp_path) -> None:
    session = {"id": "ms-a", "workspace_key": "alpha"}

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return session

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=50,
        computer_vnc_port=15950,
        computer_cdp_port=19350,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        old = mgr._allocate_runtime(session)

    old_profile = Path(old["profile_dir"])
    old_profile.mkdir(parents=True, exist_ok=True)
    marker = old_profile / "keep-me.txt"
    marker.write_text("old browser state")

    with (
        patch.object(mgr, "_slot_available", return_value=True),
        patch.object(mgr, "_stop_isolated_runtime", new=AsyncMock(return_value=None)),
        patch.object(mgr, "_ensure_isolated_runtime", new=AsyncMock(return_value=None)),
    ):
        result = await mgr.repair_runtime("ms-a", "fresh")

    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    current = registry["sessions"]["ms-a"]
    assert current["slot"] != old["slot"]
    assert current["profile_dir"] != str(old_profile)
    assert current["profile_dir"].endswith("chrome-pair-1")
    assert marker.read_text() == "old browser state"
    assert result["re_pair"]["browser_state_preserved"] is False
    assert result["re_pair"]["mode"] == "fresh"


@pytest.mark.asyncio
async def test_repair_runtime_rolls_back_registry_when_new_runtime_fails(tmp_path) -> None:
    session = {"id": "ms-a", "workspace_key": "alpha"}

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return session

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=60,
        computer_vnc_port=15960,
        computer_cdp_port=19360,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        old = mgr._allocate_runtime(session)

    ensure = AsyncMock(side_effect=[RuntimeError("new runtime failed"), None])
    with (
        patch.object(mgr, "_slot_available", return_value=True),
        patch.object(mgr, "_stop_isolated_runtime", new=AsyncMock(return_value=None)),
        patch.object(mgr, "_ensure_isolated_runtime", new=ensure),
    ):
        with pytest.raises(RuntimeError, match="new runtime failed"):
            await mgr.repair_runtime("ms-a", "keep")

    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    assert registry["sessions"]["ms-a"]["slot"] == old["slot"]
    assert registry["sessions"]["ms-a"]["profile_dir"] == old["profile_dir"]
    assert ensure.await_count == 2


@pytest.mark.asyncio
async def test_repair_runtime_can_target_specific_display(tmp_path) -> None:
    session = {"id": "ms-a", "workspace_key": "alpha"}

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return session

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=70,
        computer_vnc_port=15970,
        computer_cdp_port=19370,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        old = mgr._allocate_runtime(session)

    with (
        patch.object(mgr, "_slot_available", return_value=True),
        patch.object(mgr, "_stop_isolated_runtime", new=AsyncMock(return_value=None)),
        patch.object(mgr, "_ensure_isolated_runtime", new=AsyncMock(return_value=None)),
    ):
        result = await mgr.repair_runtime("ms-a", "keep", target_display=74)

    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    current = registry["sessions"]["ms-a"]
    assert old["display"] == 70
    assert current["display"] == 74
    assert current["slot"] == 4
    assert current["vnc_port"] == 15974
    assert current["cdp_port"] == 19374
    assert result["re_pair"]["target_display"] == 74


@pytest.mark.asyncio
async def test_repair_runtime_rejects_target_assigned_to_another_session(tmp_path) -> None:
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
        computer_vnc_display_base=80,
        computer_vnc_port=15980,
        computer_cdp_port=19380,
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        mgr._allocate_runtime(sessions["ms-a"])
        other = mgr._allocate_runtime(sessions["ms-b"])

    stop = AsyncMock(return_value=None)
    with (
        patch.object(mgr, "_slot_available", return_value=True),
        patch.object(mgr, "_stop_isolated_runtime", new=stop),
    ):
        with pytest.raises(RuntimeError, match="already assigned"):
            await mgr.repair_runtime("ms-a", "keep", target_display=other["display"])

    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_targets_reports_available_and_assigned_displays(tmp_path) -> None:
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
        computer_vnc_display_base=90,
        computer_vnc_port=15990,
        computer_cdp_port=19390,
        computer_adopt_workspace="alpha",
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        current = mgr._allocate_runtime(sessions["ms-a"])
        other = mgr._allocate_runtime(sessions["ms-b"])

    with patch.object(mgr, "_slot_available", side_effect=lambda slot: slot != 3):
        info = await mgr.repair_targets("ms-a")

    by_display = {item["display"]: item for item in info["targets"]}
    assert info["current_display"] == current["display"]
    assert info["current_adopted"] is True
    assert by_display[current["display"]]["state"] == "current"
    assert by_display[other["display"]]["state"] == "assigned"
    assert by_display[93]["state"] == "occupied"
    assert by_display[94]["state"] == "available"


@pytest.mark.asyncio
async def test_adopted_runtime_can_fresh_repair_to_specific_display_without_stopping_external(tmp_path) -> None:
    session = {"id": "ms-o", "workspace_key": "oriverse"}

    class FakeDB:
        async def get_managed_session(self, session_id: str):
            return session

    studio = StudioConfig(
        computer_use_enabled=True,
        computer_session_isolation_enabled=True,
        computer_runtime_dir=str(tmp_path),
        computer_vnc_display_base=2,
        computer_vnc_port=5902,
        computer_cdp_port=9222,
        computer_adopt_workspace="oriverse",
    )
    mgr = ComputerUseManager(studio, FakeDB())

    with patch.object(mgr, "_slot_available", return_value=True):
        old = mgr._allocate_runtime(session)

    stop = AsyncMock(return_value=None)
    with (
        patch.object(mgr, "_slot_available", return_value=True),
        patch.object(mgr, "_stop_isolated_runtime", new=stop),
        patch.object(mgr, "_ensure_isolated_runtime", new=AsyncMock(return_value=None)),
    ):
        result = await mgr.repair_runtime("ms-o", "fresh", target_display=5)

    registry = __import__("json").loads((tmp_path / "registry.json").read_text())
    current = registry["sessions"]["ms-o"]
    assert old["adopted"] is True
    assert current["adopted"] is False
    assert current["display"] == 5
    assert current["slot"] == 3
    assert current["vnc_port"] == 5905
    assert current["cdp_port"] == 9225
    assert current["profile_dir"].endswith("chrome-pair-1")
    assert result["re_pair"]["previous_display"] == ":2"
    assert result["re_pair"]["target_display"] == 5
    stop.assert_not_awaited()


def test_settings_rejects_invalid_isolated_geometry() -> None:
    with pytest.raises(ValueError, match="computer_geometry"):
        StudioConfig(
            computer_use_enabled=True,
            computer_session_isolation_enabled=True,
            computer_geometry="wide",
        )
