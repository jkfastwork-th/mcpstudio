from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_chatgpt_callback_is_allowed_without_weakening_form_csp():
    source = (ROOT / "mcp_studio" / "main.py").read_text()
    assert "form-action 'self' https://chatgpt.com" in source
    assert "form-action *" not in source
    assert "form-action https:" not in source


def test_m5_3_2_certification_surface_present():
    source = (ROOT / "mcp_studio" / "main.py").read_text()
    assert '@app.get("/api/certification/m5.3.2")' in source
    assert '"version": "0.9.10-m6.2.5"' in source
