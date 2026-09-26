from scripts.certify_cognitive_node_routing import compare_case


def _plan(order, *, lane="fast", eligible=True):
    decisions = [
        {
            "runtime": runtime,
            "eligible": eligible,
            "reason": None if eligible else "no_eligible_route",
        }
        for runtime in order
    ]
    return {
        "lane": lane,
        "identity_continuity": True,
        "semantic_authority_changed": False,
        "candidates": [{"runtime": runtime} for runtime in order],
        "infrastructure_routing": {
            "mode": "shadow",
            "filtered_runtimes": [],
            "semantic_authority_changed": False,
            "decisions": decisions,
        },
    }


def test_compare_case_passes_when_shadow_is_semantically_identical():
    legacy = {
        "lane": "fast",
        "candidates": [{"runtime": "hermes"}, {"runtime": "claude"}],
    }
    shadow = _plan(["hermes", "claude"])

    result = compare_case("fast_utility", "fast", legacy, shadow)

    assert result["passed"] is True
    assert result["semantic_drift"] == 0
    assert result["candidate_order_equal"] is True
    assert result["all_routes_eligible"] is True


def test_compare_case_detects_candidate_order_drift():
    legacy = {
        "lane": "fast",
        "candidates": [{"runtime": "hermes"}, {"runtime": "claude"}],
    }
    shadow = _plan(["claude", "hermes"])

    result = compare_case("fast_utility", "fast", legacy, shadow)

    assert result["passed"] is False
    assert result["semantic_drift"] == 1
    assert result["candidate_order_equal"] is False


def test_compare_case_fails_when_n7_route_is_not_eligible():
    legacy = {
        "lane": "deep",
        "candidates": [{"runtime": "claude"}],
    }
    shadow = _plan(["claude"], lane="deep", eligible=False)

    result = compare_case("reasoning_high", "deep", legacy, shadow)

    assert result["passed"] is False
    assert result["semantic_drift"] == 0
    assert result["all_routes_eligible"] is False
