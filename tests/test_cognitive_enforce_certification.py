from scripts.certify_cognitive_enforce_routing import (
    compare_filtered_case,
    compare_healthy_case,
)


def _baseline(order, *, lane="fast"):
    return {
        "lane": lane,
        "candidates": [{"runtime": runtime} for runtime in order],
    }


def _enforce(
    order,
    *,
    lane="fast",
    filtered_runtime=None,
    rejection="backend_unavailable",
):
    decisions = []
    all_runtimes = list(order)
    if filtered_runtime is not None:
        all_runtimes = [filtered_runtime, *all_runtimes]
    for runtime in all_runtimes:
        if runtime == filtered_runtime:
            decisions.append(
                {
                    "runtime": runtime,
                    "eligible": False,
                    "reason": "no_eligible_route",
                    "route_rejections": [
                        {
                            "reason": rejection,
                        }
                    ],
                }
            )
        else:
            decisions.append(
                {
                    "runtime": runtime,
                    "eligible": True,
                    "reason": None,
                    "selected": {
                        "node_id": "openclaw",
                    },
                }
            )
    return {
        "lane": lane,
        "identity_continuity": True,
        "semantic_authority_changed": False,
        "candidates": [{"runtime": runtime} for runtime in order],
        "infrastructure_routing": {
            "mode": "enforce",
            "routing_authority": "infrastructure_eligibility_only",
            "semantic_authority_changed": False,
            "filtered_runtimes": (
                [filtered_runtime] if filtered_runtime is not None else []
            ),
            "decisions": decisions,
        },
    }


def test_healthy_enforce_preserves_semantic_order():
    baseline = _baseline(["hermes", "codex"])
    enforce = _enforce(["hermes", "codex"])

    result = compare_healthy_case(baseline, enforce)

    assert result["passed"] is True
    assert result["semantic_drift"] == 0
    assert result["candidate_order_equal"] is True


def test_filtered_primary_preserves_survivor_order_without_semantic_drift():
    baseline = _baseline(["hermes", "codex"])
    enforce = _enforce(
        ["codex"],
        filtered_runtime="hermes",
        rejection="backend_unavailable",
    )

    result = compare_filtered_case(
        baseline,
        enforce,
        filtered_runtime="hermes",
        expected_rejection="backend_unavailable",
    )

    assert result["passed"] is True
    assert result["filtered_before_dispatch"] is True
    assert result["survivor_order_preserved"] is True
    assert result["enforce_candidates"] == ["codex"]
    assert result["semantic_drift"] == 0


def test_filtered_case_fails_when_fallback_order_changes():
    baseline = _baseline(["hermes", "codex", "claude"])
    enforce = _enforce(
        ["claude", "codex"],
        filtered_runtime="hermes",
        rejection="backend_unavailable",
    )

    result = compare_filtered_case(
        baseline,
        enforce,
        filtered_runtime="hermes",
        expected_rejection="backend_unavailable",
    )

    assert result["passed"] is False
    assert result["survivor_order_preserved"] is False
    assert result["semantic_drift"] == 1


def test_filtered_case_requires_expected_n7_rejection():
    baseline = _baseline(["hermes", "codex"])
    enforce = _enforce(
        ["codex"],
        filtered_runtime="hermes",
        rejection="health_stale",
    )

    result = compare_filtered_case(
        baseline,
        enforce,
        filtered_runtime="hermes",
        expected_rejection="backend_unavailable",
    )

    assert result["passed"] is False
    assert result["filtered_before_dispatch"] is False
