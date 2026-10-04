from __future__ import annotations

from typing import Any


EXECUTION_CERTIFICATION_SCHEMA = "hirda-execution-certification-v1"
AI_QUALITY_CERTIFICATION_SCHEMA = "hirda-ai-quality-certification-v1"


def _check(name: str, passed: bool, *, actual: Any, expected: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "actual": actual,
        "expected": expected,
    }


def certify_execution(
    work: dict[str, Any],
    *,
    pstack_task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministically certify execution without judging answer quality."""
    result = work.get("result")
    result = result if isinstance(result, dict) else {}
    semantic = result.get("semantic_completion")
    semantic = semantic if isinstance(semantic, dict) else {}
    execution = work.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    metadata = work.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    deterministic_action = metadata.get("deterministic_action") is True
    readiness = (
        result.get("runtime_readiness")
        if isinstance(result.get("runtime_readiness"), dict)
        else execution.get("runtime_readiness")
    )
    readiness = readiness if isinstance(readiness, dict) else {}
    action_verification = result.get("action_verification")
    action_verification = (
        action_verification
        if isinstance(action_verification, dict)
        else {}
    )

    execution_specific_check = (
        _check(
            "deterministic_action_verified",
            action_verification.get("passed") is True,
            actual=action_verification,
            expected={"passed": True},
        )
        if deterministic_action
        else _check(
            "runtime_readiness_passed",
            readiness.get("ready") is True,
            actual=readiness.get("ready"),
            expected=True,
        )
    )

    checks = [
        _check(
            "work_terminal_completed",
            str(work.get("state") or "").casefold() == "completed",
            actual=work.get("state"),
            expected="completed",
        ),
        _check(
            "execution_terminal_completed",
            str(work.get("execution_state") or "").casefold() == "completed",
            actual=work.get("execution_state"),
            expected="completed",
        ),
        _check(
            "semantic_completion_success",
            str(semantic.get("status") or "").casefold() == "success",
            actual=semantic.get("status"),
            expected="success",
        ),
        execution_specific_check,
        _check(
            "no_failure_class",
            not str(work.get("failure_class") or "").strip(),
            actual=work.get("failure_class"),
            expected=None,
        ),
        _check(
            "no_execution_error",
            not str(work.get("error") or "").strip(),
            actual=work.get("error"),
            expected=None,
        ),
    ]

    task = pstack_task if isinstance(pstack_task, dict) else None
    if task is not None:
        lifecycle = task.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        tab = task.get("herdr_tab")
        tab = tab if isinstance(tab, dict) else {}
        environment = task.get("execution_environment")
        environment = environment if isinstance(environment, dict) else {}
        checks.extend(
            [
                _check(
                    "pstack_terminal",
                    (
                        str(task.get("status") or "").casefold() == "completed"
                        and str(lifecycle.get("status") or "").casefold() == "completed"
                        and str(lifecycle.get("current_phase") or "").casefold() == "terminal"
                    ),
                    actual={
                        "task": task.get("status"),
                        "lifecycle": lifecycle.get("status"),
                        "phase": lifecycle.get("current_phase"),
                    },
                    expected={
                        "task": "completed",
                        "lifecycle": "completed",
                        "phase": "terminal",
                    },
                ),
                _check(
                    "disposable_tab_closed",
                    (
                        not tab
                        or bool(tab.get("persistent"))
                        or str(tab.get("status") or "").casefold()
                        in {"closed", "unavailable"}
                    ),
                    actual=tab.get("status") if tab else None,
                    expected="closed_or_persistent",
                ),
                _check(
                    "execution_environment_closed",
                    (
                        not environment
                        or str(environment.get("lifecycle") or "").casefold() != "disposable"
                        or str(environment.get("status") or "").casefold() == "closed"
                    ),
                    actual=environment.get("status") if environment else None,
                    expected="closed_or_persistent",
                ),
            ]
        )

    passed = all(item["passed"] for item in checks)
    return {
        "schema": EXECUTION_CERTIFICATION_SCHEMA,
        "certification_type": "execution",
        "deterministic": True,
        "execution_kind": (
            "deterministic_action" if deterministic_action else "agent_runtime"
        ),
        "passed": passed,
        "outcome": "PASS" if passed else "FAIL",
        "checks": checks,
        "quality_judgement_included": False,
        "work_id": work.get("id"),
    }


def certify_ai_quality(
    work: dict[str, Any],
    submission: dict[str, Any],
    *,
    execution_certification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record quality assessment independently from deterministic execution."""
    execution = (
        execution_certification
        if isinstance(execution_certification, dict)
        else certify_execution(work)
    )
    score = float(submission.get("score", 0.0))
    requested_pass = bool(submission.get("passed"))
    eligible = execution.get("passed") is True
    passed = eligible and requested_pass
    return {
        "schema": AI_QUALITY_CERTIFICATION_SCHEMA,
        "certification_type": "ai_quality",
        "deterministic": False,
        "eligible": eligible,
        "passed": passed,
        "outcome": (
            "PASS"
            if passed
            else ("INELIGIBLE" if not eligible else "FAIL")
        ),
        "score": score,
        "evaluator": str(submission.get("evaluator") or "human"),
        "rubric": submission.get("rubric"),
        "notes": submission.get("notes"),
        "execution_certification": {
            "schema": execution.get("schema"),
            "outcome": execution.get("outcome"),
            "passed": execution.get("passed"),
        },
        "execution_state_mutated": False,
        "work_id": work.get("id"),
    }
