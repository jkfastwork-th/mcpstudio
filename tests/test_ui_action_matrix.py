from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text()
JS = (ROOT / "static" / "app.js").read_text()
MAIN = (ROOT / "mcp_studio" / "main.py").read_text()


def attr(tag: str, name: str) -> str | None:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', tag)
    return match.group(1) if match else None


STATIC_DIRECT_HANDLERS = {
    "notificationBtn": "notificationBtn')?.addEventListener('click'",
    "themeLightBtn": "themeLightBtn').addEventListener('click'",
    "themeDarkBtn": "themeDarkBtn').addEventListener('click'",
    "fontDecreaseBtn": "fontDecreaseBtn').addEventListener('click'",
    "fontResetBtn": "fontResetBtn').addEventListener('click'",
    "fontIncreaseBtn": "fontIncreaseBtn').addEventListener('click'",
    "refreshBtn": "refreshBtn').addEventListener('click'",
    "reflexRefreshBtn": "reflexRefreshBtn')?.addEventListener('click'",
    "actionLoadBtn": "actionLoadBtn')?.addEventListener('click'",
    "actionEvaluateBtn": "actionEvaluateBtn')?.addEventListener('click'",
    "managedHistoryToggle": "managedHistoryToggle').addEventListener('click'",
    "newManagedSessionBtn": "newManagedSessionBtn').addEventListener('click'",
    "cancelManagedSessionBtn": "cancelManagedSessionBtn').addEventListener('click'",
    "sessionHistoryToggle": "sessionHistoryToggle').addEventListener('click'",
    "registerWorkspaceBtn": "registerWorkspaceBtn').addEventListener('click'",
    "pollBtn": "pollBtn').addEventListener('click'",
    "herdrBtn": "herdrBtn').addEventListener('click'",
    "computerRePairBtn": "target.id==='computerRePairBtn'",
    "computerReconnectBtn": "target.id==='computerReconnectBtn'",
    "computerDisconnectBtn": "target.id==='computerDisconnectBtn'",
    "computerFullscreenBtn": "target.id==='computerFullscreenBtn'",
}

STATIC_DATA_HANDLERS = {
    "data-go-view": "closest('[data-go-view]')",
    "data-copy-source": "closest('[data-copy-command],[data-copy-source]')",
    "data-copy-command": "closest('[data-copy-command],[data-copy-source]')",
    "data-proxy-click": "closest('[data-proxy-click]')",
    "data-color-scheme": "closest('button[data-color-scheme]')",
}

DYNAMIC_ACTION_HANDLERS = {
    "data-lane-state-apply": "closest('[data-lane-state-apply]')",
    "data-agent-check": "closest('[data-agent-check]')",
    "data-agent-details": "closest('[data-agent-details]')",
    "data-go-view": "closest('[data-go-view]')",
    "data-managed-resume": "closest('[data-managed-resume]')",
    "data-managed-restart": "closest('[data-managed-restart]')",
    "data-managed-stop": "closest('[data-managed-stop]')",
    "data-managed-rename": "closest('[data-managed-rename]')",
    "data-managed-history": "closest('[data-managed-history]')",
    "data-managed-rollover": "closest('[data-managed-rollover]')",
    "data-managed-history-close": "closest('[data-managed-history-close]')",
    "data-copy-command": "closest('[data-copy-command],[data-copy-source]')",
    "data-gateway-attach": "closest('[data-gateway-attach]')",
    "data-gateway-detach": "closest('[data-gateway-detach]')",
    "data-tunnel-session": "closest('[data-tunnel-session]')",
    "data-computer-session": "closest('[data-computer-session]')",
}


def test_every_static_button_has_a_real_action_contract():
    buttons = re.findall(r"<button\b[^>]*>", HTML)
    assert buttons

    unknown: list[str] = []
    for tag in buttons:
        button_id = attr(tag, "id")
        button_type = attr(tag, "type")

        if button_type == "submit":
            assert "managedSessionForm').addEventListener('submit'" in JS
            continue

        if button_id in {"appDialogCancel", "appDialogConfirm"}:
            expected = (
                "cancelBtn.addEventListener('click',onCancel)"
                if button_id == "appDialogCancel"
                else "confirmBtn.addEventListener('click',onConfirm)"
            )
            assert expected in JS
            continue

        if button_id and button_id in STATIC_DIRECT_HANDLERS:
            assert STATIC_DIRECT_HANDLERS[button_id] in JS
            continue

        action_attrs = [
            name for name in STATIC_DATA_HANDLERS if attr(tag, name) is not None
        ]
        if action_attrs:
            for name in action_attrs:
                assert STATIC_DATA_HANDLERS[name] in JS
            continue

        unknown.append(tag)

    assert not unknown, "Static buttons without a real action contract:\n" + "\n".join(unknown)


def test_every_dynamic_button_has_a_delegated_handler():
    dynamic_buttons = re.findall(r"<button\b[^>]*>", JS)
    assert dynamic_buttons

    unknown: list[str] = []
    used_actions: set[str] = set()
    for tag in dynamic_buttons:
        actions = [
            name for name in DYNAMIC_ACTION_HANDLERS
            if re.search(rf"\b{re.escape(name)}(?:=|\b)", tag)
        ]
        if not actions:
            unknown.append(tag)
            continue
        used_actions.update(actions)

    assert not unknown, "Dynamic buttons without delegated actions:\n" + "\n".join(unknown)
    for name in used_actions:
        assert DYNAMIC_ACTION_HANDLERS[name] in JS


def test_all_action_buttons_use_explicit_button_types():
    for source_name, source in (("HTML", HTML), ("JS", JS)):
        for tag in re.findall(r"<button\b[^>]*>", source):
            button_type = attr(tag, "type")
            assert button_type in {"button", "submit"}, (
                f"{source_name} action button is missing an explicit type: {tag}"
            )


def test_notifications_fetch_live_data_when_initial_cache_is_unavailable():
    assert "latestData?.alertsData||await getJson('/api/alerts?status=open&limit=20')" in JS


def test_navigation_and_go_view_targets_resolve_to_real_pages():
    pages = set(re.findall(r'data-page="([^"]+)"', HTML))
    nav_targets = set(re.findall(r'data-view="([^"]+)"', HTML))
    static_go_targets = set(re.findall(r'data-go-view="([^"]+)"', HTML))
    dynamic_go_targets = set(re.findall(r'data-go-view="([^"]+)"', JS))

    assert nav_targets <= pages
    assert static_go_targets <= pages
    assert dynamic_go_targets <= pages
    for target in nav_targets | static_go_targets | dynamic_go_targets:
        assert re.search(rf"\b{re.escape(target)}:\[", JS), f"{target} missing from viewMeta"


def test_delegated_color_scheme_handler_is_scoped_to_buttons():
    assert "closest('button[data-color-scheme]')" in JS
    assert "closest('[data-color-scheme]')" not in JS


def test_overview_capsule_detail_opens_matching_ledger_or_session_detail():
    assert 'data-go-view="sessions" data-capsule-id="${esc(capsuleId)}"' in JS
    assert 'data-managed-session-id="${esc(activeSession.id)}"' in JS
    assert 'data-capsule-id="${esc(capsule.capsule_id)}"' in JS
    assert "function focusCapsuleLedgerItem(capsuleId)" in JS
    assert "const focused=focusCapsuleLedgerItem(go.dataset.capsuleId)" in JS
    assert "if(!focused&&go.dataset.managedSessionId)" in JS
    assert "renderManagedHistory(await getJson(" in JS


def test_proxy_controls_target_real_controls():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    targets = set(re.findall(r'data-proxy-click="([^"]+)"', HTML))
    assert targets
    assert targets <= ids


def test_attach_button_never_fails_silently():
    assert "if(!sel?.value){" in JS
    assert "Choose a project session" in JS
    assert "Select a managed project session before attaching this client transport." in JS
    assert "managed/attach" in JS


def test_button_backend_routes_exist():
    routes = {
        ("GET", "/api/agents/runtimes"),
        ("GET", "/api/action-adapter/registry"),
        ("GET", "/api/action-adapter/providers"),
        ("GET", "/api/action-adapter/providers/{provider_id}"),
        ("POST", "/api/action-adapter/providers/{provider_id}/evaluate"),
        ("POST", "/api/managed/workspaces"),
        ("POST", "/api/managed/sessions"),
        ("POST", "/api/managed/sessions/{session_id}/rename"),
        ("POST", "/api/managed/sessions/{session_id}/resume"),
        ("GET", "/api/managed/sessions/{session_id}/history"),
        ("POST", "/api/managed/sessions/{session_id}/restart"),
        ("POST", "/api/managed/sessions/{session_id}/stop"),
        ("POST", "/api/gateway/sessions/{gateway_session_id}/managed/attach"),
        ("POST", "/api/gateway/sessions/{gateway_session_id}/managed/detach"),
        ("POST", "/api/health/poll"),
        ("POST", "/api/herdr/refresh"),
        ("GET", "/api/computer/status"),
        ("GET", "/api/computer/descriptor/{managed_session_id}"),
        ("GET", "/api/computer/re-pair-targets/{managed_session_id}"),
        ("POST", "/api/computer/re-pair/{managed_session_id}"),
    }
    for method, route in routes:
        assert f'@app.{method.lower()}("{route}")' in MAIN, f"Missing backend route: {method} {route}"


def test_dashboard_load_dependencies_have_backend_routes():
    routes = {
        "/api/status",
        "/api/workers",
        "/api/work",
        "/api/sessions",
        "/api/gateway/sessions",
        "/api/managed/sessions",
        "/api/managed/workspaces",
        "/api/openai/compatibility",
        "/api/operations",
        "/api/observability",
        "/api/alerts",
        "/api/audit",
        "/api/events",
        "/api/capsules",
        "/api/agents/runtimes",
    }
    for route in routes:
        assert f'@app.get("{route}")' in MAIN
