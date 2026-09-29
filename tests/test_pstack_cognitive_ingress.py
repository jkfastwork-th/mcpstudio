from __future__ import annotations

import pytest

from mcp_studio import main


class _Registration:
    stage = "ready"


class _PstackManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def get(self, integration_id: str):
        assert integration_id == "pstack"
        return _Registration()

    async def reconcile(self, integration_id: str):
        raise AssertionError("ready Pstack should not need reconcile")

    async def call_backend_tool(self, name: str, arguments: dict, *, context: dict):
        self.calls.append((name, dict(arguments), dict(context)))
        return {
            "trace_id": "PSTACK-TRACE-TEST",
            "workflow": "bug-fix",
            "source": {
                "repository": "michael-denyer/pstack-claude",
                "revision": "rev",
                "path": "bug-fix.md",
            },
            "coordinator": {
                "hirda_request": {
                    "prompt": "PSTACK METHODOLOGY:\n\nreproduce -> diagnose -> patch -> verify",
                    "capability": "coding",
                    "preferred_depth": "deep",
                    "context": {
                        "workflow_provider": "pstack",
                        "workflow": "bug-fix",
                        "role": "coordinator",
                        "runtime_authority": "hirda",
                        "pstack_trace_id": "PSTACK-TRACE-TEST",
                    },
                }
            },
        }


@pytest.mark.asyncio
async def test_coding_ingress_auto_applies_pstack_without_taking_runtime_authority(
    monkeypatch,
):
    manager = _PstackManager()
    monkeypatch.setattr(main, "integration_manager", manager)

    prepared, metadata = await main._prepare_pstack_cognitive_payload(
        {
            "request_id": "COG-42",
            "prompt": "Fix the reconnect crash regression.",
            "capability": "coding",
            "preferred_depth": "auto",
            "preferred_runtime": "hermes",
            "context": {"managed_session_id": "ms-42"},
        }
    )

    assert metadata is not None and metadata["applied"] is True
    assert metadata["trace_id"] == "PSTACK-TRACE-TEST"
    assert prepared["prompt"].startswith("PSTACK METHODOLOGY:")
    assert prepared["preferred_depth"] == "auto"
    assert prepared["preferred_runtime"] == "hermes"
    assert prepared["context"]["pstack_methodology_applied"] is True
    assert prepared["context"]["workflow_provider"] == "pstack"
    assert prepared["context"]["runtime_authority"] == "hirda"

    name, arguments, context = manager.calls[0]
    assert name == "hirda__pstack__route_task"
    assert arguments["goal"] == "Fix the reconnect crash regression."
    assert context["task_id"] == "COG-42"
    assert context["managed_session_id"] == "ms-42"


@pytest.mark.asyncio
async def test_noncoding_ingress_does_not_invoke_pstack(monkeypatch):
    manager = _PstackManager()
    monkeypatch.setattr(main, "integration_manager", manager)

    original = {
        "request_id": "COG-REFLECTION",
        "prompt": "Reflect on this diary entry.",
        "capability": "reflection",
        "context": {},
    }
    prepared, metadata = await main._prepare_pstack_cognitive_payload(original)

    assert prepared == original
    assert metadata is None
    assert manager.calls == []
