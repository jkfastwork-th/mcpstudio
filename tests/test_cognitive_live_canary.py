import os

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("HIRDA_LIVE_CANARY") != "1",
    reason="set HIRDA_LIVE_CANARY=1 to probe the running HIRDA service",
)


def _base() -> str:
    return os.environ.get("HIRDA_BASE_URL", "http://127.0.0.1:8100")


def _assert_status(client: httpx.Client) -> None:
    status = client.get(f"{_base()}/api/cognition/status")
    status.raise_for_status()
    snapshot = status.json()
    assert snapshot["enabled"] is True
    assert snapshot["execute_enabled"] is True
    assert snapshot["semantic_authority"] == "external"


def _run_case(
    client: httpx.Client,
    *,
    label: str,
    capability: str,
    depth: str,
    expected: str,
    preferred_runtime: str,
    target_pane_id: str | None = None,
) -> None:
    plan = client.post(
        f"{_base()}/api/cognition/plan",
        json={
            "request_id": f"COG-LIVE-{label}-PLAN",
            "prompt": f"Return exactly {expected}. Do not use tools.",
            "capability": capability,
            "preferred_depth": depth,
            "timeout_seconds": 30,
            "allow_fallback": True,
            **({"target_pane_id": target_pane_id} if target_pane_id else {}),
        },
    )
    plan.raise_for_status()
    routing = plan.json()
    assert routing["lane"] == depth
    assert routing["identity_continuity"] is True
    assert routing["semantic_authority_changed"] is False
    assert routing["candidates"]
    assert routing["candidates"][0]["runtime"] == preferred_runtime

    execute = client.post(
        f"{_base()}/api/cognition/execute",
        json={
            "request_id": f"COG-LIVE-{label}-EXEC",
            "prompt": f"Return exactly {expected}. Do not use tools.",
            "capability": capability,
            "preferred_depth": depth,
            "preferred_runtime": preferred_runtime,
            "timeout_seconds": 30,
            "allow_fallback": False,
            **({"target_pane_id": target_pane_id} if target_pane_id else {}),
        },
    )
    execute.raise_for_status()
    result = execute.json()
    assert result["status"] == "success", result
    assert str(result.get("output") or "").strip() == expected, result
    assert result["runtime"] == preferred_runtime, result
    assert result["identity_continuity"] is True
    assert result["semantic_authority_changed"] is False
    assert result["attempts"]
    assert result["attempts"][-1]["status"] == "success"


def test_live_cognitive_fast():
    with httpx.Client(timeout=40.0) as client:
        _assert_status(client)
        _run_case(
            client,
            label="FAST",
            capability="fast_utility",
            depth="fast",
            expected="HIRDA_FAST_OK",
            preferred_runtime="hermes",
            target_pane_id=os.environ.get("HIRDA_CERT_HERMES_PANE"),
        )


def test_live_cognitive_deep():
    with httpx.Client(timeout=40.0) as client:
        _assert_status(client)
        _run_case(
            client,
            label="DEEP",
            capability="reasoning_high",
            depth="deep",
            expected="HIRDA_DEEP_OK",
            preferred_runtime="claude",
            target_pane_id=os.environ.get("HIRDA_CERT_CLAUDE_PANE"),
        )
