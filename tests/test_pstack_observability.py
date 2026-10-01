from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "plugins" / "pstack" / "adapter.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "hirda_test_pstack_observability",
        ADAPTER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_route_task_emits_bounded_trace_and_hirda_boundary():
    module = _load_module()
    adapter = module.PstackIntegration(repository_root=ROOT)

    result = await adapter.call_tool(
        "route_task",
        {"goal": "Fix the reconnect crash regression."},
        context={
            "managed_session_id": "ms-test",
            "task_id": "task-42",
        },
    )

    snapshot = adapter.activity_snapshot()
    trace = snapshot["active_trace"]

    assert result["trace_id"] == trace["trace_id"]
    assert trace["workflow"] == "bug-fix"
    assert trace["status"] == "ready_for_hirda"
    assert trace["identity"]["task_id"] == "task-42"
    assert snapshot["runtime_authority"] == "hirda"
    assert snapshot["model_selection"] == "deferred_to_hirda"
    assert [event["phase"] for event in snapshot["events"]] == [
        "received",
        "workflow_selected",
        "methodology_loaded",
        "handoff_ready",
    ]
    assert snapshot["events"][-1]["status"] == "ready"
    request = result["coordinator"]["hirda_request"]
    assert request["context"]["pstack_trace_id"] == result["trace_id"]
    assert "preferred_runtime" not in request


def test_review_named_workspace_does_not_select_review_workflow():
    module = _load_module()
    workflow = module.PstackIntegration._select_workflow(
        "mcp-studio-social-hub-task-connector-review",
    )
    assert workflow == "feature"


@pytest.mark.parametrize(
    "goal",
    [
        "Review this diff for correctness.",
        "Audit this code for security issues.",
        "Interrogate the design assumptions.",
        "Complete the PR review.",
    ],
)
def test_explicit_review_intent_selects_review_workflow(goal):
    module = _load_module()
    workflow = module.PstackIntegration._select_workflow(goal)
    assert workflow == "review"


@pytest.mark.asyncio
async def test_pstack_failure_is_visible_in_activity_trace():
    module = _load_module()
    adapter = module.PstackIntegration(repository_root=ROOT)

    with pytest.raises(module.PstackAdapterError, match="pstack_goal_required"):
        await adapter.call_tool(
            "route_task",
            {"goal": ""},
            context={"managed_session_id": "ms-test"},
        )

    snapshot = adapter.activity_snapshot()
    assert snapshot["active_trace"]["status"] == "failed"
    assert snapshot["events"][-1]["phase"] == "failed"
    assert snapshot["events"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_hirda_lifecycle_events_continue_existing_pstack_trace():
    module = _load_module()
    adapter = module.PstackIntegration(repository_root=ROOT)

    result = await adapter.call_tool(
        "route_task",
        {"goal": "Add a timeline for Pstack activity."},
        context={"task_id": "task-hirda-lifecycle"},
    )
    adapter.record_external_event(
        result["trace_id"],
        actor="hirda",
        phase="hirda_execution_started",
        title="HIRDA execution started",
        detail="Runtime selection and execution began.",
        status="running",
        trace_status="executing",
        metadata={"runtime": "hermes"},
    )

    snapshot = adapter.activity_snapshot()
    assert snapshot["active_trace"]["status"] == "executing"
    assert snapshot["events"][-1]["actor"] == "hirda"
    assert snapshot["events"][-1]["phase"] == "hirda_execution_started"
    assert snapshot["events"][-1]["runtime_authority"] == "hirda"


def test_pstack_activity_ui_is_wired_for_live_sse():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "static" / "app.pstack.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "pstack.css").read_text(encoding="utf-8")

    assert 'data-view="pstack"' in html
    assert 'data-page="pstack"' in html
    assert "/static/app.pstack.js" in html
    assert "/static/pstack.css" in html
    assert "/api/pstack/activity/stream" in app
    assert "new EventSource" in app
    assert "READY FOR HIRDA" in app
    assert ".pstack-timeline-row" in css
