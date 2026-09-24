from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_session_ui_surfaces_machine_health_capabilities_and_effective_policy() -> None:
    js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "styles.css").read_text()

    assert "getJson('/api/machines')" in js
    assert "function machineForSession" in js
    assert "function effectiveMachinePermissions" in js
    assert "session policy ∩ machine policy" in js
    assert "machineProviderReady(machine,'desktop_commander')" in js
    assert "machineProviderReady(machine,'computer_use')" in js
    assert "machinePermissionBadges(policy)" in js
    assert "managed-session-row machine-aware" in js
    assert "session-machine-detail" in js
    assert ".machine-health.online" in css
    assert ".machine-permission-badge.allowed" in css
    assert ".machine-permission-badge.blocked" in css
    assert ".machine-capability-badge.ready" in css
    assert ".session-machine-detail" in css


def test_computer_ui_uses_machine_policy_and_provider_readiness() -> None:
    js = (ROOT / "static" / "app.js").read_text()

    assert "computerUiState={descriptor:null,sessions:[],selectedSessionId:null,status:null,machinesData:null}" in js
    assert "effectiveMachinePermissions(s,machine)" in js
    assert "machineProviderReady(machine,'computer_use')&&permissions.execute===true" in js
    assert "data-gui-available" in js
    assert "aria-disabled" in js
    assert "Computer Use is unavailable on " in js
    assert "Machine policy blocks interactive Computer Use on " in js


def test_machine_ui_keeps_machine_summary_in_managed_sessions() -> None:
    js = (ROOT / "static" / "app.js").read_text()
    assert "machineSummary.online_count" in js
    assert "machineSummary.machine_count" in js
    assert "<span>MACHINE</span>" in js
