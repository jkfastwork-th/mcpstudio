from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hirda_identity_is_user_facing_source_of_truth():
    template = (ROOT / "templates" / "index.html").read_text()
    app_js = (ROOT / "static" / "app.js").read_text()
    computer_js = (ROOT / "static" / "app.computer.js").read_text()

    assert "/static/brand/hirda-sidebar-lockup-light.webp" in template
    assert "/static/brand/hirda-sidebar-lockup-dark.webp" in template
    assert "HIRDA · Multi-Agent Orchestration" in template
    assert "HIRDA Gateway" in template
    assert "Use HIRDA Studio for" in template
    assert "HIRDA Computer desktop" in template
    assert "MCP Studio" not in template
    assert "MCP Studio" not in app_js
    assert "MCP Studio" not in computer_js


def test_hirda_m1_preserves_legacy_infrastructure_namespace():
    assert (ROOT / "mcp_studio").is_dir()
    service = (ROOT / "systemd" / "mcp-studio.service").read_text()
    assert "mcp-studio" in service
    assert (ROOT / "HIRDA_M1_IDENTITY_PRODUCT_BOUNDARY.md").is_file()
