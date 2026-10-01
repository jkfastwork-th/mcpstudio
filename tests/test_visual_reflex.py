from types import SimpleNamespace

import pytest

from mcp_studio import visual_reflex
from mcp_studio.action_adapter import JevActionJudgment


def _studio():
    return SimpleNamespace(
        jev_enabled=True,
        jev_mode="shadow",
        jev_api_url="https://api.typesafe.ai/v1/systemone",
        jev_api_key_env="TYPESAFE_API_KEY",
        jev_model="jev-latest",
        jev_timeout_seconds=2.0,
        jev_max_state_chars=12000,
    )


def _payload():
    return {
        "schema": visual_reflex.VISUAL_REFLEX_REQUEST_SCHEMA,
        "request_id": "jkfastdev:instagram:share",
        "goal": "Click the Share button to publish the prepared Instagram post.",
        "machine_id": "JKFASTDEV",
        "viewport": {"width": 1920, "height": 1080},
        "state": {"app": "instagram", "screen": "composer"},
        "targets": [
            {
                "target_id": "cancel",
                "label": "Cancel",
                "role": "button",
                "source": "uia",
                "bounds": {"x": 1600, "y": 20, "width": 100, "height": 40},
                "confidence": 0.99,
            },
            {
                "target_id": "share",
                "label": "Share",
                "role": "button",
                "source": "dom",
                "bounds": {"x": 1740, "y": 20, "width": 120, "height": 40},
                "confidence": 0.96,
            },
        ],
    }


def _judgment(selected_action_id, *, evaluated=True, error=None):
    return JevActionJudgment(
        schema="hirda-jev-action-judgment-v1",
        enabled=True,
        evaluated=evaluated,
        mode="shadow",
        request_id="jkfastdev:instagram:share",
        provider="visual-reflex",
        domain="computer",
        selected_action_id=selected_action_id,
        confidence=0.98 if selected_action_id else 0.0,
        probabilities={},
        needs_human_review=0.0,
        needs_more_information=0.0,
        consequence_risk=0.0,
        uncertainty=0.0,
        model="jev-test",
        error=error,
    )


def test_normalize_rejects_duplicate_and_out_of_bounds_targets():
    payload = _payload()
    payload["targets"][1]["target_id"] = "cancel"
    with pytest.raises(visual_reflex.VisualReflexError, match="duplicate target_id"):
        visual_reflex.normalize_visual_reflex_request(payload)

    payload = _payload()
    payload["targets"][1]["bounds"]["x"] = 1900
    with pytest.raises(visual_reflex.VisualReflexError, match="outside viewport"):
        visual_reflex.normalize_visual_reflex_request(payload)


def test_action_envelope_keeps_coordinates_out_of_jev_candidates():
    request = visual_reflex.normalize_visual_reflex_request(_payload())
    envelope = visual_reflex.build_visual_action_envelope(request)
    rendered = envelope.as_dict()

    assert envelope.provider == "visual-reflex"
    assert envelope.domain == "computer"
    assert rendered["actions"][1]["arguments"] == {"target_id": "share"}
    serialized = repr(rendered["actions"])
    assert "'x':" not in serialized
    assert "'y':" not in serialized
    assert "'width':" not in serialized
    assert "'height':" not in serialized

    payload = _payload()
    payload["state"]["cursor"] = {"x": 100, "y": 200}
    payload["state"]["dialog"] = {"bounds": {"x": 1, "y": 2, "width": 3, "height": 4}, "name": "composer"}
    safe = visual_reflex.build_visual_action_envelope(
        visual_reflex.normalize_visual_reflex_request(payload)
    ).as_dict()["state"]
    assert safe["cursor"] == {}
    assert safe["dialog"] == {"name": "composer"}


@pytest.mark.asyncio
async def test_jev_selects_target_id_and_runtime_computes_center(monkeypatch):
    seen = {}

    async def fake_evaluate(studio, envelope):
        seen["envelope"] = envelope
        return _judgment("share")

    monkeypatch.setattr(visual_reflex, "evaluate_action_envelope", fake_evaluate)
    result = await visual_reflex.evaluate_visual_reflex(_studio(), _payload())

    assert result["selected_target_id"] == "share"
    assert result["selection_source"] == "jev"
    assert result["execution_plan"]["x"] == 1800.0
    assert result["execution_plan"]["y"] == 40.0
    assert result["execution_plan"]["may_execute"] is False
    assert result["execution_plan"]["runtime_authorization_required"] is True
    assert seen["envelope"].actions[1].arguments == {"target_id": "share"}
    assert result["metrics"]["candidate_count"] == 2
    assert result["metrics"]["jev_ms"] >= 0


@pytest.mark.asyncio
async def test_fallback_prefers_source_priority_before_detector_confidence(monkeypatch):
    payload = _payload()
    payload["targets"][0]["confidence"] = 1.0
    payload["targets"][1]["confidence"] = 0.1

    async def fake_evaluate(studio, envelope):
        return _judgment(None, evaluated=False, error="jev_request_failed")

    monkeypatch.setattr(visual_reflex, "evaluate_action_envelope", fake_evaluate)
    result = await visual_reflex.evaluate_visual_reflex(_studio(), payload)

    assert result["selected_target_id"] == "share"
    assert result["selection_source"] == "fallback"
    assert result["target"]["source"] == "dom"
    assert result["jev"]["error"] == "jev_request_failed"
