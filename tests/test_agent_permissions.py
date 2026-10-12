from __future__ import annotations

import pytest

from mcp_studio.agent_permissions import resolve_agent_permissions


def test_claude_default_is_reviewed_auto_not_unbounded_bypass():
    plan = resolve_agent_permissions("claude")
    assert plan["agent_args"] == ["--permission-mode", "auto"]
    assert plan["effective_permission_mode"] == "auto"
    assert plan["auto_accept_enabled"] is True
    assert plan["permission_bypass"] is False
    assert plan["manual_approval_possible"] is True


def test_claude_existing_accept_edits_contract_is_not_duplicated():
    plan = resolve_agent_permissions(
        "claude", agent_args=["--permission-mode", "acceptEdits", "--model", "sonnet"]
    )
    assert plan["agent_args"] == [
        "--permission-mode", "acceptEdits", "--model", "sonnet"
    ]
    assert plan["effective_permission_mode"] == "acceptEdits"


def test_codex_uses_workspace_sandbox_when_auto_approving():
    plan = resolve_agent_permissions("codex")
    assert plan["agent_args"] == [
        "--sandbox", "workspace-write", "--ask-for-approval", "never"
    ]
    assert plan["sandbox"] == "workspace-write"
    assert plan["auto_accept_enabled"] is True
    assert plan["permission_bypass"] is False


def test_codex_respects_read_only_sandbox_in_noninteractive_mode():
    plan = resolve_agent_permissions(
        "codex", agent_args=["--sandbox", "read-only"]
    )
    assert plan["agent_args"] == [
        "--sandbox", "read-only", "--ask-for-approval", "never"
    ]


def test_manual_mode_does_not_add_auto_accept_flags():
    assert resolve_agent_permissions("claude", permission_policy="manual")[
        "agent_args"
    ] == []
    assert resolve_agent_permissions("codex", permission_policy="manual")[
        "agent_args"
    ] == []


def test_hermes_never_auto_launches_yolo():
    plan = resolve_agent_permissions(
        "hermes", agent_args=["--provider", "9router", "--model", "test"]
    )
    assert plan["agent_args"] == [
        "--provider", "9router", "--model", "test"
    ]
    assert plan["auto_accept_enabled"] is False
    assert plan["manual_approval_possible"] is True
    assert plan["unsupported_reason"]
    assert "--yolo" not in plan["agent_args"]


@pytest.mark.parametrize("runtime,arg", [
    ("claude", "--dangerously-skip-permissions"),
    ("codex", "--dangerously-bypass-approvals-and-sandbox"),
    ("hermes", "--yolo"),
])
def test_unsafe_bypass_flags_are_rejected(runtime, arg):
    with pytest.raises(ValueError, match="bypass_forbidden"):
        resolve_agent_permissions(runtime, agent_args=[arg])


@pytest.mark.parametrize("value", ["bypassPermissions", "invalid", "yolo"])
def test_unsafe_claude_permission_modes_are_rejected(value):
    with pytest.raises(ValueError, match="unsafe_or_unknown"):
        resolve_agent_permissions("claude", agent_args=["--permission-mode", value])


@pytest.mark.parametrize("value", ["danger-full-access", "foo"])
def test_unsafe_codex_sandbox_modes_are_rejected(value):
    with pytest.raises(ValueError, match="unsafe_sandbox"):
        resolve_agent_permissions("codex", agent_args=["--sandbox", value])


def test_codex_never_approval_requires_a_real_sandbox():
    with pytest.raises(ValueError, match="requires_sandbox"):
        resolve_agent_permissions("codex", permission_policy="manual",
                                  agent_args=["--ask-for-approval", "never"])


def test_unknown_runtime_cannot_claim_auto_approval():
    with pytest.raises(ValueError, match="unsupported"):
        resolve_agent_permissions("unknown", permission_policy="auto_accept")


@pytest.mark.parametrize("policy", ["bypass", "auto_approve_all", "yolo", ""])
def test_unknown_permission_policy_is_rejected(policy):
    with pytest.raises(ValueError, match="policy_unknown"):
        resolve_agent_permissions("claude", permission_policy=policy)
