from scripts.certify_cognitive_enforce_cutover import compare_live_enforce


def _baseline(order, lane="fast"):
    return {
        "lane": lane,
        "candidates": [{"runtime": runtime} for runtime in order],
    }


def _enforce(order, lane="fast", *, filtered=None, eligible=True):
    decisions = []
    for runtime in order:
        decisions.append(
            {
                "runtime": runtime,
                "eligible": eligible,
                "selected": {"node_id": "openclaw"} if eligible else None,
            }
        )
    return {
        "lane": lane,
        "identity_continuity": True,
        "semantic_authority_changed": False,
        "candidates": [{"runtime": runtime} for runtime in order],
        "infrastructure_routing": {
            "mode": "enforce",
            "enforced": True,
            "routing_authority": "infrastructure_eligibility_only",
            "semantic_authority_changed": False,
            "filtered_runtimes": filtered or [],
            "decisions": decisions,
        },
    }


def test_live_enforce_matches_baseline():
    result = compare_live_enforce(
        _baseline(["hermes", "codex"]),
        _enforce(["hermes", "codex"]),
    )
    assert result["passed"] is True
    assert result["semantic_drift"] == 0


def test_live_enforce_detects_order_drift():
    result = compare_live_enforce(
        _baseline(["hermes", "codex"]),
        _enforce(["codex", "hermes"]),
    )
    assert result["passed"] is False
    assert result["semantic_drift"] == 1


def test_live_enforce_rejects_unexpected_filtering():
    result = compare_live_enforce(
        _baseline(["hermes", "codex"]),
        _enforce(["hermes", "codex"], filtered=["claude"]),
    )
    assert result["passed"] is False


def test_live_enforce_requires_candidate_eligibility():
    result = compare_live_enforce(
        _baseline(["hermes"]),
        _enforce(["hermes"], eligible=False),
    )
    assert result["passed"] is False
