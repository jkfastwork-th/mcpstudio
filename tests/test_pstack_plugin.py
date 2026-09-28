from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from mcp_studio.cognitive_router import normalize_cognitive_request
from mcp_studio.plugin_folder import inspect_plugin_folder


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "pstack"
ADAPTER_PATH = PLUGIN_ROOT / "adapter.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("hirda_test_pstack", ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pstack_plugin_manifest_is_runtime_neutral_and_preflight_safe():
    candidate = inspect_plugin_folder(PLUGIN_ROOT)

    assert candidate.preflight_ok is True
    assert candidate.plugin_id == "pstack"
    assert candidate.quarantine_reason == "trust_not_established"
    assert set(candidate.permissions.values()) == {"read"}
    assert candidate.metadata["runtime_authority"] == "hirda"
    assert candidate.metadata["model_selection"] == "deferred_to_hirda"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Fix the reconnect crash regression.", "bug-fix"),
        ("Add a workflow adapter for coding tasks.", "feature"),
        ("Review this PR diff for subtle bugs.", "review"),
    ],
)
async def test_three_pstack_workflows_route_without_pinning_runtime(goal, expected):
    module = _load_module()
    adapter = module.PstackIntegration(repository_root=ROOT)

    result = await adapter.call_tool(
        "route_task",
        {"goal": goal},
        context={"managed_session_id": "test"},
    )

    assert result["workflow"] == expected
    assert result["runtime_authority"] == "hirda"
    assert result["model_selection"] == "deferred"
    assert result["methodology"]
    request = result["coordinator"]["hirda_request"]
    assert "preferred_runtime" not in request

    normalized = normalize_cognitive_request(request)
    assert normalized.preferred_runtime is None
    assert normalized.capability == "coding"
    assert normalized.preferred_depth == "deep"
    assert normalized.context["workflow_provider"] == "pstack"
    assert normalized.context["workflow"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "capability", "depth"),
    [
        ("code", "coding", "deep"),
        ("investigator", "research", "deep"),
        ("reviewer", "coding", "deep"),
        ("judgment", "reasoning_high", "deep"),
        ("prose", "reflection", "auto"),
    ],
)
async def test_role_translation_delegates_runtime_authority_to_hirda(
    role, capability, depth
):
    module = _load_module()
    adapter = module.PstackIntegration(repository_root=ROOT)

    result = await adapter.call_tool(
        "build_role_request",
        {"role": role, "task": "Do the assigned slice.", "workflow": "feature"},
        context={},
    )
    request = normalize_cognitive_request(result["hirda_request"])

    assert request.capability == capability
    assert request.preferred_depth == depth
    assert request.preferred_runtime is None
    assert request.context["runtime_authority"] == "hirda"
    assert request.context["role"] == role


@pytest.mark.asyncio
async def test_pstack_adapter_validates_pinned_upstream_and_certifies_three_routes():
    module = _load_module()
    adapter = module.PstackIntegration(repository_root=ROOT)

    validation = await adapter.validate()
    certification = await adapter.certify()

    assert validation["ok"] is True
    assert validation["observed_revision"] == module.PSTACK_REVISION
    assert validation["missing_workflows"] == []
    assert certification["ok"] is True
    assert set(certification["routes"]) == {"bug-fix", "feature", "review"}


@pytest.mark.asyncio
async def test_trusted_plugin_lifecycle_exposes_pstack_backend_without_runtime_pin():
    from mcp_studio.integrations import IntegrationManager

    candidate = inspect_plugin_folder(PLUGIN_ROOT)
    assert candidate.fingerprint

    manager = IntegrationManager()
    manager.configure_plugin_folder(
        ROOT / "plugins",
        runtime_enabled=True,
        trusted_fingerprints=[candidate.fingerprint],
    )
    activated = manager.activate_trusted_plugins()
    assert "pstack" in activated["activated"]

    detail = await manager.reconcile("pstack")
    assert detail["stage"] == "ready"
    snapshot = manager.plugin_folder_snapshot()
    pstack = next(item for item in snapshot["plugins"] if item["id"] == "pstack")
    assert pstack["status"]["code_loaded"] is True

    result = await manager.call_backend_tool(
        "hirda__pstack__route_task",
        {"goal": "Fix the reconnect crash regression."},
        context={"managed_session_id": "cert-pstack"},
    )
    assert result["workflow"] == "bug-fix"
    assert "preferred_runtime" not in result["coordinator"]["hirda_request"]
    assert result["runtime_authority"] == "hirda"
