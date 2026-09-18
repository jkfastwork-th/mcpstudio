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
    assert "getJson('/api/managed/sessions')" in js
    assert "computerSessionSelect" not in html
    assert "computerConnectBtn" not in html
    assert ".computer-session-grid" in css
    assert ".computer-session-card" in css
    assert ".computer-monitor-icon" in css
    assert ".computer-viewer-panel[hidden]" in css
