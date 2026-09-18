from pathlib import Path
import pytest
import yaml
from mcp_studio.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, **studio_overrides):
    data = {
        "studio": {
            "production_mode": True,
            "gateway_enabled": True,
            "gateway_auth_mode": "bearer",
            "gateway_session_enabled": True,
            "gateway_session_auto_reconnect": True,
            "gateway_session_test_mode": False,
            "resilience_test_mode": False,
            "connectivity_test_mode": False,
            "oauth_enabled": True,
            "oauth_issuer": "https://serena.example.test",
            "oauth_resource_url": "https://serena.example.test/mcp/serena-8001",
            "oauth_resource_server_id": "serena-8001",
            "oauth_require_pkce": True,
        },
        "servers": [{"id": "serena-8001", "name": "Serena", "url": "http://127.0.0.1:8001/mcp", "enabled": True}],
        "tunnels": [],
    }
    data["studio"].update(studio_overrides)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_production_config_loads(tmp_path):
    s = load_settings(_config(tmp_path))
    assert s.studio.production_mode is True


@pytest.mark.parametrize("flag", ["gateway_session_test_mode", "resilience_test_mode", "connectivity_test_mode"])
def test_production_rejects_test_modes(tmp_path, flag):
    with pytest.raises(ValueError, match="test modes disabled"):
        load_settings(_config(tmp_path, **{flag: True}))


def test_production_rejects_disabled_oauth(tmp_path):
    with pytest.raises(ValueError, match="oauth_enabled=true"):
        load_settings(_config(tmp_path, oauth_enabled=False))


def test_fault_routes_are_hidden_in_production_source():
    source = (ROOT / "mcp_studio" / "main.py").read_text()
    assert source.count("if settings.studio.production_mode:") >= 4
    assert 'raise HTTPException(status_code=404, detail="Not found")' in source


def test_production_scripts_and_units_exist():
    for rel in [
        "scripts/run-prod.sh",
        "scripts/production-preflight.py",
        "scripts/install-production.sh",
        "scripts/certify-m6-production.sh",
        "systemd/mcp-studio.service",
        "systemd/mcp-studio-backup.timer",
        "systemd/mcp-studio-restore-drill.service",
        "systemd/mcp-studio-restore-drill.timer",
        "scripts/restore-drill.py",
        "scripts/certify-m6.1-operations.sh",
        "scripts/certify-m6.2-observability.sh",
    ]:
        assert (ROOT / rel).exists(), rel


def test_production_rejects_disabled_observability(tmp_path):
    with pytest.raises(ValueError, match="observability_enabled=true"):
        load_settings(_config(tmp_path, observability_enabled=False))
