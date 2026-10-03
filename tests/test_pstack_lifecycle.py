import pytest

from mcp_studio.pstack_lifecycle import (
    PSTACK_LIFECYCLE_SCHEMA,
    PSTACK_LIFECYCLE_SCHEMA_V1,
    PstackLifecycleError,
    checkpoint,
    ensure_lifecycle,
    legacy_lifecycle_for_workflow,
    lifecycle_for_workflow,
    record_tool_evidence,
    workflow_gate,
)


def _pass_admitted(lifecycle):
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="find_symbol",
        tool_class="read",
        status="passed",
        detail="Inspected the affected subsystem.",
    )
    return checkpoint(
        lifecycle,
        phase="admitted",
        status="passed",
        evidence="HIRDA admitted the task after inspecting the affected subsystem.",
    )


def test_v2_lifecycle_delegates_methodology_to_pstack_inside_working():
    lifecycle = lifecycle_for_workflow("feature")
    assert lifecycle["schema"] == PSTACK_LIFECYCLE_SCHEMA
    assert lifecycle["orchestration_owner"] == "hirda"
    assert lifecycle["methodology_owner"] == "pstack"
    assert lifecycle["current_phase"] == "admitted"
    assert [stage["phase"] for stage in lifecycle["stages"]] == [
        "admitted",
        "working",
        "verified",
        "terminal",
    ]
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_WORKING_REQUIRED"

    with pytest.raises(PstackLifecycleError, match="Admitted requires"):
        checkpoint(
            lifecycle,
            phase="admitted",
            status="passed",
            evidence="admission complete",
        )

    lifecycle = _pass_admitted(lifecycle)
    assert lifecycle["current_phase"] == "working"
    assert workflow_gate(lifecycle, tool_class="write")["allowed"] is True

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="replace_content",
        tool_class="write",
        status="passed",
        detail="Applied the methodology-selected implementation change.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="working",
        status="passed",
        evidence="PStack methodology work completed with concrete implementation evidence.",
    )
    assert lifecycle["current_phase"] == "verified"
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_WORKING_REQUIRED"

    with pytest.raises(PstackLifecycleError, match="Verified requires"):
        checkpoint(
            lifecycle,
            phase="verified",
            status="passed",
            evidence="verification complete",
        )

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="execute_shell_command",
        tool_class="execute",
        status="passed",
        detail="Regression tests passed.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="verified",
        status="passed",
        evidence="Regression suite and syntax checks passed.",
    )
    assert lifecycle["current_phase"] == "terminal"

    lifecycle = checkpoint(
        lifecycle,
        phase="terminal",
        status="passed",
        evidence="HIRDA orchestration can close after successful verification.",
    )
    assert lifecycle["status"] == "completed"
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_WORKFLOW_COMPLETED"


def test_unknown_tool_class_is_fail_closed_by_pstack_gate():
    lifecycle = lifecycle_for_workflow("feature")
    gate = workflow_gate(lifecycle, tool_class="unknown")
    assert gate["allowed"] is False
    assert gate["code"] == "PSTACK_TOOL_CLASS_UNKNOWN"


def test_v2_checkpoint_order_is_fail_closed():
    lifecycle = lifecycle_for_workflow("bug-fix")
    with pytest.raises(PstackLifecycleError) as exc:
        checkpoint(
            lifecycle,
            phase="working",
            status="passed",
            evidence="Working before admission.",
        )
    assert exc.value.code == "PSTACK_PHASE_ORDER_VIOLATION"


def test_v2_review_workflow_remains_read_only():
    lifecycle = lifecycle_for_workflow("review")
    assert [stage["phase"] for stage in lifecycle["stages"]] == [
        "admitted",
        "working",
        "verified",
        "terminal",
    ]
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_REVIEW_WORKFLOW_READ_ONLY"

    lifecycle = _pass_admitted(lifecycle)
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="find_symbol",
        tool_class="read",
        status="passed",
        detail="PStack review methodology inspected the requested scope.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="working",
        status="passed",
        evidence="Review methodology completed without mutation.",
    )
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="get_diagnostics_for_file",
        tool_class="read",
        status="passed",
        detail="Diagnostics inspection completed.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="verified",
        status="passed",
        evidence="Diagnostics provided verification evidence.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="terminal",
        status="passed",
        evidence="Review orchestration complete.",
    )
    assert lifecycle["status"] == "completed"


def test_v2_failed_admission_blocks_without_skipping_phase():
    lifecycle = lifecycle_for_workflow("feature")
    lifecycle = checkpoint(
        lifecycle,
        phase="admitted",
        status="failed",
        evidence="Admission is blocked on missing workspace evidence.",
    )
    assert lifecycle["status"] == "blocked"
    assert lifecycle["current_phase"] == "admitted"
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_WORKFLOW_BLOCKED"

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="find_symbol",
        tool_class="read",
        status="passed",
        detail="Recovered the missing workspace evidence.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="admitted",
        status="passed",
        evidence="Workspace evidence recovered; admission can proceed.",
    )
    assert lifecycle["status"] == "active"
    assert lifecycle["current_phase"] == "working"


def test_v2_verified_failure_reopens_working_for_rework():
    lifecycle = _pass_admitted(lifecycle_for_workflow("feature"))
    lifecycle = record_tool_evidence(
        lifecycle,
        tool="replace_content",
        tool_class="write",
        status="passed",
        detail="PStack methodology produced an implementation candidate.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="working",
        status="passed",
        evidence="Methodology work is ready for verification.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="verified",
        status="failed",
        evidence="Verification found a correctness issue requiring rework.",
    )
    assert lifecycle["status"] == "active"
    assert lifecycle["current_phase"] == "working"
    assert lifecycle["rework_from"] == "verified"
    assert workflow_gate(lifecycle, tool_class="write")["allowed"] is True


def test_v1_lifecycle_remains_compatible_for_existing_tasks():
    lifecycle = legacy_lifecycle_for_workflow("feature")
    assert lifecycle["schema"] == PSTACK_LIFECYCLE_SCHEMA_V1
    assert [stage["phase"] for stage in lifecycle["stages"]] == [
        "analyze",
        "plan",
        "implement",
        "review",
        "verify",
        "done",
    ]
    lifecycle = ensure_lifecycle(lifecycle, workflow="feature")
    assert lifecycle["schema"] == PSTACK_LIFECYCLE_SCHEMA_V1
    assert workflow_gate(lifecycle, tool_class="write")["code"] == "PSTACK_PLAN_REQUIRED"

    lifecycle = record_tool_evidence(
        lifecycle,
        tool="find_symbol",
        tool_class="read",
        status="passed",
        detail="Inspected the legacy task scope.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="analyze",
        status="passed",
        evidence="Legacy Analyze checkpoint remains valid.",
    )
    lifecycle = checkpoint(
        lifecycle,
        phase="plan",
        status="passed",
        evidence="Legacy Plan checkpoint remains valid.",
    )
    assert lifecycle["current_phase"] == "implement"
    assert workflow_gate(lifecycle, tool_class="write")["allowed"] is True
