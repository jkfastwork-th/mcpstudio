"""Server-owned, bounded Herdr agent approval launch policy.

CLI approval modes never replace HIRDA's managed-session ownership, tool
permission, workspace scope, or PStack gates. Never emit unconditional host
permission-bypass flags.
"""
from __future__ import annotations

from typing import Any

_POLICY_VALUES = frozenset({"auto_accept", "manual"})
_CLAUDE_MODES = frozenset({"auto", "acceptEdits", "default", "plan", "dontAsk"})
_FORBIDDEN_FLAGS = frozenset({
    "--yolo", "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
})


def resolve_agent_permissions(
    runtime: str,
    *,
    permission_policy: str = "auto_accept",
    agent_args: list[str] | None = None,
) -> dict[str, Any]:
    """Return effective args and truthful, auditable permission semantics.

    auto_accept:
      Claude: let Claude auto-review actions (not bypassPermissions).
      Codex: noninteractive approvals within workspace-write sandbox.
      Hermes: keep its separate danger-approval policy; no safe CLI flag
              documented for 'smart' and --yolo is deliberately forbidden.
    """
    kind = str(runtime or "").strip().casefold()
    policy = str(permission_policy or "").strip().casefold()
    if policy not in _POLICY_VALUES:
        raise ValueError("herdr_permission_policy_unknown")

    args = [str(arg) for arg in (agent_args or []) if str(arg).strip()]
    if any(
        arg in _FORBIDDEN_FLAGS
        or arg.startswith("--permission-mode=")
        or arg.startswith("--ask-for-approval=")
        or arg.startswith("--sandbox=")
        or arg.startswith("--dangerously-")
        for arg in args
    ):
        raise ValueError("herdr_permission_bypass_forbidden")
    for named_flag in ("--permission-mode", "--ask-for-approval", "--sandbox"):
        if args.count(named_flag) > 1:
            raise ValueError("herdr_duplicate_permission_argument")

    contract: dict[str, Any] = {
        "permission_policy": policy,
        "permission_authority": "hirda",
        "permission_bypass": False,
        "runtime": kind,
    }

    if kind == "claude":
        if "--permission-mode" in args:
            index = args.index("--permission-mode")
            mode = args[index + 1] if index + 1 < len(args) else ""
            if mode not in _CLAUDE_MODES:
                raise ValueError("herdr_claude_permission_mode_unsafe_or_unknown")
        elif policy == "auto_accept":
            mode = "auto"
            args.extend(["--permission-mode", mode])
        else:
            mode = "default"
        contract.update({
            "effective_permission_mode": mode,
            "auto_accept_enabled": mode in {"auto", "acceptEdits"},
            "manual_approval_possible": True,
        })
    elif kind == "codex":
        if "--sandbox" in args:
            i = args.index("--sandbox")
            sandbox = args[i + 1] if i + 1 < len(args) else ""
            if sandbox not in {"read-only", "workspace-write"}:
                raise ValueError("herdr_codex_unsafe_sandbox_forbidden")
        else:
            sandbox = "workspace-write" if policy == "auto_accept" else None
            if sandbox:
                args.extend(["--sandbox", sandbox])
        if "--ask-for-approval" in args:
            i = args.index("--ask-for-approval")
            approval = args[i + 1] if i + 1 < len(args) else ""
            if approval not in {"never", "on-request", "untrusted"}:
                raise ValueError("herdr_codex_unsafe_approval_forbidden")
        else:
            approval = "never" if policy == "auto_accept" else "on-request"
            if policy == "auto_accept":
                args.extend(["--ask-for-approval", approval])
        if approval == "never" and sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("herdr_codex_noninteractive_requires_sandbox")
        contract.update({
            "effective_permission_mode": approval,
            "sandbox": sandbox,
            "auto_accept_enabled": approval == "never",
            "manual_approval_possible": approval != "never",
        })
    elif kind == "hermes":
        # Hermes --yolo bypasses dangerous-command approvals. Never enable it.
        contract.update({
            "effective_permission_mode": "hermes_config",
            "auto_accept_enabled": False,
            "manual_approval_possible": True,
            "unsupported_reason": (
                "hermes_auto_accept_requires_unbounded_yolo;"
                "retain_hermes_manual_or_smart_approval"
            ) if policy == "auto_accept" else None,
        })
    else:
        if policy == "auto_accept":
            raise ValueError("herdr_runtime_auto_accept_unsupported")
        contract.update({
            "effective_permission_mode": "unknown",
            "auto_accept_enabled": False,
            "manual_approval_possible": True,
        })

    contract["agent_args"] = args
    return contract
