from __future__ import annotations

from mcp_studio.agent_runtimes import AgentRuntimeInventory


def _probe(output: str, provider: str):
    return AgentRuntimeInventory._cli_auth_health.__func__(
        AgentRuntimeInventory, "hermes", binary="hermes", provider=provider
    )


def test_custom_hermes_logged_out_is_non_blocking(monkeypatch):
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_run_probe",
        staticmethod(lambda binary, args, timeout=4.0: {"returncode": 0, "stdout": "custom:9router: logged out", "stderr": ""}),
    )
    result = AgentRuntimeInventory._cli_auth_health("hermes", binary="hermes", provider="custom:9router")
    assert result["status"] == "configured"
    assert result["authenticated"] is None


def test_normal_hermes_logged_out_remains_error(monkeypatch):
    monkeypatch.setattr(
        AgentRuntimeInventory,
        "_run_probe",
        staticmethod(lambda binary, args, timeout=4.0: {"returncode": 0, "stdout": "openrouter: logged out", "stderr": ""}),
    )
    result = AgentRuntimeInventory._cli_auth_health("hermes", binary="hermes", provider="openrouter")
    assert result["status"] == "error"
    assert result["authenticated"] is False
