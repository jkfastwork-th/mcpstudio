from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text()
JS = (ROOT / "static" / "app.js").read_text()
CSS = (ROOT / "static" / "styles.css").read_text()


def test_settings_contains_only_preferences_not_operations():
    settings = HTML.split('<section class="view-page" data-page="settings">', 1)[1].split(
        '<section class="view-page" data-page="system">', 1
    )[0]
    assert "reference-settings-content" in settings
    assert "reference-theme-toggle" in settings
    assert "referenceLanguageSelect" in settings
    assert "reference-color-schemes" in settings
    assert "reference-settings-tabs" not in settings
    for operational_id in ("tunnelsPanel", "sloPanel", "advancedDiagnostics", "workersPanel", "operationsPanel"):
        assert operational_id not in settings


def test_system_is_a_first_class_navigation_page():
    assert 'data-view="system" href="#system"' in HTML
    system = HTML.split('<section class="view-page" data-page="system">', 1)[1].split(
        '<section class="view-page" data-page="computer">', 1
    )[0]
    for operational_id in ("tunnelsPanel", "sloPanel", "advancedDiagnostics", "workersPanel", "operationsPanel"):
        assert operational_id in system
    assert 'id="pollBtn"' in system
    assert 'id="herdrBtn"' in system


def test_color_scheme_controls_are_interactive_and_persisted():
    for scheme in ("default", "claude", "codex", "hermes"):
        assert f'data-color-scheme="{scheme}"' in HTML
    scheme_block = re.search(r'<div class="reference-color-schemes">.*?</div>', HTML, re.S)
    assert scheme_block
    assert " disabled" not in scheme_block.group(0)
    assert "function applyColorScheme" in JS
    assert "mcp-studio-color-scheme" in JS
    assert "loadColorScheme();" in JS


def test_topbar_controls_have_real_handlers():
    assert 'id="notificationBtn"' in HTML
    assert 'id="globalSearch"' in HTML
    assert "showNotifications" in JS
    assert "runGlobalSearch" in JS
    assert "notificationBtn" in JS
    assert "globalSearch" in JS


def test_agent_runtime_buttons_are_not_placeholders():
    assert "Test Connection" not in JS
    assert "View Logs" not in JS
    assert 'data-agent-check="' in JS
    assert 'data-agent-details="' in JS
    assert "checkAgentRuntime" in JS
    assert "showAgentRuntimeDetails" in JS
    assert "/api/agents/runtimes?refresh=true" in JS


def test_settings_proxy_controls_target_existing_controls():
    proxy_targets = re.findall(r'data-proxy-click="([^"]+)"', HTML)
    html_ids = set(re.findall(r'id="([^"]+)"', HTML))
    assert proxy_targets
    assert set(proxy_targets) <= html_ids


def test_primary_navigation_targets_exist():
    page_names = set(re.findall(r'data-page="([^"]+)"', HTML))
    assert {"agents", "sessions", "workspaces", "computer", "system", "settings"} <= page_names
    assert 'data-view="system" href="#system"' in HTML
    assert 'data-view="settings" href="#settings"' in HTML
    assert "system:['System','System'" in JS
    assert "settings:['Settings','Settings'" in JS


def test_menu_copy_and_thai_i18n_are_canonical():
    canonical = {
        "Dashboard": "แดชบอร์ด",
        "Capsule Lanes": "เลนแคปซูล",
        "Agents": "เอเจนต์",
        "Workspaces": "เวิร์กสเปซ",
        "Computer": "คอมพิวเตอร์",
        "System": "ระบบ",
        "Settings": "การตั้งค่า",
    }
    for english, thai in canonical.items():
        assert english in HTML
        assert f"'{english}':'{thai}'" in JS

    assert '<small>Capsules</small>' not in HTML
    assert "Runtime detection and switching will bind here next." not in HTML
    assert "data-page=\"operations\"" not in HTML
    assert "data-view=\"operations\"" not in HTML


def test_theme_switching_uses_authoritative_tokens_and_synced_controls():
    assert "function syncThemeControls()" in JS
    assert "document.documentElement.dataset.theme=currentTheme" in JS
    assert "localStorage.setItem('mcp-studio-theme',currentTheme)" in JS
    assert "hirda-visual-fidelity-version" not in JS
    assert "data-proxy-click=\"themeLightBtn\"" in HTML
    assert "data-proxy-click=\"themeDarkBtn\"" in HTML
    assert "localStorage.getItem('mcp-studio-theme')" in HTML

    assert 'html[data-theme="light"]{' in CSS
    assert 'html[data-theme="dark"]{' in CSS
    assert "--ref-page:#f4f7fb" in CSS
    assert "--ref-page:#0c1525" in CSS
    assert "background:var(--ref-page)" in CSS
    assert "background:var(--ref-surface)" in CSS
    assert ".reference-color-schemes button.selected" in CSS
    assert ".reference-theme-toggle button.active" in CSS


def test_only_runtime_safety_guards_disable_dynamic_session_actions():
    disabled_fragments = re.findall(r'<button[^>]*disabled[^>]*>', JS)
    assert disabled_fragments
    assert all("data-managed-restart" in item or "data-managed-stop" in item for item in disabled_fragments)
