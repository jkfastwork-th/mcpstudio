from types import SimpleNamespace

import pytest

from mcp_studio import jev_decision


def _studio(**overrides):
    values = {
        "jev_enabled": True,
        "jev_mode": "shadow",
        "jev_api_url": "https://api.typesafe.ai/v1/systemone",
        "jev_api_key_env": "TYPESAFE_API_KEY",
        "jev_model": "jev-latest",
        "jev_timeout_seconds": 2.0,
        "jev_min_confidence": 0.90,
        "jev_suspicious_threshold": 0.90,
        "jev_fail_closed": False,
        "jev_max_state_chars": 12000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _session():
    return {
        "workspace_key": "mcp-studio",
        "project_path": "/home/alfred/mcp-studio",
    }


@pytest.mark.asyncio
async def test_disabled_plane_is_noop(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    value = await jev_decision.evaluate_tool_call(
        _studio(jev_enabled=False),
        _session(),
        "read_file",
        {"relative_path": "README.md"},
        "read",
    )
    assert value.enabled is False
    assert value.blocked is False
    assert value.action == "allow"


@pytest.mark.asyncio
async def test_missing_key_shadow_never_blocks(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    value = await jev_decision.evaluate_tool_call(
        _studio(jev_mode="shadow"),
        _session(),
        "execute_shell_command",
        {"command": "pytest -q"},
        "execute",
    )
    assert value.enabled is True
    assert value.evaluated is False
    assert value.blocked is False
    assert value.error == "missing_api_key"


@pytest.mark.asyncio
async def test_missing_key_enforce_mode_is_still_teacher_only(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    value = await jev_decision.evaluate_tool_call(
        _studio(jev_mode="enforce", jev_fail_closed=True),
        _session(),
        "execute_shell_command",
        {"command": "pytest -q"},
        "execute",
    )
    assert value.blocked is False
    assert value.code is None
    assert value.error == "missing_api_key"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return _Response(self.payload)


@pytest.mark.asyncio
async def test_high_confidence_deny_is_teacher_label_only(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _Client.payload = {
        "model": "jev-1.13.0",
        "answers": {
            "action": {
                "type": "choice",
                "choice": "deny",
                "confidence": 0.98,
                "probabilities": {"allow": 0.01, "review": 0.01, "deny": 0.98},
            },
            "suspicious": {"type": "noul", "noul": 0.97},
        },
    }
    monkeypatch.setattr(jev_decision.httpx, "AsyncClient", _Client)
    value = await jev_decision.evaluate_tool_call(
        _studio(jev_mode="enforce"),
        _session(),
        "execute_shell_command",
        {"command": "echo safe"},
        "execute",
    )
    assert value.evaluated is True
    assert value.action == "deny"
    assert value.blocked is False
    assert value.code is None
    assert value.model == "jev-1.13.0"


@pytest.mark.asyncio
async def test_shadow_observes_deny_without_blocking(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _Client.payload = {
        "answers": {
            "action": {"type": "choice", "choice": "deny", "confidence": 0.99},
            "suspicious": {"type": "noul", "noul": 0.99},
        },
    }
    monkeypatch.setattr(jev_decision.httpx, "AsyncClient", _Client)
    value = await jev_decision.evaluate_tool_call(
        _studio(jev_mode="shadow"),
        _session(),
        "execute_shell_command",
        {"command": "echo safe"},
        "execute",
    )
    assert value.evaluated is True
    assert value.action == "deny"
    assert value.blocked is False


def test_sanitize_redacts_secret_fields_and_inline_bearer():
    value = jev_decision._sanitize(
        {
            "Authorization": "Bearer abc.def",
            "nested": {
                "password": "hunter2",
                "command": "curl -H 'Authorization: Bearer abc123' https://example.test",
            },
        },
        limit=1000,
    )
    assert value["Authorization"] == "[REDACTED]"
    assert value["nested"]["password"] == "[REDACTED]"
    assert "abc123" not in value["nested"]["command"]
