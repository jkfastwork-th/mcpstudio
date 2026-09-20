from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
MAIN = (ROOT / "mcp_studio" / "main.py").read_text(encoding="utf-8")


def test_reflex_is_first_class_navigation_page():
    assert 'data-view="reflex" href="#reflex"' in HTML
    assert 'data-page="reflex"' in HTML
    assert 'id="reflexMetricsPanel"' in HTML
    assert 'id="reflexWindowSelect"' in HTML
    assert 'id="reflexRefreshBtn"' in HTML
    assert "reflex:['Reflex','Reflex'" in JS


def test_reflex_ui_is_wired_to_real_metrics_api():
    assert '@app.get("/api/reflex/metrics")' in MAIN
    assert "function renderReflexMetrics" in JS
    assert "function refreshReflexMetrics" in JS
    assert "/api/reflex/metrics?window=" in JS
    assert "renderReflexMetrics(latestData)" in JS
    assert "reflexWindowSelect" in JS
    assert "reflexRefreshBtn" in JS


def test_reflex_ui_shows_teacher_boundary_and_no_raw_arguments():
    assert "JEV teacher" in HTML or "JEV teacher" in JS
    assert "static permission remains the hard boundary" in JS
    assert "JEV is teacher/shadow evidence" in JS
    assert "arguments and credentials are never shown" in JS
    assert "raw_arguments_exposed" not in JS
    assert ".reflex-action-pill" in CSS
    assert ".reflex-timeline" in CSS
    assert ".reflex-summary-grid" in CSS


def test_reflex_navigation_has_thai_label():
    assert "'Reflex':'รีเฟล็กซ์'" in JS
    assert "'DECISION ENGINE':'ระบบตัดสินใจ'" in JS
