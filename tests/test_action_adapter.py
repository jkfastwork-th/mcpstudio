from types import SimpleNamespace

import pytest

from mcp_studio import action_adapter


def _studio(**overrides):
    values = {
        "jev_enabled": True,
        "jev_mode": "shadow",
        "jev_api_url": "https://api.typesafe.ai/v1/systemone",
        "jev_api_key_env": "TYPESAFE_API_KEY",
        "jev_model": "jev-latest",
        "jev_timeout_seconds": 2.0,
        "jev_max_state_chars": 12000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _finance_envelope():
    return {
        "schema": "hirda-action-envelope-v1",
        "request_id": "navinvestor:rebalance:001",
        "provider": "navinvestor",
        "domain": "finance",
        "goal": "Choose among already-computed portfolio actions.",
        "state": {
            "cash_pct": 18.0,
            "allocation_drift_pct": 4.2,
            "market_open": True,
        },
        "actions": [
            {
                "action_id": "navinvestor:hold",
                "operation": "hold",
                "title": "Hold current portfolio",
                "permission_class": "read",
                "arguments": {},
                "constraints": {},
                "risk": {
                    "consequential": False,
                    "reversible": True,
                    "financial": False,
                },
            },
            {
                "action_id": "navinvestor:rebalance:plan-a",
                "operation": "rebalance",
                "title": "Prepare rebalance plan A",
                "permission_class": "execute",
                "arguments": {
                    "plan_id": "plan-a",
                    "api_token": "must-never-reach-jev",
                },
                "constraints": {
                    "within_position_limits": True,
                    "within_order_value_limit": True,
                },
                "risk": {
                    "consequential": True,
                    "reversible": False,
                    "external_side_effect": True,
                    "financial": True,
                    "requires_human_approval": True,
                },
            },
        ],
        "metadata": {"test": True},
    }


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    payload = None
    request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        type(self).request = {"url": url, **kwargs}
        return _Response(type(self).payload)


def test_normalize_browser_sample_is_side_effect_free_contract():
    envelope = action_adapter.normalize_action_envelope(
        action_adapter.sample_action_envelope()
    )
    assert envelope.schema == action_adapter.ACTION_ENVELOPE_SCHEMA
    assert envelope.provider == "demo-browser"
    assert envelope.domain == "browser"
    assert len(envelope.actions) == 2
    assert envelope.metadata["executor_attached"] is False


def test_finance_uses_same_schema_without_special_execution_path():
    envelope = action_adapter.normalize_action_envelope(_finance_envelope())
    assert envelope.domain == "finance"
    candidate = envelope.actions[1]
    assert candidate.permission_class == "execute"
    assert candidate.risk.financial is True
    assert candidate.risk.consequential is True
    assert candidate.risk.requires_human_approval is True


def test_duplicate_action_ids_are_rejected():
    payload = action_adapter.sample_action_envelope()
    payload["actions"][1]["action_id"] = payload["actions"][0]["action_id"]
    with pytest.raises(action_adapter.ActionEnvelopeError, match="duplicate action_id"):
        action_adapter.normalize_action_envelope(payload)


def test_invalid_permission_class_is_rejected():
    payload = action_adapter.sample_action_envelope()
    payload["actions"][0]["permission_class"] = "financial_execute"
    with pytest.raises(action_adapter.ActionEnvelopeError, match="permission_class"):
        action_adapter.normalize_action_envelope(payload)


def test_unknown_fields_and_non_boolean_risk_are_rejected_strictly():
    payload = action_adapter.sample_action_envelope()
    payload["surprise"] = True
    with pytest.raises(action_adapter.ActionEnvelopeError, match="unsupported fields"):
        action_adapter.normalize_action_envelope(payload)

    payload = action_adapter.sample_action_envelope()
    payload["actions"][0]["risk"]["consequential"] = "false"
    with pytest.raises(action_adapter.ActionEnvelopeError, match="must be a boolean"):
        action_adapter.normalize_action_envelope(payload)


@pytest.mark.asyncio
async def test_missing_key_is_advisory_failure_and_never_execution_authority(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = await action_adapter.evaluate_action_envelope(
        _studio(),
        _finance_envelope(),
    )
    assert result.evaluated is False
    assert result.error == "missing_api_key"
    assert result.selected_action_id is None
    assert result.advisory_only is True
    assert result.runtime_authorization_required is True
    assert result.may_execute is False
    assert result.external_action_authority is False


@pytest.mark.asyncio
async def test_jev_choice_translates_tokens_back_to_provider_ids_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _Client.payload = {
        "model": "jev-1.13.0",
        "answers": {
            "selected_action": {
                "type": "choice",
                "choice": "a1",
                "confidence": 0.91,
                "probabilities": {
                    "a0": 0.09,
                    "a1": 0.91,
                    "invented": 0.99,
                },
            },
            "needs_human_review": {"type": "noul", "noul": 0.97},
            "needs_more_information": {"type": "noul", "noul": 0.18},
            "consequence_risk": {"type": "noul", "noul": 0.88},
            "uncertainty": {"type": "noul", "noul": 0.21},
        },
    }
    monkeypatch.setattr(action_adapter.httpx, "AsyncClient", _Client)

    envelope = _finance_envelope()
    envelope["goal"] = "Review this safely Authorization: Bearer top-secret-goal-token"
    envelope["actions"][1]["description"] = "Prepare plan with Bearer top-secret-description-token"
    result = await action_adapter.evaluate_action_envelope(
        _studio(),
        envelope,
    )

    assert result.evaluated is True
    assert result.selected_action_id == "navinvestor:rebalance:plan-a"
    assert result.probabilities == {
        "navinvestor:hold": 0.09,
        "navinvestor:rebalance:plan-a": 0.91,
    }
    assert result.needs_human_review == 0.97
    assert result.consequence_risk == 0.88
    assert result.may_execute is False
    assert result.external_action_authority is False

    request_json = _Client.request["json"]
    encoded = str(request_json)
    assert "must-never-reach-jev" not in encoded
    assert "top-secret-goal-token" not in encoded
    assert "top-secret-description-token" not in encoded
    assert request_json["state"]["constraints"]["jev_may_not_execute"] is True
    assert set(request_json["questions"]["selected_action"]["criteria"]) == {"a0", "a1"}


@pytest.mark.asyncio
async def test_unknown_jev_choice_is_rejected_not_executed(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _Client.payload = {
        "answers": {
            "selected_action": {
                "type": "choice",
                "choice": "a99",
                "confidence": 0.99,
            }
        }
    }
    monkeypatch.setattr(action_adapter.httpx, "AsyncClient", _Client)

    result = await action_adapter.evaluate_action_envelope(
        _studio(),
        action_adapter.sample_action_envelope(),
    )
    assert result.evaluated is False
    assert result.error == "jev_invalid_action"
    assert result.selected_action_id is None
    assert result.may_execute is False
