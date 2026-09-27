from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from mcp_studio import main


class FakeResult:
    def __init__(self, data):
        self.data = dict(data)

    def as_dict(self):
        return dict(self.data)


class FakeCognitiveRouter:
    def __init__(self, result, *, delay=0.0):
        self.result = dict(result)
        self.delay = delay
        self.calls = 0

    async def execute(self, payload):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return FakeResult({**self.result, "request_id": payload["request_id"]})


class FakeObservability:
    def __init__(self):
        self.execute_calls = 0
        self.error_calls = 0

    async def record_execute(self, payload, result, *, elapsed_ms):
        self.execute_calls += 1

    async def record_error(self, *args, **kwargs):
        self.error_calls += 1


@pytest.fixture(autouse=True)
def reset_idempotency_state(monkeypatch):
    main._cognitive_execute_inflight.clear()
    main._cognitive_execute_completed.clear()
    monkeypatch.setattr(main, "_cognitive_execute_idempotency_lock", asyncio.Lock())
    monkeypatch.setattr(main.settings.studio, "cognitive_router_enabled", True)
    monkeypatch.setattr(main.settings.studio, "cognitive_router_execute_enabled", True)


@pytest.mark.asyncio
async def test_concurrent_duplicate_execute_shares_one_physical_execution(monkeypatch):
    router = FakeCognitiveRouter({"status": "success", "output": "OK"}, delay=0.03)
    observability = FakeObservability()
    monkeypatch.setattr(main, "cognitive_router", router)
    monkeypatch.setattr(main, "cognitive_observability", observability)
    payload = {
        "request_id": "COG-IDEMPOTENT-CONCURRENT",
        "prompt": "Return OK.",
        "capability": "fast_utility",
    }

    first, second = await asyncio.gather(
        main.cognitive_router_execute(dict(payload)),
        main.cognitive_router_execute(dict(payload)),
    )

    assert first == second
    assert router.calls == 1
    assert observability.execute_calls == 1


@pytest.mark.asyncio
async def test_completed_duplicate_execute_reuses_cached_result(monkeypatch):
    router = FakeCognitiveRouter({"status": "success", "output": "OK"})
    observability = FakeObservability()
    monkeypatch.setattr(main, "cognitive_router", router)
    monkeypatch.setattr(main, "cognitive_observability", observability)
    payload = {
        "request_id": "COG-IDEMPOTENT-CACHED",
        "prompt": "Return OK.",
        "capability": "fast_utility",
    }

    first = await main.cognitive_router_execute(dict(payload))
    second = await main.cognitive_router_execute(dict(payload))

    assert first == second
    assert router.calls == 1
    assert observability.execute_calls == 1


@pytest.mark.asyncio
async def test_failed_cognitive_result_is_idempotently_cached(monkeypatch):
    router = FakeCognitiveRouter(
        {
            "status": "failed",
            "output": None,
            "error": "all_cognitive_runtimes_failed",
        }
    )
    observability = FakeObservability()
    monkeypatch.setattr(main, "cognitive_router", router)
    monkeypatch.setattr(main, "cognitive_observability", observability)
    payload = {
        "request_id": "COG-IDEMPOTENT-FAILED",
        "prompt": "Return OK.",
        "capability": "fast_utility",
    }

    first = await main.cognitive_router_execute(dict(payload))
    second = await main.cognitive_router_execute(dict(payload))

    assert first["status"] == "failed"
    assert second == first
    assert router.calls == 1
    assert observability.execute_calls == 1


@pytest.mark.asyncio
async def test_duplicate_request_id_with_different_payload_fails_closed(monkeypatch):
    router = FakeCognitiveRouter({"status": "success", "output": "OK"})
    observability = FakeObservability()
    monkeypatch.setattr(main, "cognitive_router", router)
    monkeypatch.setattr(main, "cognitive_observability", observability)

    await main.cognitive_router_execute(
        {
            "request_id": "COG-IDEMPOTENT-CONFLICT",
            "prompt": "Return OK.",
            "capability": "fast_utility",
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await main.cognitive_router_execute(
            {
                "request_id": "COG-IDEMPOTENT-CONFLICT",
                "prompt": "Return something else.",
                "capability": "fast_utility",
            }
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "cognitive_request_id_conflict"
    assert router.calls == 1
