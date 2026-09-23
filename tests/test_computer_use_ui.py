from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_computer_monitor_gallery_contract() -> None:
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "styles.css").read_text()

    for element_id in (
        "computerSessionGrid",
        "computerViewerPanel",
        "computerSelectedSessionName",
        "computerViewer",
        "computerStatusPill",
    ):
        assert f'id="{element_id}"' in html
        assert element_id in js

    assert "data-computer-session" in js
    assert "computerMonitorSvg" in js
    assert "connectComputerView(sessionCard.dataset.computerSession)" in js
    assert "if(view==='computer') setTimeout(refreshComputerView,0);" in js
    assert "ready&&!computerUiState.selectedSessionId" in js
    assert "getJson('/api/managed/sessions')" in js
    assert "computerSessionSelect" not in html
    assert "computerConnectBtn" not in html
    assert ".computer-session-grid" in css
    assert ".computer-session-card" in css
    assert ".computer-monitor-icon" in css
    assert ".computer-viewer-panel[hidden]" in css


def test_novnc_websocket_path_resolves_to_app_root() -> None:
    js = (ROOT / "static" / "app.js").read_text()
    assert "const websocketPath=descriptor.websocket_path" in js
    assert "let path='../..'+websocketPath;" in js
    assert "let path='api/computer/vnc/ws/'" not in js

    from urllib.parse import urljoin
    viewer = "https://studio.example/computer/novnc/vnc.html"
    websocket_path = "/api/computer/vnc/ws/ms-test"
    resolved = urljoin(viewer, "../.." + websocket_path)
    assert resolved == "https://studio.example/api/computer/vnc/ws/ms-test"


def test_computer_ui_requires_vnc_password_and_uses_fragment_params() -> None:
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()
    main = (ROOT / "mcp_studio" / "main.py").read_text()

    assert 'id="computerVncPasswordInput"' in html
    assert "descriptor.runtime_mode==='session-isolated'&&!vncPassword" in js
    assert "viewerParams.set('password',vncPassword)" in js
    assert "viewerParams.set('reconnect','0')" in js
    assert "url.hash=viewerParams.toString()" in js
    assert "url.searchParams.set('password'" not in js
    assert 'request.url.path.startswith(("/computer/novnc/", "/static/"))' in main
    assert 'response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"' in main


def test_computer_ui_supports_runtime_re_pair() -> None:
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "styles.css").read_text()
    main = (ROOT / "mcp_studio" / "main.py").read_text()

    assert 'id="computerRePairBtn"' in html
    assert "async function repairComputerView()" in js
    assert "type:'select'" in js
    assert "Keep browser session" in js
    assert "Fresh desktop" in js
    assert "getJson('/api/computer/re-pair-targets/'" in js
    assert "target_display" in js
    assert "Auto · next available desktop" in js
    assert "current_adopted" in js
    assert "runtime_displays" in js
    assert "'VNC :'+runtimeDisplay" in js
    assert "descriptor.desktop_display" in js
    assert "computer-session-runtime" in js
    assert "sendJson(" in js and "'/api/computer/re-pair/'" in js
    assert "target.id==='computerRePairBtn'" in js
    assert ".dialog-field select" in css
    assert '@app.get("/api/computer/re-pair-targets/{managed_session_id}")' in main
    assert '@app.post("/api/computer/re-pair/{managed_session_id}")' in main


def test_computer_ui_surfaces_permissions_and_isolated_transport() -> None:
    js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "styles.css").read_text()
    assert "computer-session-permissions" in js
    assert "permissionBadge('write','W')" in js
    assert "status.runtime_mode==='session-isolated'?status.transport_ready" in js
    assert ".computer-permission-badge.allowed" in css
    assert ".computer-permission-badge.blocked" in css
    assert "if(!status.websockify_reachable)" not in js
    assert "if(!computerStatus.websockify_reachable)" not in js


def test_computer_legacy_ui_uses_mode_aware_transport() -> None:
    js = (ROOT / "static" / "app.computer.js").read_text()
    assert "computer.runtime_mode === 'session-isolated' ? computer.transport_ready : computer.websockify_reachable" in js
    assert "computerStatus.runtime_mode === 'session-isolated' ? computerStatus.transport_ready : computerStatus.websockify_reachable" in js
    assert "if(!computerStatus.websockify_reachable)" not in js
