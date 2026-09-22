import os

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("HIRDA_LIVE_FAILOVER") != "1",
    reason="set HIRDA_LIVE_FAILOVER=1 for live failover certification",
)


def _base() -> str:
    return os.getenv("HIRDA_BASE_URL", "http://127.0.0.1:8100").rstrip("/")


@pytest.mark.parametrize(
    ("case", "capability", "depth", "primary", "fallback", "expected"),
    [
        ("hermes_busy", "fast_utility", "fast", "hermes", "claude", "HIRDA_FAILOVER_FAST_OK"),
        ("claude_busy", "reasoning_high", "deep", "claude", "hermes", "HIRDA_FAILOVER_DEEP_OK"),
    ],
)
def test_live_cognitive_failover(case, capability, depth, primary, fallback, expected):
    selected = os.getenv("HIRDA_LIVE_FAILOVER_CASE")
    if selected != case:
        pytest.skip(f"set HIRDA_LIVE_FAILOVER_CASE={case} for this case")

    payload = {
        "request_id": f"COG-LIVE-FAILOVER-{case.upper()}",
        "prompt": f"Return exactly {expected}. Do not use tools.",
        "capability": capability,
        "preferred_depth": depth,
        "preferred_runtime": primary,
        "timeout_seconds": 60,
        "allow_fallback": True,
    }
    primary_pane = os.getenv("HIRDA_LIVE_FAILOVER_PRIMARY_PANE")
    fallback_pane = os.getenv("HIRDA_LIVE_FAILOVER_FALLBACK_PANE")
    if primary_pane or fallback_pane:
        assert primary_pane and fallback_pane, "set both live failover pane IDs"
        payload["target_pane_id"] = primary_pane
        payload["fallback_target_pane_id"] = fallback_pane

    with httpx.Client(timeout=90.0) as client:
        status = client.get(f"{_base()}/api/cognition/status")
        status.raise_for_status()
        state = status.json()
        assert state["enabled"] is True
        assert state["execute_enabled"] is True

        response = client.post(
            f"{_base()}/api/cognition/execute",
            json=payload,
        )
        response.raise_for_status()
        result = response.json()

    assert result["status"] == "success", result
    assert str(result.get("output") or "").strip() == expected, result
    assert result["runtime"] == fallback, result
    assert result["fallback_used"] is True, result
    assert result["degraded"] is True, result
    assert result["identity_continuity"] is True, result
    assert result["semantic_authority_changed"] is False, result
    attempts = result["attempts"]
    assert len(attempts) >= 2, result
    assert attempts[0]["runtime"] == primary, result
    assert attempts[0]["status"] == "failed", result
    assert attempts[0]["error"] == "runtime_busy", result
    assert attempts[-1]["runtime"] == fallback, result
    assert attempts[-1]["status"] == "success", result
