import pytest

from mcp_studio.pstack_lifecycle import (
    PstackLifecycleError,
    checkpoint,
    lifecycle_for_workflow,
    record_tool_evidence,
    workflow_gate,
)


def _pass_analyze(lifecycle):
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="find_symbol",
        tool_class="read",
        status="passed",
        detail="Inspected the affected subsystem.",
    )
    return checkpoint(
        lifecycle,
        phase="analyze",
        status="passed",
        evidence="Inspected the affected subsystem and identified the change boundary.",
    )


def test_feature_lifecycle_enforces_plan_before_writes_and_verify_before_done():
    lifecycle = lifecycle_for_workflow("feature")
    assert lifecycle["current_phase"] == "analyze"
    assert [stage["phase"] for stage in lifecycle["stages"]] == [
        "analyze",
        "plan",
        "implement",
        "review",
        "verify",
        "done",
    ]
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_PLAN_REQUIRED"

    with pytest.raises(PstackLifecycleError, match="Analyze requires"):
        checkpoint(
            lifecycle,
            phase="analyze",
            status="passed",
            evidence="analysis complete",
        )

    lifecycle = _pass_analyze(lifecycle)
    assert lifecycle["current_phase"] == "plan"
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_PLAN_REQUIRED"

    lifecycle = checkpoint(
        lifecycle,
        phase="plan",
        status="passed",
        evidence="Plan: update the lifecycle gate, persist checkpoints, then add regression tests.",
    )
    assert lifecycle["current_phase"] == "implement"
    assert workflow_gate(lifecycle, tool_class="write")["allowed"] is True

    with pytest.raises(PstackLifecycleError, match="successful write evidence"):
        checkpoint(
            lifecycle,
            phase="implement",
            status="passed",
            evidence="implementation complete",
        )

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="replace_content",
        tool_class="write",
        status="passed",
        detail="Applied the implementation patch.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="implement",
        status="passed",
        evidence="Implemented the planned lifecycle gate.",
    )
    assert lifecycle["current_phase"] == "review"
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_IMPLEMENTATION_GATE_CLOSED"

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="execute_shell_command",
        tool_class="read",
        status="passed",
        detail="Reviewed the targeted git diff.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="review",
        status="passed",
        evidence="Reviewed the diff for correctness, regressions, and authority boundaries.",
    )
    assert lifecycle["current_phase"] == "verify"

    with pytest.raises(PstackLifecycleError, match="Verify requires"):
        checkpoint(
            lifecycle,
            phase="verify",
            status="passed",
            evidence="tests passed",
        )

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="execute_shell_command",
        tool_class="execute",
        status="passed",
        detail="pytest passed.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="verify",
        status="passed",
        evidence="Regression suite and syntax checks passed.",
    )
    assert lifecycle["current_phase"] == "done"

    lifecycle = checkpoint(
        lifecycle,
        phase="done",
        status="passed",
        evidence="All required Pstack gates passed.",
    )
    assert lifecycle["status"] == "completed"
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_WORKFLOW_COMPLETED"


def test_checkpoint_order_is_fail_closed():
    lifecycle = lifecycle_for_workflow("bug-fix")
    with pytest.raises(PstackLifecycleError) as exc:
        checkpoint(
            lifecycle,
            phase="plan",
            status="passed",
            evidence="Plan before analysis.",
        )
    assert exc.value.code == "PSTACK_PHASE_ORDER_VIOLATION"


def test_review_workflow_is_read_only_and_shorter():
    lifecycle = lifecycle_for_workflow("review")
    assert [stage["phase"] for stage in lifecycle["stages"]] == [
        "analyze",
        "review",
        "done",
    ]
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_REVIEW_WORKFLOW_READ_ONLY"

    lifecycle = _pass_analyze(lifecycle)
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="find_symbol",
        tool_class="read",
        status="passed",
        detail="Inspected the review scope.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="review",
        status="passed",
        evidence="Reviewed the requested scope and recorded findings.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="done",
        status="passed",
        evidence="Review workflow complete.",
    )
    assert lifecycle["status"] == "completed"


def test_failed_checkpoint_blocks_without_skipping_phase():
    lifecycle = _pass_analyze(lifecycle_for_workflow("feature"))
    lifecycle = checkpoint(
        lifecycle,
        phase="plan",
        status="failed",
        evidence="Plan is blocked on missing acceptance criteria.",
    )
    assert lifecycle["status"] == "blocked"
    assert lifecycle["current_phase"] == "plan"

    lifecycle = checkpoint(
        lifecycle,
        phase="plan",
        status="passed",
        evidence="Acceptance criteria recovered; plan is now complete.",
    )
    assert lifecycle["status"] == "active"
    assert lifecycle["current_phase"] == "implement"


def test_review_failure_reopens_implementation_for_rework():
    lifecycle = _pass_analyze(lifecycle_for_workflow("feature"))
    lifecycle = checkpoint(
        lifecycle,
        phase="plan",
        status="passed",
        evidence="Plan is complete.",
    )
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="replace_content",
        tool_class="write",
        status="passed",
        detail="Implemented the planned change.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="implement",
        status="passed",
        evidence="Implementation completed.",
    )
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="execute_shell_command",
        tool_class="read",
        status="passed",
        detail="Reviewed the diff and found a lifecycle gap.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="review",
        status="failed",
        evidence="Review found a correctness issue that requires implementation rework.",
    )
    assert lifecycle["status"] == "active"
    assert lifecycle["current_phase"] == "implement"
    assert lifecycle["rework_from"] == "review"
    assert workflow_gate(lifecycle, tool_class="write")["allowed"] is True
