from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
MAIN = (ROOT / "mcp_studio" / "main.py").read_text(encoding="utf-8")


def test_system_monitor_is_wired_into_existing_system_page():
    system = HTML.split('<section class="view-page" data-page="system">', 1)[1].split(
        '<section class="view-page" data-page="computer">', 1
    )[0]
    for element_id in (
        "systemMonitorSummary",
        "systemMonitorCharts",
        "systemMonitorAllocator",
        "systemMonitorProcesses",
        "systemMonitorAlerts",
    ):
        assert f'id="{element_id}"' in system

    assert '@app.get("/api/system-monitor")' in MAIN
    assert "return system_monitor.snapshot()" in MAIN
    assert "await system_monitor.start()" in MAIN
    assert "await system_monitor.stop()" in MAIN


def test_system_monitor_frontend_polls_lightweight_endpoint_only():
    assert "function renderSystemMonitor" in JS
    assert "function refreshSystemMonitor" in JS
    assert "setInterval(refreshSystemMonitor,10000)" in JS
    assert "getJson('/api/system-monitor')" in JS
    assert "renderSystemMonitor(latestData)" in JS
    assert "systemMonitorSparkline" in JS
    assert "MALLOC_ARENA_MAX" in JS


def test_system_monitor_styles_are_responsive():
    assert ".system-monitor-shell" in CSS
    assert ".system-monitor-summary" in CSS
    assert ".system-monitor-spark" in CSS
    assert ".system-monitor-process-row" in CSS
    assert "@media(max-width:820px)" in CSS
