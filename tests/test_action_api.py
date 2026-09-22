from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mcp_studio import main


@pytest.mark.asyncio
async def test_action_provider_catalog_endpoint_is_side_effect_free():
    result = await main.action_adapter_providers()
    assert result["executor_attached"] is False
    assert [row["provider_id"] for row in result["providers"]] == [
        "browser",
        "world",
        "finance",
    ]


@pytest.mark.asyncio
async def test_action_provider_detail_endpoint_returns_normalized_envelope():
    result = await main.action_adapter_provider("world")
    assert result["provider_id"] == "world"
    assert result["executor_attached"] is False
    assert result["envelope"]["schema"] == "hirda-action-envelope-v1"
    assert result["envelope"]["metadata"]["executor_attached"] is False


@pytest.mark.asyncio
async def test_action_provider_unknown_endpoint_returns_404():
    with pytest.raises(HTTPException) as exc:
        await main.action_adapter_provider("not-real")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_action_evaluate_endpoint_returns_judgment_without_execution(monkeypatch):
    class Judgment:
        def as_dict(self):
            return {
                "schema": "hirda-jev-action-judgment-v1",
                "enabled": True,
                "evaluated": True,
                "mode": "shadow",
                "request_id": "jaa1:browser:search",
                "provider": "test-browser",
                "domain": "browser",
                "selected_action_id": "browser:submit-search",
                "confidence": 0.88,
                "probabilities": {"browser:submit-search": 0.88},
                "needs_human_review": 0.05,
                "needs_more_information": 0.10,
                "consequence_risk": 0.05,
                "uncertainty": 0.12,
                "model": "jev-test",
                "error": None,
                "advisory_only": True,
                "runtime_authorization_required": True,
                "may_execute": False,
                "external_action_authority": False,
            }

    calls = []

    async def fake_evaluate(studio, envelope):
        calls.append((studio, envelope))
        return Judgment()

    monkeypatch.setattr(main, "evaluate_action_envelope", fake_evaluate)
    result = await main.action_adapter_evaluate("browser")

    assert len(calls) == 1
    assert result["schema"] == "hirda-action-playground-result-v1"
    assert result["executor_attached"] is False
    assert result["execution_performed"] is False
    assert result["judgment"]["selected_action_id"] == "browser:submit-search"
    assert result["judgment"]["may_execute"] is False
